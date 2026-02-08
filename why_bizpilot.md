# BizPilot - Quick Start Guide

Get BizPilot running locally in **15 minutes**.

---

## Prerequisites

- **Python 3.11+**
- **Docker & Docker Compose** (recommended) OR
- **Redis** + **PostgreSQL** (if running natively)

---

## Option 1: Docker (Recommended - 5 minutes)

### 1. Get API Keys (3 minutes)

You need **one** API key to get started:

**Anthropic Claude** (Required):
- Go to: https://console.anthropic.com/
- Sign up (free $5 credit)
- Create API key
- Copy key (starts with `sk-ant-...`)

**Optional** (can add later):
- SendGrid: https://signup.sendgrid.com/ (free 100 emails/day)
- OpenAI: https://platform.openai.com/ (better embeddings, costs ~$0.0001/lead)

### 2. Configure Environment (1 minute)

```bash
# Copy template
cp .env.template .env

# Edit with your API key
nano .env  # or use any text editor
```

Minimum config (only change this line):
```bash
ANTHROPIC_API_KEY=sk-ant-your-actual-key-here
```

### 3. Start Everything (1 minute)

```bash
docker-compose up -d
```

This starts:
- PostgreSQL database
- Redis queue
- FastAPI application (port 8000)
- Celery worker

### 4. Initialize Data (30 seconds)

```bash
# Initialize database and sample knowledge base
docker-compose exec api python models.py
docker-compose exec api python rag_pipeline.py
```

### 5. Test It (30 seconds)

```bash
# Check API is running
curl http://localhost:8000/health

# Should return: {"status": "healthy", "timestamp": "..."}
```

