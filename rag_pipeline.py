"""
BizPilot - RAG Pipeline
Retrieval-Augmented Generation for lead classification and message generation
"""
from typing import List, Dict, Tuple, Optional
import chromadb
from chromadb.config import Settings
from sentence_transformers import SentenceTransformer
import numpy as np
from dataclasses import dataclass
import os
import json

from models import KnowledgeBase, get_session


@dataclass
class RetrievedContext:
    """Retrieved context from vector DB"""
    doc_id: str
    content: str
    score: float
    metadata: Dict


class EmbeddingService:
    """
    Handle embeddings for RAG pipeline
    Supports multiple backends: local (sentence-transformers) or API (OpenAI)
    """
    
    def __init__(self, model_name: str = "all-MiniLM-L6-v2"):
        """
        Initialize embedding service
        
        Args:
            model_name: Local model name or 'openai' for API
        """
        self.model_name = model_name
        
        if model_name == "openai":
            # Use OpenAI embeddings API (higher quality, costs money)
            import openai
            self.client = openai.OpenAI(api_key=os.getenv('OPENAI_API_KEY'))
            self.embedding_model = None
        else:
            # Use local sentence-transformers (free, faster, lower quality)
            self.embedding_model = SentenceTransformer(model_name)
            self.client = None
        
        print(f"✓ Embedding service initialized: {model_name}")
    
    def embed(self, text: str) -> List[float]:
        """Generate embedding for text"""
        if self.client:
            # OpenAI API
            response = self.client.embeddings.create(
                model="text-embedding-3-small",
                input=text
            )
            return response.data[0].embedding
        else:
            # Local model
            embedding = self.embedding_model.encode(text, convert_to_numpy=True)
            return embedding.tolist()
    
    def embed_batch(self, texts: List[str]) -> List[List[float]]:
        """Generate embeddings for multiple texts"""
        if self.client:
            response = self.client.embeddings.create(
                model="text-embedding-3-small",
                input=texts
            )
            return [item.embedding for item in response.data]
        else:
            embeddings = self.embedding_model.encode(texts, convert_to_numpy=True)
            return embeddings.tolist()


class VectorStore:
    """
    Vector database for semantic search
    Uses ChromaDB for simplicity (can swap for Pinecone/Weaviate in production)
    """
    
    def __init__(self, collection_name: str = "bizpilot_knowledge"):
        """Initialize vector store"""
        # Use persistent storage
        persist_dir = os.getenv('CHROMA_PERSIST_DIR', './chroma_db')
        
        self.client = chromadb.Client(Settings(
            persist_directory=persist_dir,
            anonymized_telemetry=False
        ))
        
        # Get or create collection
        self.collection = self.client.get_or_create_collection(
            name=collection_name,
            metadata={"hnsw:space": "cosine"}
        )
        
        print(f"✓ Vector store initialized: {collection_name}")
    
    def add_documents(self, documents: List[Dict], embeddings: List[List[float]]):
        """
        Add documents to vector store
        
        Args:
            documents: List of {id, content, metadata}
            embeddings: Corresponding embeddings
        """
        self.collection.add(
            ids=[doc['id'] for doc in documents],
            embeddings=embeddings,
            documents=[doc['content'] for doc in documents],
            metadatas=[doc.get('metadata', {}) for doc in documents]
        )
        print(f"✓ Added {len(documents)} documents to vector store")
    
    def search(self, query_embedding: List[float], n_results: int = 5, 
               filter_metadata: Optional[Dict] = None) -> List[RetrievedContext]:
        """
        Semantic search
        
        Args:
            query_embedding: Query vector
            n_results: Number of results to return
            filter_metadata: Filter by metadata (e.g., {'doc_type': 'faq'})
        
        Returns:
            List of RetrievedContext objects
        """
        results = self.collection.query(
            query_embeddings=[query_embedding],
            n_results=n_results,
            where=filter_metadata
        )
        
        contexts = []
        for i in range(len(results['ids'][0])):
            contexts.append(RetrievedContext(
                doc_id=results['ids'][0][i],
                content=results['documents'][0][i],
                score=1.0 - results['distances'][0][i],  # Convert distance to similarity
                metadata=results['metadatas'][0][i]
            ))
        
        return contexts
    
    def delete_document(self, doc_id: str):
        """Delete document from vector store"""
        self.collection.delete(ids=[doc_id])


