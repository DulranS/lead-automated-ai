"""
BizPilot - FastAPI Backend
REST API for lead ingestion, message management, and analytics
"""
from fastapi import FastAPI, HTTPException, BackgroundTasks, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from typing import List, Optional
from datetime import datetime, timedelta
from pydantic import BaseModel, EmailStr
import structlog

from models import (
    Lead, Message, Interaction, Experiment, ModelLog,
    LeadSource, LeadIntent, MessageStatus, Channel,
    LeadCreate, MessageReview,
    get_session, init_db
)
from rag_pipeline import RAGPipeline, initialize_sample_knowledge_base
from message_generator import MessageGenerator
from worker_tasks import process_new_lead, send_message


# Initialize structured logging
logger = structlog.get_logger()

# Initialize FastAPI app
app = FastAPI(
    title="BizPilot API",
    description="Autonomous Growth Ops Platform",
    version="1.0.0"
)

# CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Configure for production
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# ============================================================================
# DEPENDENCY INJECTION
# ============================================================================

def get_rag_pipeline():
    """Dependency: RAG pipeline"""
    return RAGPipeline()


def get_message_generator():
    """Dependency: Message generator"""
    return MessageGenerator()


# ============================================================================
# REQUEST/RESPONSE MODELS
# ============================================================================

class LeadIngestWebhook(BaseModel):
    """Webhook payload for lead ingestion"""
    source: LeadSource
    name: str
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    company: Optional[str] = None
    message: Optional[str] = None
    metadata: Optional[dict] = {}


class LeadResponse(BaseModel):
    """Lead response model"""
    id: int
    name: str
    email: Optional[str]
    company: Optional[str]
    source: str
    intent: Optional[str]
    intent_confidence: Optional[float]
    created_at: datetime
    
    class Config:
        from_attributes = True


class MessageResponse(BaseModel):
    """Message response model"""
    id: int
    lead_id: int
    subject: Optional[str]
    body: str
    channel: str
    status: str
    confidence_score: Optional[float]
    created_at: datetime
    
    class Config:
        from_attributes = True


class DashboardMetrics(BaseModel):
    """Dashboard metrics response"""
    total_leads: int
    leads_today: int
    hot_leads: int
    warm_leads: int
    cold_leads: int
    messages_sent: int
    messages_pending_review: int
    avg_response_time_minutes: float
    conversion_rate: float


# ============================================================================
# STARTUP/SHUTDOWN
# ============================================================================

@app.on_event("startup")
async def startup_event():
    """Initialize on startup"""
    logger.info("🚀 Starting BizPilot API")
    
    # Initialize database
    init_db()
    
    # Initialize sample knowledge base
    initialize_sample_knowledge_base()
    
    # Index knowledge base
    rag = RAGPipeline()
    rag.index_knowledge_base()
    
    logger.info("✓ BizPilot API ready")


# ============================================================================
# HEALTH CHECK
# ============================================================================

@app.get("/health")
async def health_check():
    """Health check endpoint"""
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}


# ============================================================================
# LEAD ENDPOINTS
# ============================================================================

