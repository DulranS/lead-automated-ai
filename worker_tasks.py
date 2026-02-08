"""
BizPilot - Worker Tasks
Async job processing with Celery for lead classification and message sending
"""
from celery import Celery
import os
from datetime import datetime
import structlog

from models import Lead, Message, Interaction, MessageStatus, get_session
from rag_pipeline import RAGPipeline
from message_generator import MessageGenerator


# Initialize structured logging
logger = structlog.get_logger()

# Initialize Celery
celery_app = Celery(
    'bizpilot',
    broker=os.getenv('REDIS_URL', 'redis://localhost:6379/0'),
    backend=os.getenv('REDIS_URL', 'redis://localhost:6379/0')
)

# Celery configuration
celery_app.conf.update(
    task_serializer='json',
    accept_content=['json'],
    result_serializer='json',
    timezone='UTC',
    enable_utc=True,
    task_track_started=True,
    task_time_limit=300,  # 5 minutes max
    task_soft_time_limit=240,  # 4 minutes soft limit
)


# ============================================================================
# TASKS
# ============================================================================

@celery_app.task(name='process_new_lead', bind=True, max_retries=3)
def process_new_lead(self, lead_id: int):
    """
    Process new lead: RAG classification + message generation
    
    This is the core workflow:
    1. Retrieve lead data
    2. Run RAG pipeline to classify intent
    3. Generate personalized follow-up message
    4. Store message for review
    """
    logger.info("processing_lead", lead_id=lead_id, task_id=self.request.id)
    
    session = get_session()
    
    try:
        # Get lead
        lead = session.query(Lead).filter(Lead.id == lead_id).first()
        if not lead:
            logger.error("lead_not_found", lead_id=lead_id)
            return {'status': 'error', 'message': 'Lead not found'}
        
        # Build lead data dict
        lead_data = {
            'name': lead.name,
            'email': lead.email,
            'company': lead.company,
            'source_metadata': lead.source_metadata or {}
        }
        
        # Step 1: RAG Classification
        rag = RAGPipeline()
        classification = rag.classify_lead(lead_data)
        
        # Update lead with classification
        lead.intent = classification['intent']
        lead.intent_confidence = classification['confidence']
        lead.classification_reason = classification['reason']
        
        logger.info("lead_classified", 
                   lead_id=lead_id,
                   intent=classification['intent'],
                   confidence=classification['confidence'])
        
        # Step 2: Generate Message
        generator = MessageGenerator()
        message_data = generator.generate_message(
            lead_data=lead_data,
            intent=classification['intent']
        )
        
        # Create message record
        message = Message(
            lead_id=lead_id,
            subject=message_data['subject'],
            body=message_data['body'],
            channel=message_data['channel'],
            model_version=message_data['model_version'],
            prompt_template_id=message_data['template_used'],
            confidence_score=message_data['confidence'],
            retrieved_docs=message_data['context_used'],
            context_snippets=message_data['context_snippets'],
            status=MessageStatus.GENERATED
        )
        
        session.add(message)
        
        # Track interaction
        interaction = Interaction(
            lead_id=lead_id,
            interaction_type='message_generated',
            metadata={
                'intent': classification['intent'],
                'confidence': message_data['confidence']
            }
        )
        session.add(interaction)
        
        session.commit()
        
        logger.info("message_generated", 
                   lead_id=lead_id,
                   message_id=message.id,
                   confidence=message_data['confidence'])
        
        # Auto-send if high confidence (>0.85)
        if message_data['confidence'] >= 0.85 and os.getenv('AUTO_SEND_ENABLED', 'false') == 'true':
            send_message.delay(message.id)
        
        return {
            'status': 'success',
            'lead_id': lead_id,
            'message_id': message.id,
            'intent': classification['intent'],
            'confidence': message_data['confidence']
        }
    
    except Exception as e:
        session.rollback()
        logger.error("lead_processing_failed", lead_id=lead_id, error=str(e))
        
        # Retry with exponential backoff
        raise self.retry(exc=e, countdown=2 ** self.request.retries)
    
    finally:
        session.close()


