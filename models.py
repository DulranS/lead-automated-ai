"""
BizPilot - Core Data Models
Lead management, RAG pipeline, and audit trails
"""
from datetime import datetime
from enum import Enum
from typing import Optional, Dict, List
from sqlalchemy import create_engine, Column, Integer, String, Text, DateTime, Float, Boolean, JSON, ForeignKey, Enum as SQLEnum
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, relationship
from pydantic import BaseModel, Field, EmailStr
import os

Base = declarative_base()


# ============================================================================
# ENUMS
# ============================================================================

class LeadSource(str, Enum):
    """Lead ingestion sources"""
    EMAIL = "email"
    GOOGLE_FORM = "google_form"
    WHATSAPP = "whatsapp"
    WEBHOOK = "webhook"
    CSV_UPLOAD = "csv_upload"
    API = "api"


class LeadIntent(str, Enum):
    """Lead classification based on intent"""
    HOT = "hot"           # High purchase intent, ready to buy
    WARM = "warm"         # Interested, needs nurturing
    COLD = "cold"         # Low intent, early stage
    UNQUALIFIED = "unqualified"  # Not a fit


class MessageStatus(str, Enum):
    """Status of follow-up messages"""
    GENERATED = "generated"      # AI generated, awaiting review
    APPROVED = "approved"        # Human approved
    SENT = "sent"               # Successfully sent
    FAILED = "failed"           # Send failed
    EDITED = "edited"           # Human edited before sending
    REJECTED = "rejected"       # Human rejected


class Channel(str, Enum):
    """Communication channels"""
    EMAIL = "email"
    SMS = "sms"
    WHATSAPP = "whatsapp"
    TELEGRAM = "telegram"


# ============================================================================
# DATABASE MODELS
# ============================================================================

class Lead(Base):
    """Core lead entity"""
    __tablename__ = 'leads'
    
    id = Column(Integer, primary_key=True)
    
    # Lead identification
    email = Column(String(255), index=True)
    phone = Column(String(50))
    name = Column(String(255))
    company = Column(String(255))
    
    # Metadata
    source = Column(SQLEnum(LeadSource), nullable=False)
    source_metadata = Column(JSON)  # Raw data from source
    
    # Classification (RAG output)
    intent = Column(SQLEnum(LeadIntent))
    intent_confidence = Column(Float)  # 0.0-1.0
    classification_reason = Column(Text)  # Explainability
    
    # Embedding
    embedding_id = Column(String(255))  # Vector DB reference
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_contact_at = Column(DateTime)
    
    # Relationships
    messages = relationship("Message", back_populates="lead")
    interactions = relationship("Interaction", back_populates="lead")
    
    def __repr__(self):
        return f"<Lead(id={self.id}, email='{self.email}', intent={self.intent})>"


class Message(Base):
    """Follow-up messages (generated and sent)"""
    __tablename__ = 'messages'
    
    id = Column(Integer, primary_key=True)
    lead_id = Column(Integer, ForeignKey('leads.id'), nullable=False)
    
    # Message content
    subject = Column(String(500))
    body = Column(Text, nullable=False)
    channel = Column(SQLEnum(Channel), nullable=False)
    
    # Generation metadata
    model_version = Column(String(100))  # e.g., "claude-sonnet-4-5"
    prompt_template_id = Column(String(100))
    confidence_score = Column(Float)  # Model confidence in this message
    
    # RAG context used
    retrieved_docs = Column(JSON)  # List of doc IDs used for generation
    context_snippets = Column(JSON)  # Actual text snippets used
    
    # Status tracking
    status = Column(SQLEnum(MessageStatus), default=MessageStatus.GENERATED)
    human_edited = Column(Boolean, default=False)
    edit_count = Column(Integer, default=0)
    
    # Sending
    sent_at = Column(DateTime)
    sent_via = Column(String(100))  # e.g., "sendgrid", "twilio"
    external_id = Column(String(255))  # External service message ID
    
    # Engagement
    opened = Column(Boolean, default=False)
    opened_at = Column(DateTime)
    clicked = Column(Boolean, default=False)
    replied = Column(Boolean, default=False)
    reply_at = Column(DateTime)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    # Relationships
    lead = relationship("Lead", back_populates="messages")
    
    def __repr__(self):
        return f"<Message(id={self.id}, lead_id={self.lead_id}, status={self.status})>"