class RAGPipeline:
    """
    Main RAG pipeline for lead classification and context retrieval
    """
    
    def __init__(self, embedding_model: str = "all-MiniLM-L6-v2"):
        """Initialize RAG pipeline"""
        self.embedding_service = EmbeddingService(embedding_model)
        self.vector_store = VectorStore()
        print("✓ RAG pipeline initialized")
    
    def index_knowledge_base(self, force_refresh: bool = False):
        """
        Index all knowledge base documents into vector store
        
        Args:
            force_refresh: Re-index even if already indexed
        """
        session = get_session()
        
        # Get all active knowledge base documents
        docs = session.query(KnowledgeBase).filter_by(active=True).all()
        
        if not docs:
            print("⚠ No knowledge base documents found")
            session.close()
            return
        
        # Check which need indexing
        docs_to_index = []
        for doc in docs:
            if force_refresh or not doc.embedding_id:
                docs_to_index.append(doc)
        
        if not docs_to_index:
            print("✓ Knowledge base already indexed")
            session.close()
            return
        
        print(f"📚 Indexing {len(docs_to_index)} documents...")
        
        # Generate embeddings
        contents = [doc.content for doc in docs_to_index]
        embeddings = self.embedding_service.embed_batch(contents)
        
        # Prepare for vector store
        vector_docs = []
        for i, doc in enumerate(docs_to_index):
            doc_id = f"kb_{doc.id}"
            vector_docs.append({
                'id': doc_id,
                'content': doc.content,
                'metadata': {
                    'title': doc.title,
                    'doc_type': doc.doc_type,
                    'source_url': doc.source_url or '',
                    'priority': doc.priority
                }
            })
            
            # Update database with embedding ID
            doc.embedding_id = doc_id
        
        # Add to vector store
        self.vector_store.add_documents(vector_docs, embeddings)
        
        # Commit changes
        session.commit()
        session.close()
        
        print(f"✓ Indexed {len(docs_to_index)} documents")
    
    def classify_lead(self, lead_data: Dict) -> Dict:
        """
        Classify lead intent using RAG
        
        Args:
            lead_data: Lead information (name, company, email, source_metadata)
        
        Returns:
            Classification result with intent, confidence, reason, retrieved_docs
        """
        # Build query from lead data
        query_parts = []
        
        if lead_data.get('company'):
            query_parts.append(f"Company: {lead_data['company']}")
        
        # Extract message/inquiry from source metadata
        source_meta = lead_data.get('source_metadata', {})
        if source_meta.get('message'):
            query_parts.append(f"Message: {source_meta['message']}")
        if source_meta.get('subject'):
            query_parts.append(f"Subject: {source_meta['subject']}")
        
        query_text = "\n".join(query_parts) if query_parts else lead_data.get('name', '')
        
        # Generate embedding
        query_embedding = self.embedding_service.embed(query_text)
        
        # Retrieve relevant context
        contexts = self.vector_store.search(
            query_embedding=query_embedding,
            n_results=5
        )
        
        # Build classification prompt
        context_text = "\n\n".join([
            f"[{ctx.doc_id}] {ctx.content}" 
            for ctx in contexts
        ])
        
        # Simple rule-based classification for demo
        # In production, use LLM with retrieved context
        intent, confidence, reason = self._classify_with_rules(
            query_text, 
            contexts,
            source_meta
        )
        
        return {
            'intent': intent,
            'confidence': confidence,
            'reason': reason,
            'retrieved_docs': [ctx.doc_id for ctx in contexts],
            'context_snippets': [ctx.content[:200] for ctx in contexts]
        }
    
    def _classify_with_rules(self, query: str, contexts: List[RetrievedContext], 
                            source_meta: Dict) -> Tuple[str, float, str]:
        """
        Simple rule-based classification (replace with LLM in production)
        """
        query_lower = query.lower()
        
        # Hot signals
        hot_signals = ['urgent', 'asap', 'immediately', 'buy now', 'purchase', 
                      'pricing', 'demo', 'trial', 'start today']
        if any(signal in query_lower for signal in hot_signals):
            return 'hot', 0.85, f"High-intent keywords detected: {[s for s in hot_signals if s in query_lower]}"
        
        # Warm signals
        warm_signals = ['interested', 'learn more', 'information', 'how does', 
                       'tell me about', 'curious', 'exploring']
        if any(signal in query_lower for signal in warm_signals):
            return 'warm', 0.70, f"Interest indicators found: {[s for s in warm_signals if s in query_lower]}"
        
        # Cold/unqualified
        if len(query) < 20:
            return 'cold', 0.60, "Brief inquiry, needs more context"
        
        # Use context relevance
        if contexts and contexts[0].score > 0.7:
            return 'warm', 0.65, f"Relevant to our knowledge base: {contexts[0].metadata.get('title', 'N/A')}"
        
        return 'cold', 0.50, "Standard inquiry, no strong signals"
    
    def retrieve_context_for_message(self, lead_data: Dict, intent: str) -> List[RetrievedContext]:
        """
        Retrieve relevant context for message generation
        
        Args:
            lead_data: Lead information
            intent: Classified intent
        
        Returns:
            Relevant contexts for message generation
        """
        # Build query
        query_parts = []
        
        # Prioritize based on intent
        if intent == 'hot':
            query_parts.append("pricing demo trial purchase")
        elif intent == 'warm':
            query_parts.append("features benefits how it works")
        else:
            query_parts.append("introduction overview getting started")
        
        if lead_data.get('company'):
            query_parts.append(lead_data['company'])
        
        query_text = " ".join(query_parts)
        query_embedding = self.embedding_service.embed(query_text)
        
        # Search with intent-specific filtering
        contexts = self.vector_store.search(
            query_embedding=query_embedding,
            n_results=3,
            filter_metadata={'doc_type': 'faq'} if intent == 'hot' else None
        )
        
        return contexts