@celery_app.task(name='send_message', bind=True, max_retries=3)
def send_message(self, message_id: int):
    """
    Send approved message via appropriate channel
    
    Supports: Email (SendGrid), SMS (Twilio), WhatsApp (Twilio)
    """
    logger.info("sending_message", message_id=message_id, task_id=self.request.id)
    
    session = get_session()
    
    try:
        # Get message and lead
        message = session.query(Message).filter(Message.id == message_id).first()
        if not message:
            logger.error("message_not_found", message_id=message_id)
            return {'status': 'error', 'message': 'Message not found'}
        
        lead = session.query(Lead).filter(Lead.id == message.lead_id).first()
        if not lead:
            logger.error("lead_not_found", lead_id=message.lead_id)
            return {'status': 'error', 'message': 'Lead not found'}
        
        # Send via appropriate channel
        if message.channel == 'email':
            success, external_id = send_email(
                to=lead.email,
                subject=message.subject,
                body=message.body
            )
        elif message.channel == 'sms':
            success, external_id = send_sms(
                to=lead.phone,
                body=message.body
            )
        elif message.channel == 'whatsapp':
            success, external_id = send_whatsapp(
                to=lead.phone,
                body=message.body
            )
        else:
            logger.error("unsupported_channel", channel=message.channel)
            return {'status': 'error', 'message': f'Unsupported channel: {message.channel}'}
        
        if success:
            # Update message status
            message.status = MessageStatus.SENT
            message.sent_at = datetime.utcnow()
            message.external_id = external_id
            
            # Update lead last contact time
            lead.last_contact_at = datetime.utcnow()
            
            # Track interaction
            interaction = Interaction(
                lead_id=lead.id,
                interaction_type='message_sent',
                channel=message.channel,
                metadata={'message_id': message_id}
            )
            session.add(interaction)
            
            session.commit()
            
            logger.info("message_sent", message_id=message_id, channel=message.channel)
            
            return {
                'status': 'success',
                'message_id': message_id,
                'channel': message.channel,
                'external_id': external_id
            }
        else:
            # Send failed
            message.status = MessageStatus.FAILED
            session.commit()
            
            logger.error("message_send_failed", message_id=message_id)
            
            # Retry
            raise self.retry(countdown=60)
    
    except Exception as e:
        session.rollback()
        logger.error("send_message_error", message_id=message_id, error=str(e))
        raise self.retry(exc=e, countdown=2 ** self.request.retries)
    
    finally:
        session.close()


# ============================================================================
# CHANNEL IMPLEMENTATIONS
# ============================================================================

def send_email(to: str, subject: str, body: str) -> tuple:
    """
    Send email via SendGrid
    
    Returns:
        (success: bool, external_id: str)
    """
    try:
        from sendgrid import SendGridAPIClient
        from sendgrid.helpers.mail import Mail
        
        api_key = os.getenv('SENDGRID_API_KEY')
        if not api_key:
            logger.warning("sendgrid_not_configured", to=to)
            return (True, 'dry_run_email')  # Dry run for demo
        
        message = Mail(
            from_email=os.getenv('SENDGRID_FROM_EMAIL', 'noreply@bizpilot.com'),
            to_emails=to,
            subject=subject,
            html_content=body
        )
        
        sg = SendGridAPIClient(api_key)
        response = sg.send(message)
        
        # Extract message ID from headers
        external_id = response.headers.get('X-Message-Id', 'unknown')
        
        return (True, external_id)
    
    except Exception as e:
        logger.error("sendgrid_error", error=str(e))
        return (False, None)


def send_sms(to: str, body: str) -> tuple:
    """
    Send SMS via Twilio
    
    Returns:
        (success: bool, external_id: str)
    """
    try:
        from twilio.rest import Client
        
        account_sid = os.getenv('TWILIO_ACCOUNT_SID')
        auth_token = os.getenv('TWILIO_AUTH_TOKEN')
        from_number = os.getenv('TWILIO_PHONE_NUMBER')
        
        if not all([account_sid, auth_token, from_number]):
            logger.warning("twilio_not_configured", to=to)
            return (True, 'dry_run_sms')  # Dry run for demo
        
        client = Client(account_sid, auth_token)
        
        message = client.messages.create(
            body=body,
            from_=from_number,
            to=to
        )
        
        return (True, message.sid)
    
    except Exception as e:
        logger.error("twilio_error", error=str(e))
        return (False, None)


def send_whatsapp(to: str, body: str) -> tuple:
    """
    Send WhatsApp message via Twilio
    
    Returns:
        (success: bool, external_id: str)
    """
    try:
        from twilio.rest import Client
        
        account_sid = os.getenv('TWILIO_ACCOUNT_SID')
        auth_token = os.getenv('TWILIO_AUTH_TOKEN')
        
        if not all([account_sid, auth_token]):
            logger.warning("twilio_not_configured", to=to)
            return (True, 'dry_run_whatsapp')
        
        client = Client(account_sid, auth_token)
        
        message = client.messages.create(
            body=body,
            from_='whatsapp:+14155238886',  # Twilio sandbox
            to=f'whatsapp:{to}'
        )
        
        return (True, message.sid)
    
    except Exception as e:
        logger.error("whatsapp_error", error=str(e))
        return (False, None)


if __name__ == "__main__":
    # Start worker with: celery -A worker_tasks worker --loglevel=info
    pass