@app.post("/api/leads", response_model=LeadResponse, status_code=201)
async def create_lead(
    lead_data: LeadIngestWebhook,
    background_tasks: BackgroundTasks
):
    """
    Ingest a new lead and trigger async processing
    
    This endpoint:
    1. Creates lead record
    2. Triggers background job for RAG classification
    3. Generates follow-up message
    4. Returns immediately (non-blocking)
    """
    logger.info("lead_ingested", source=lead_data.source, email=lead_data.email)
    
    session = get_session()
    
    try:
        # Create lead
        lead = Lead(
            name=lead_data.name,
            email=lead_data.email,
            phone=lead_data.phone,
            company=lead_data.company,
            source=lead_data.source,
            source_metadata={
                'message': lead_data.message,
                **lead_data.metadata
            }
        )
        
        session.add(lead)
        session.commit()
        session.refresh(lead)
        
        lead_id = lead.id
        
        # Track interaction
        interaction = Interaction(
            lead_id=lead_id,
            interaction_type='lead_created',
            metadata={'source': lead_data.source.value}
        )
        session.add(interaction)
        session.commit()
        
        # Trigger background processing
        background_tasks.add_task(process_new_lead, lead_id)
        
        logger.info("lead_created", lead_id=lead_id)
        
        return LeadResponse.from_orm(lead)
    
    except Exception as e:
        session.rollback()
        logger.error("lead_creation_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    
    finally:
        session.close()


@app.get("/api/leads", response_model=List[LeadResponse])
async def list_leads(
    intent: Optional[LeadIntent] = None,
    limit: int = 50,
    offset: int = 0
):
    """List leads with optional filtering"""
    session = get_session()
    
    try:
        query = session.query(Lead)
        
        if intent:
            query = query.filter(Lead.intent == intent)
        
        leads = query.order_by(Lead.created_at.desc()).limit(limit).offset(offset).all()
        
        return [LeadResponse.from_orm(lead) for lead in leads]
    
    finally:
        session.close()


@app.get("/api/leads/{lead_id}", response_model=LeadResponse)
async def get_lead(lead_id: int):
    """Get specific lead"""
    session = get_session()
    
    try:
        lead = session.query(Lead).filter(Lead.id == lead_id).first()
        
        if not lead:
            raise HTTPException(status_code=404, detail="Lead not found")
        
        return LeadResponse.from_orm(lead)
    
    finally:
        session.close()


# ============================================================================
# MESSAGE ENDPOINTS
# ============================================================================

@app.get("/api/messages", response_model=List[MessageResponse])
async def list_messages(
    status: Optional[MessageStatus] = None,
    lead_id: Optional[int] = None,
    limit: int = 50
):
    """List messages with optional filtering"""
    session = get_session()
    
    try:
        query = session.query(Message)
        
        if status:
            query = query.filter(Message.status == status)
        if lead_id:
            query = query.filter(Message.lead_id == lead_id)
        
        messages = query.order_by(Message.created_at.desc()).limit(limit).all()
        
        return [MessageResponse.from_orm(msg) for msg in messages]
    
    finally:
        session.close()


@app.post("/api/messages/{message_id}/review")
async def review_message(
    message_id: int,
    review: MessageReview,
    background_tasks: BackgroundTasks
):
    """
    Human-in-the-loop: Review and approve/edit/reject message
    
    Actions:
    - 'approve': Send as-is
    - 'edit': Update content and send
    - 'reject': Mark as rejected
    """
    session = get_session()
    
    try:
        message = session.query(Message).filter(Message.id == message_id).first()
        
        if not message:
            raise HTTPException(status_code=404, detail="Message not found")
        
        if review.action == 'approve':
            message.status = MessageStatus.APPROVED
            # Trigger send
            background_tasks.add_task(send_message, message_id)
            
        elif review.action == 'edit':
            message.human_edited = True
            message.edit_count += 1
            
            if review.edited_subject:
                message.subject = review.edited_subject
            if review.edited_body:
                message.body = review.edited_body
            
            message.status = MessageStatus.EDITED
            # Trigger send
            background_tasks.add_task(send_message, message_id)
            
        elif review.action == 'reject':
            message.status = MessageStatus.REJECTED
        
        session.commit()
        
        logger.info("message_reviewed", message_id=message_id, action=review.action)
        
        return {"status": "success", "action": review.action}
    
    except Exception as e:
        session.rollback()
        logger.error("message_review_failed", error=str(e))
        raise HTTPException(status_code=500, detail=str(e))
    
    finally:
        session.close()


# ============================================================================
# ANALYTICS ENDPOINTS
# ============================================================================

@app.get("/api/dashboard/metrics", response_model=DashboardMetrics)
async def get_dashboard_metrics():
    """Get dashboard metrics"""
    session = get_session()
    
    try:
        # Total leads
        total_leads = session.query(Lead).count()
        
        # Leads today
        today = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
        leads_today = session.query(Lead).filter(Lead.created_at >= today).count()
        
        # By intent
        hot_leads = session.query(Lead).filter(Lead.intent == LeadIntent.HOT).count()
        warm_leads = session.query(Lead).filter(Lead.intent == LeadIntent.WARM).count()
        cold_leads = session.query(Lead).filter(Lead.intent == LeadIntent.COLD).count()
        
        # Messages
        messages_sent = session.query(Message).filter(Message.status == MessageStatus.SENT).count()
        messages_pending = session.query(Message).filter(
            Message.status == MessageStatus.GENERATED
        ).count()
        
        # Response time
        avg_response_time = session.query(Lead).filter(
            Lead.last_contact_at.isnot(None)
        ).all()
        
        if avg_response_time:
            time_diffs = [
                (lead.last_contact_at - lead.created_at).total_seconds() / 60
                for lead in avg_response_time
            ]
            avg_response_minutes = sum(time_diffs) / len(time_diffs)
        else:
            avg_response_minutes = 0.0
        
        # Conversion rate (simplified: replied / sent)
        replied = session.query(Message).filter(Message.replied == True).count()
        conversion_rate = (replied / messages_sent * 100) if messages_sent > 0 else 0.0
        
        return DashboardMetrics(
            total_leads=total_leads,
            leads_today=leads_today,
            hot_leads=hot_leads,
            warm_leads=warm_leads,
            cold_leads=cold_leads,
            messages_sent=messages_sent,
            messages_pending_review=messages_pending,
            avg_response_time_minutes=round(avg_response_minutes, 1),
            conversion_rate=round(conversion_rate, 1)
        )
    
    finally:
        session.close()


@app.get("/api/analytics/model-performance")
async def get_model_performance():
    """Get model performance metrics"""
    session = get_session()
    
    try:
        # Last 24 hours
        yesterday = datetime.utcnow() - timedelta(days=1)
        
        logs = session.query(ModelLog).filter(
            ModelLog.created_at >= yesterday
        ).all()
        
        if not logs:
            return {
                "total_calls": 0,
                "avg_latency_ms": 0,
                "avg_confidence": 0,
                "total_cost_usd": 0
            }
        
        total_calls = len(logs)
        avg_latency = sum(log.latency_ms for log in logs) / total_calls
        avg_confidence = sum(log.confidence_score or 0 for log in logs) / total_calls
        total_cost = sum(log.cost_usd or 0 for log in logs)
        
        return {
            "total_calls": total_calls,
            "avg_latency_ms": round(avg_latency, 0),
            "avg_confidence": round(avg_confidence, 2),
            "total_cost_usd": round(total_cost, 4),
            "time_period": "24h"
        }
    
    finally:
        session.close()


# ============================================================================
# WEBHOOK ENDPOINTS (for external integrations)
# ============================================================================

@app.post("/webhooks/typeform")
async def typeform_webhook(payload: dict, background_tasks: BackgroundTasks):
    """Webhook for Typeform submissions"""
    # Parse Typeform payload
    form_response = payload.get('form_response', {})
    answers = form_response.get('answers', [])
    
    # Extract fields (adjust based on your form)
    lead_data = {}
    for answer in answers:
        field_type = answer.get('type')
        if field_type == 'email':
            lead_data['email'] = answer.get('email')
        elif field_type == 'short_text':
            if 'name' not in lead_data:
                lead_data['name'] = answer.get('text')
    
    # Create lead
    lead_webhook = LeadIngestWebhook(
        source=LeadSource.GOOGLE_FORM,
        name=lead_data.get('name', 'Unknown'),
        email=lead_data.get('email'),
        metadata=payload
    )
    
    return await create_lead(lead_webhook, background_tasks)


@app.post("/webhooks/whatsapp")
async def whatsapp_webhook(payload: dict, background_tasks: BackgroundTasks):
    """Webhook for WhatsApp messages (Twilio)"""
    # Parse WhatsApp payload
    from_number = payload.get('From', '')
    body = payload.get('Body', '')
    
    lead_webhook = LeadIngestWebhook(
        source=LeadSource.WHATSAPP,
        name=from_number,
        phone=from_number,
        message=body,
        metadata=payload
    )
    
    return await create_lead(lead_webhook, background_tasks)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)