def initialize_sample_knowledge_base():
    """Initialize sample knowledge base for demo"""
    session = get_session()
    
    # Check if already populated
    existing = session.query(KnowledgeBase).count()
    if existing > 0:
        print(f"✓ Knowledge base already has {existing} documents")
        session.close()
        return
    
    sample_docs = [
        {
            'title': 'Product Overview',
            'content': 'BizPilot automates lead management and follow-up for small businesses. It uses AI to classify leads, generate personalized responses, and track conversions. Key features include smart triage, automated follow-ups, and performance analytics.',
            'doc_type': 'product_page'
        },
        {
            'title': 'Pricing - Starter Plan',
            'content': 'Our Starter plan is $49/month and includes: up to 100 leads per month, automated triage, email and SMS follow-ups, basic analytics dashboard. Perfect for solopreneurs and small teams.',
            'doc_type': 'faq'
        },
        {
            'title': 'Pricing - Growth Plan',
            'content': 'Growth plan at $149/month includes: up to 500 leads per month, multi-channel follow-ups (email, SMS, WhatsApp), A/B testing, advanced analytics, priority support. Ideal for growing businesses.',
            'doc_type': 'faq'
        },
        {
            'title': 'How It Works',
            'content': 'BizPilot integrates with your existing tools (email, forms, CRM). When a new lead comes in, our AI analyzes their inquiry, classifies their intent (hot/warm/cold), and generates a personalized follow-up message. You can review and edit before sending, or auto-send based on confidence thresholds.',
            'doc_type': 'product_page'
        },
        {
            'title': 'Integration Setup',
            'content': 'Setting up BizPilot takes less than 10 minutes. Connect your email via Gmail/Outlook OAuth, add webhook URLs to your forms, or upload CSV files. We support Google Forms, Typeform, HubSpot, Salesforce, and custom webhooks.',
            'doc_type': 'faq'
        },
        {
            'title': 'Privacy & Security',
            'content': 'We take privacy seriously. All data is encrypted at rest and in transit. We offer PII redaction for sensitive information, and you can request data deletion at any time. For enterprises, we provide on-premise deployment options.',
            'doc_type': 'product_page'
        },
        {
            'title': 'Demo Request',
            'content': 'Want to see BizPilot in action? Book a 15-minute demo with our team. We\'ll walk through your specific use case and show you how BizPilot can improve your lead conversion rates. Calendar link: https://cal.com/bizpilot/demo',
            'doc_type': 'faq'
        },
        {
            'title': 'ROI Metrics',
            'content': 'Our customers typically see: 40-60% faster response times, 15-25% increase in conversion rates, 10-15 hours saved per week on manual follow-ups. Average ROI is 3-5x within the first 3 months.',
            'doc_type': 'case_study'
        }
    ]
    
    for doc_data in sample_docs:
        doc = KnowledgeBase(**doc_data)
        session.add(doc)
    
    session.commit()
    session.close()
    
    print(f"✓ Added {len(sample_docs)} sample documents to knowledge base")


if __name__ == "__main__":
    # Demo usage
    from models import init_db
    
    # Initialize database
    init_db()
    
    # Add sample knowledge base
    initialize_sample_knowledge_base()
    
    # Initialize RAG pipeline
    rag = RAGPipeline()
    
    # Index knowledge base
    rag.index_knowledge_base()
    
    # Test classification
    test_lead = {
        'name': 'John Doe',
        'company': 'Acme Corp',
        'email': 'john@acme.com',
        'source_metadata': {
            'message': 'I want to see a demo of your product ASAP. Can we schedule for tomorrow?'
        }
    }
    
    result = rag.classify_lead(test_lead)
    print(f"\n🎯 Classification Result:")
    print(f"  Intent: {result['intent']}")
    print(f"  Confidence: {result['confidence']:.2f}")
    print(f"  Reason: {result['reason']}")
    print(f"  Retrieved docs: {result['retrieved_docs']}")