**Done!** 🎉 Jump to [Usage Examples](#usage-examples)

---

## Option 2: Local Development (15 minutes)

For native Python development without Docker.

### 1. Install Dependencies (5 minutes)

```bash
# Create virtual environment
python3 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install packages
pip install -r requirements.txt
```

### 2. Start Required Services (varies by OS)

**Redis**:
```bash
# macOS (with Homebrew)
brew install redis
redis-server

# Ubuntu/Debian
sudo apt install redis-server
sudo systemctl start redis

# Windows
# Download from: https://github.com/microsoftarchive/redis/releases
```

**PostgreSQL** (or use SQLite for simplicity):
```bash
# For SQLite (easier):
# Edit .env: DATABASE_URL=sqlite:///bizpilot.db

# For PostgreSQL:
# macOS: brew install postgresql
# Ubuntu: sudo apt install postgresql
# Create database: createdb bizpilot
```

### 3. Configure Environment

```bash
cp .env.template .env
nano .env
```

Set:
```bash
ANTHROPIC_API_KEY=sk-ant-your-key-here
DATABASE_URL=sqlite:///bizpilot.db  # or postgresql://...
REDIS_URL=redis://localhost:6379/0
```

### 4. Initialize Database

```bash
python models.py
python rag_pipeline.py
```

### 5. Start Services (3 terminals)

**Terminal 1 - API**:
```bash
source venv/bin/activate
uvicorn api:app --reload --host 0.0.0.0 --port 8000
```

**Terminal 2 - Worker**:
```bash
source venv/bin/activate
celery -A worker_tasks worker --loglevel=info
```

**Terminal 3 - Testing**:
```bash
curl http://localhost:8000/health
```

---

## Usage Examples

### Ingest a Lead

```bash
curl -X POST http://localhost:8000/api/leads \
  -H "Content-Type: application/json" \
  -d '{
    "source": "api",
    "name": "John Smith",
    "email": "john@company.com",
    "company": "Acme Corp",
    "message": "I want to schedule a demo ASAP. We need better lead management for our 20-person sales team."
  }'
```

**Response**:
```json
{
  "id": 1,
  "name": "John Smith",
  "email": "john@company.com",
  "source": "api",
  "intent": null,
  "created_at": "2026-02-09T10:00:00Z"
}
```

### Check Processing Status

Wait 2-3 seconds for async processing, then:

```bash
curl http://localhost:8000/api/leads/1
```

**Response**:
```json
{
  "id": 1,
  "name": "John Smith",
  "email": "john@company.com",
  "source": "api",
  "intent": "hot",
  "intent_confidence": 0.87,
  "created_at": "2026-02-09T10:00:00Z"
}
```

### View Generated Message

```bash
curl http://localhost:8000/api/messages?lead_id=1
```

**Response**:
```json
[
  {
    "id": 1,
    "lead_id": 1,
    "subject": "Demo for Acme Corp - Tomorrow?",
    "body": "Hi John,\n\nGreat timing! We help companies like Acme Corp automate lead management for sales teams.\n\nI have a slot available tomorrow at 2 PM EST for a 15-minute demo...",
    "channel": "email",
    "status": "generated",
    "confidence_score": 0.85,
    "created_at": "2026-02-09T10:00:05Z"
  }
]
```

### Review & Approve Message

```bash
curl -X POST http://localhost:8000/api/messages/1/review \
  -H "Content-Type: application/json" \
  -d '{
    "message_id": 1,
    "action": "approve"
  }'
```

**Response**:
```json
{
  "status": "success",
  "action": "approve"
}
```

This triggers sending via SendGrid (if configured) or dry-run mode.

### View Dashboard Metrics

```bash
curl http://localhost:8000/api/dashboard/metrics
```

**Response**:
```json
{
  "total_leads": 1,
  "leads_today": 1,
  "hot_leads": 1,
  "warm_leads": 0,
  "cold_leads": 0,
  "messages_sent": 1,
  "messages_pending_review": 0,
  "avg_response_time_minutes": 0.08,
  "conversion_rate": 0.0
}
```

---

## API Documentation

Open your browser to:

**http://localhost:8000/docs**

FastAPI provides auto-generated interactive documentation where you can test all endpoints.

---

## Common Issues

### "Connection refused" on Redis

**Problem**: Redis not running

**Fix**:
```bash
# Check Redis status
redis-cli ping  # Should return "PONG"

# Start Redis
# macOS: brew services start redis
# Linux: sudo systemctl start redis
```

### "No module named 'anthropic'"

**Problem**: Dependencies not installed

**Fix**:
```bash
pip install -r requirements.txt
```

### "ANTHROPIC_API_KEY not set"

**Problem**: Environment variables not loaded

**Fix**:
```bash
# Make sure .env file exists
cp .env.template .env

# Edit with your key
nano .env

# For Docker:
docker-compose down && docker-compose up -d

# For local:
# Restart the services after editing .env
```

### Worker not processing tasks

**Problem**: Celery worker not connected to Redis

**Fix**:
```bash
# Check worker logs
# Docker: docker-compose logs worker
# Local: check terminal running celery worker

# Should see:
# [INFO] Connected to redis://localhost:6379/0
# [INFO] Ready to accept tasks
```

### Database migrations needed

**Problem**: "relation does not exist" errors

**Fix**:
```bash
# Reinitialize database
python models.py

# Or with Docker:
docker-compose exec api python models.py
```

---

## Next Steps

### Test the Full Workflow

1. ✅ **Ingest leads** (via API, webhook, or CSV)
2. ✅ **Check classification** (hot/warm/cold intent)
3. ✅ **Review messages** (generated with RAG context)
4. ✅ **Approve & send** (via SendGrid/Twilio)
5. ✅ **View metrics** (dashboard & analytics)

### Customize for Your Use Case

**Add your own knowledge base**:
```python
from models import KnowledgeBase, get_session

session = get_session()

# Add your FAQs, product pages, etc.
doc = KnowledgeBase(
    title="My Product Features",
    content="BizPilot offers automated lead triage, multi-channel messaging...",
    doc_type="product_page"
)

session.add(doc)
session.commit()

# Re-index
from rag_pipeline import RAGPipeline
rag = RAGPipeline()
rag.index_knowledge_base(force_refresh=True)
```

**Configure webhooks** for your form provider:
- Typeform: Set webhook URL to `http://your-server:8000/webhooks/typeform`
- Google Forms: Use Zapier to POST to `/api/leads`
- HubSpot/Salesforce: Configure webhook to POST to `/api/leads`

**Enable auto-sending**:
```bash
# In .env
AUTO_SEND_ENABLED=true  # Messages with confidence >0.85 auto-send
```

### Deploy to Production

See `README.md` for:
- Docker deployment
- Kubernetes setup
- Monitoring with Prometheus
- Scaling strategies

---

## Demo Presentation

Ready to demo? See `DEMO_SCRIPT.md` for:
- 3-minute walkthrough
- Technical talking points
- Metrics to highlight
- Q&A prep

---

## Support

- **Full docs**: See `README.md`
- **Demo guide**: See `DEMO_SCRIPT.md`
- **API docs**: http://localhost:8000/docs
- **Logs**: 
  - Docker: `docker-compose logs -f api worker`
  - Local: Check terminal outputs

---

**You're all set!** 🚀

BizPilot is now running and ready to process leads with AI-powered classification and personalized follow-ups.

Test it with a few sample leads, review the generated messages, and see the magic happen.