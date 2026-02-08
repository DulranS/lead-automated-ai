"""
BizPilot - Message Generation Agent
Uses Claude AI to generate personalized follow-up messages with RAG context
"""
from typing import Dict, List, Optional
from anthropic import Anthropic
import os
import time
from datetime import datetime
import json

from models import LeadIntent, Channel, ModelLog, get_session
from rag_pipeline import RAGPipeline, RetrievedContext


class MessageGenerator:
    """
    AI-powered message generation with explainability and confidence scoring
    """
    
    def __init__(self, model_version: str = "claude-sonnet-4-5-20250929"):
        """
        Initialize message generator
        
        Args:
            model_version: Claude model to use
        """
        self.client = Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))
        self.model_version = model_version
        self.rag_pipeline = RAGPipeline()
        
        # Load templates
        self.templates = self._load_templates()
        
        print(f"✓ Message generator initialized: {model_version}")
    
    def _load_templates(self) -> Dict:
        """Load message templates for different intents"""
        return {
            'hot': {
                'name': 'high_intent_followup',
                'system_prompt': """You are a helpful sales assistant for BizPilot, a lead management automation platform.

The lead has shown HIGH purchase intent. Your goal is to:
1. Acknowledge their specific interest/question
2. Provide relevant information from the knowledge base
3. Make it easy for them to take the next step (demo, trial, purchase)

Tone: Professional but friendly. Direct and action-oriented.
Length: 80-120 words max.
Include: Clear CTA with specific next step.""",
                'user_template': """Lead Information:
Name: {name}
Company: {company}
Email: {email}
Message: {message}

Context from our knowledge base:
{context}

Generate a personalized follow-up email that:
1. References their specific inquiry
2. Uses the provided context to answer their needs
3. Includes a clear call-to-action

Format:
SUBJECT: [subject line]
BODY: [email body]"""
            },
            'warm': {
                'name': 'nurture_followup',
                'system_prompt': """You are a helpful sales assistant for BizPilot.

This lead is INTERESTED but needs nurturing. Your goal is to:
1. Acknowledge their interest
2. Educate them on how BizPilot solves their problem
3. Build trust with relevant information

Tone: Helpful and consultative, not pushy.
Length: 100-150 words.
Include: Soft CTA (learn more, see examples, etc.)""",
                'user_template': """Lead Information:
Name: {name}
Company: {company}
Message: {message}

Relevant information:
{context}

Write a nurturing follow-up that:
1. Addresses their inquiry
2. Provides helpful context
3. Invites them to learn more

Format:
SUBJECT: [subject line]
BODY: [email body]"""
            },
            'cold': {
                'name': 'introduction',
                'system_prompt': """You are a helpful assistant for BizPilot.

This lead is EARLY STAGE. Your goal is to:
1. Acknowledge their inquiry professionally
2. Provide a brief introduction to BizPilot
3. Offer resources

Tone: Professional and informative, no hard sell.
Length: 60-100 words.
Include: Resource offer (guide, case study, etc.)""",
                'user_template': """Lead Information:
Name: {name}
Email: {email}
Message: {message}

Company info:
{context}

Write a brief, professional introduction email.

Format:
SUBJECT: [subject line]
BODY: [email body]"""
            }
        }
    
    def generate_message(self, lead_data: Dict, intent: str, 
                        retrieved_contexts: Optional[List[RetrievedContext]] = None) -> Dict:
        """
        Generate personalized follow-up message
        
        Args:
            lead_data: Lead information
            intent: Classified intent (hot/warm/cold)
            retrieved_contexts: Pre-retrieved RAG contexts (optional)
        
        Returns:
            Dict with subject, body, confidence, template_used, context_used, metadata
        """
        start_time = time.time()
        
        # Retrieve contexts if not provided
        if retrieved_contexts is None:
            retrieved_contexts = self.rag_pipeline.retrieve_context_for_message(
                lead_data, 
                intent
            )
        
        # Build context string
        context_text = "\n\n".join([
            f"• {ctx.content[:300]}"
            for ctx in retrieved_contexts[:3]
        ])
        
        # Get template for intent
        template = self.templates.get(intent, self.templates['cold'])
        
        # Build prompt
        user_message = template['user_template'].format(
            name=lead_data.get('name', 'there'),
            company=lead_data.get('company', 'your company'),
            email=lead_data.get('email', ''),
            message=lead_data.get('source_metadata', {}).get('message', 'their inquiry'),
            context=context_text
        )
        
        # Call Claude API
        try:
            response = self.client.messages.create(
                model=self.model_version,
                max_tokens=1000,
                system=template['system_prompt'],
                messages=[
                    {"role": "user", "content": user_message}
                ]
            )
            
            content = response.content[0].text
            
            # Parse response
            subject, body = self._parse_response(content)
            
            # Calculate confidence (based on context relevance)
            confidence = self._calculate_confidence(retrieved_contexts, intent)
            
            # Track latency and tokens
            latency_ms = int((time.time() - start_time) * 1000)
            tokens_used = response.usage.input_tokens + response.usage.output_tokens
            
            # Log model call
            self._log_model_call(
                operation='generate_message',
                input_data={'lead': lead_data, 'intent': intent},
                output_data={'subject': subject, 'body': body},
                latency_ms=latency_ms,
                tokens_used=tokens_used,
                confidence=confidence
            )
            
            return {
                'subject': subject,
                'body': body,
                'channel': 'email',
                'confidence': confidence,
                'template_used': template['name'],
                'context_used': [ctx.doc_id for ctx in retrieved_contexts],
                'context_snippets': [ctx.content[:200] for ctx in retrieved_contexts],
                'model_version': self.model_version,
                'latency_ms': latency_ms,
                'tokens_used': tokens_used
            }
        
        except Exception as e:
            print(f"❌ Error generating message: {e}")
            
            # Fallback to template
            return self._generate_fallback_message(lead_data, intent)
    
    def _parse_response(self, content: str) -> tuple:
        """Parse Claude's response into subject and body"""
        lines = content.split('\n')
        
        subject = ""
        body_lines = []
        in_body = False
        
        for line in lines:
            if line.strip().startswith('SUBJECT:'):
                subject = line.replace('SUBJECT:', '').strip()
            elif line.strip().startswith('BODY:'):
                in_body = True
                # Get text after BODY:
                body_start = line.replace('BODY:', '').strip()
                if body_start:
                    body_lines.append(body_start)
            elif in_body:
                body_lines.append(line)
        
        body = '\n'.join(body_lines).strip()
        
        # Fallback if parsing failed
        if not subject:
            subject = f"Re: Your inquiry about BizPilot"
        if not body:
            body = content
        
        return subject, body
    
    def _calculate_confidence(self, contexts: List[RetrievedContext], intent: str) -> float:
        """
        Calculate confidence score based on context relevance and intent
        
        Higher confidence when:
        - Retrieved contexts have high similarity scores
        - Intent is clear (hot > warm > cold)
        """
        if not contexts:
            return 0.5
        
        # Average similarity score
        avg_similarity = sum(ctx.score for ctx in contexts) / len(contexts)
        
        # Intent multiplier
        intent_weights = {'hot': 1.0, 'warm': 0.85, 'cold': 0.7}
        intent_mult = intent_weights.get(intent, 0.6)
        
        # Combined score
        confidence = min(avg_similarity * intent_mult, 0.95)
        
        return round(confidence, 2)
    
    def _generate_fallback_message(self, lead_data: Dict, intent: str) -> Dict:
        """Generate template-based fallback message if AI fails"""
        templates = {
            'hot': {
                'subject': f"Re: Your BizPilot inquiry",
                'body': f"""Hi {lead_data.get('name', 'there')},

Thanks for your interest in BizPilot!

I'd love to show you how we can help automate your lead management and boost conversions.

Are you available for a quick 15-minute demo this week?

Best,
BizPilot Team"""
            },
            'warm': {
                'subject': f"Learn more about BizPilot",
                'body': f"""Hi {lead_data.get('name', 'there')},

Thanks for reaching out!

BizPilot helps small businesses automate lead triage and follow-ups, typically improving conversion rates by 15-25%.

I'd be happy to share more details about how it works. Would you like to see a quick demo?

Best,
BizPilot Team"""
            },
            'cold': {
                'subject': f"Nice to meet you!",
                'body': f"""Hi {lead_data.get('name', 'there')},

Thanks for your inquiry!

BizPilot is a lead management automation platform for small businesses. We'd be happy to share more information.

Feel free to reply if you'd like to learn more!

Best,
BizPilot Team"""
            }
        }
        
        template = templates.get(intent, templates['cold'])
        
        return {
            'subject': template['subject'],
            'body': template['body'],
            'channel': 'email',
            'confidence': 0.50,
            'template_used': 'fallback',
            'context_used': [],
            'context_snippets': [],
            'model_version': 'fallback',
            'latency_ms': 0,
            'tokens_used': 0
        }
    
    def _log_model_call(self, operation: str, input_data: Dict, output_data: Dict,
                       latency_ms: int, tokens_used: int, confidence: float):
        """Log model call for observability"""
        session = get_session()
        
        # Estimate cost (Claude Sonnet pricing: ~$3/million input tokens, ~$15/million output)
        # Rough estimate: average $0.01 per 1000 tokens
        cost_usd = (tokens_used / 1000) * 0.01
        
        log = ModelLog(
            model_version=self.model_version,
            operation=operation,
            input_data=input_data,
            output_data=output_data,
            latency_ms=latency_ms,
            tokens_used=tokens_used,
            cost_usd=cost_usd,
            confidence_score=confidence
        )
        
        session.add(log)
        session.commit()
        session.close()
    
    def generate_variant(self, lead_data: Dict, intent: str, variant_name: str) -> Dict:
        """
        Generate message variant for A/B testing
        
        Args:
            lead_data: Lead information
            intent: Intent classification
            variant_name: 'control' or 'variant_a', 'variant_b', etc.
        
        Returns:
            Generated message with variant tag
        """
        # For A/B testing, modify system prompt or temperature
        # Example: variant uses different tone
        
        if variant_name == 'control':
            # Standard generation
            return self.generate_message(lead_data, intent)
        
        elif variant_name == 'variant_short':
            # Shorter, punchier version
            # Modify template to be more concise
            pass
        
        # Return standard for now
        return self.generate_message(lead_data, intent)


if __name__ == "__main__":
    # Demo usage
    from models import init_db
    from rag_pipeline import initialize_sample_knowledge_base
    
    # Initialize
    init_db()
    initialize_sample_knowledge_base()
    
    rag = RAGPipeline()
    rag.index_knowledge_base()
    
    generator = MessageGenerator()
    
    # Test lead
    test_lead = {
        'name': 'Sarah Johnson',
        'company': 'TechStart Inc',
        'email': 'sarah@techstart.com',
        'source_metadata': {
            'message': 'I saw your product and want to schedule a demo. Can we talk tomorrow? I need to improve our lead response times urgently.'
        }
    }
    
    # Classify
    classification = rag.classify_lead(test_lead)
    print(f"\n📊 Classification: {classification['intent']} (confidence: {classification['confidence']:.2f})")
    
    # Generate message
    message = generator.generate_message(
        test_lead,
        classification['intent']
    )
    
    print(f"\n✉️  Generated Message:")
    print(f"Subject: {message['subject']}")
    print(f"\n{message['body']}")
    print(f"\nConfidence: {message['confidence']:.2f}")
    print(f"Template: {message['template_used']}")
    print(f"Latency: {message['latency_ms']}ms")
    print(f"Tokens: {message['tokens_used']}")