class KnowledgeBase(Base):
    """Business knowledge base for RAG"""
    __tablename__ = 'knowledge_base'
    
    id = Column(Integer, primary_key=True)
    
    # Document metadata
    title = Column(String(500))
    content = Column(Text, nullable=False)
    doc_type = Column(String(100))  # 'faq', 'product_page', 'case_study', etc.
    source_url = Column(String(1000))
    
    # Embeddings
    embedding_id = Column(String(255), unique=True)  # Vector DB reference
    
    # Metadata
    tags = Column(JSON)  # For filtering
    priority = Column(Integer, default=0)  # Higher = more important
    active = Column(Boolean, default=True)
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def __repr__(self):
        return f"<KnowledgeBase(id={self.id}, title='{self.title}')>"


class Interaction(Base):
    """Track all lead interactions for conversion analysis"""
    __tablename__ = 'interactions'
    
    id = Column(Integer, primary_key=True)
    lead_id = Column(Integer, ForeignKey('leads.id'), nullable=False)
    
    # Interaction details
    interaction_type = Column(String(100))  # 'message_sent', 'opened', 'clicked', 'replied', 'converted'
    channel = Column(SQLEnum(Channel))
    metadata = Column(JSON)
    
    # Timestamps
    occurred_at = Column(DateTime, default=datetime.utcnow, index=True)
    
    # Relationships
    lead = relationship("Lead", back_populates="interactions")
    
    def __repr__(self):
        return f"<Interaction(lead_id={self.lead_id}, type={self.interaction_type})>"


class Experiment(Base):
    """A/B testing experiments"""
    __tablename__ = 'experiments'
    
    id = Column(Integer, primary_key=True)
    
    # Experiment config
    name = Column(String(255), nullable=False)
    description = Column(Text)
    variant_a = Column(JSON)  # Config for variant A
    variant_b = Column(JSON)  # Config for variant B
    
    # Status
    active = Column(Boolean, default=True)
    start_date = Column(DateTime, default=datetime.utcnow)
    end_date = Column(DateTime)
    
    # Results
    results = Column(JSON)  # Stores metrics comparison
    winner = Column(String(10))  # 'a', 'b', or 'tie'
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    def __repr__(self):
        return f"<Experiment(id={self.id}, name='{self.name}', active={self.active})>"


class ModelLog(Base):
    """Audit trail for model outputs"""
    __tablename__ = 'model_logs'
    
    id = Column(Integer, primary_key=True)
    
    # Model info
    model_version = Column(String(100), nullable=False)
    operation = Column(String(100))  # 'classify', 'generate_message', etc.
    
    # Input/Output
    input_data = Column(JSON)
    output_data = Column(JSON)
    
    # Performance
    latency_ms = Column(Integer)
    tokens_used = Column(Integer)
    cost_usd = Column(Float)
    
    # Quality
    confidence_score = Column(Float)
    human_override = Column(Boolean, default=False)
    feedback_rating = Column(Integer)  # 1-5 if human rates it
    
    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow, index=True)
    
    def __repr__(self):
        return f"<ModelLog(id={self.id}, model={self.model_version}, op={self.operation})>"


# ============================================================================
# PYDANTIC SCHEMAS (API contracts)
# ============================================================================

class LeadCreate(BaseModel):
    """Input schema for creating a lead"""
    email: Optional[EmailStr] = None
    phone: Optional[str] = None
    name: str
    company: Optional[str] = None
    source: LeadSource
    source_metadata: Optional[Dict] = {}
    
    class Config:
        use_enum_values = True


class LeadClassification(BaseModel):
    """Output of RAG-based lead classification"""
    intent: LeadIntent
    confidence: float = Field(ge=0.0, le=1.0)
    reason: str
    retrieved_docs: List[str] = []
    
    class Config:
        use_enum_values = True


class MessageGeneration(BaseModel):
    """Output of message generation"""
    subject: Optional[str] = None
    body: str
    channel: Channel
    confidence: float = Field(ge=0.0, le=1.0)
    template_used: str
    context_used: List[str] = []
    
    class Config:
        use_enum_values = True


class MessageReview(BaseModel):
    """Admin review action"""
    message_id: int
    action: str  # 'approve', 'edit', 'reject'
    edited_body: Optional[str] = None
    edited_subject: Optional[str] = None


# ============================================================================
# DATABASE SETUP
# ============================================================================

def get_engine():
    """Get database engine"""
    database_url = os.getenv('DATABASE_URL', 'sqlite:///bizpilot.db')
    return create_engine(database_url, echo=False)


def init_db():
    """Initialize database tables"""
    engine = get_engine()
    Base.metadata.create_all(engine)
    print("✓ Database initialized")
    return engine


def get_session():
    """Get database session"""
    engine = get_engine()
    Session = sessionmaker(bind=engine)
    return Session()


if __name__ == "__main__":
    init_db()