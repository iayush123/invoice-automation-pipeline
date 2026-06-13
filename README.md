# Event-Driven Invoice Automation Pipeline

A backend system I built to explore how LLMs can be wired into real business workflows — not as a chatbot, but as a silent processing engine that extracts, decides, and acts on documents autonomously.

The idea came from reading about how accounts-payable teams waste hours manually triaging invoices. I wanted to build something that actually solves that end-to-end, with production concerns like idempotency, async processing, and human oversight built in from the start — not bolted on later.

**Live demo:** `https://invoice-automation-pipeline-production.up.railway.app/docs`

---

## What it does

You upload an invoice (PDF or image). The system:

1. Hashes the file and rejects duplicates before doing anything else
2. Queues it for async processing (the API returns 202 immediately)
3. Runs it through a Vision LLM to extract vendor, amount, line items, due date
4. Applies a risk policy — auto-approve low-risk invoices, flag high-risk ones
5. Sends an interactive Slack message for flagged invoices with approve/reject buttons
6. Executes a (mocked) payment and writes every step to an immutable audit log

The interesting part is step 5 — the LangGraph graph literally pauses mid-execution, serializes its state to Postgres, and waits. When someone clicks a Slack button, the graph resumes from exactly where it stopped. This means the approval flow survives server restarts without any polling.

```
POST /invoices/upload  →  202  →  worker picks up job
                                        │
                              LLM extracts invoice data
                                        │
                                   risk policy
                                   /          \
                             auto-pay       Slack message
                                              │
                                         [paused in DB]
                                              │
                                       human clicks button
                                              │
                                         graph resumes
                                              │
                                        payment + audit log
```

---

## Why I built it this way

A few decisions I'm particularly happy with:

**Idempotency via SHA-256 hash** — I store a SHA-256 hash of the raw file bytes with a `UNIQUE` constraint in Postgres. If the same invoice comes in twice (even concurrently, even from different endpoints), the DB constraint fires and we return 409. No race conditions, no application-level check-then-insert. This one took me a while to get right — I initially tried application-level dedup and realized it had a race window under concurrent load.

**LangGraph interrupt for human-in-the-loop** — I could have used a simple webhook + database polling approach, but I wanted to explore LangGraph's checkpointing model. Storing graph state in Postgres means a paused approval doesn't live in RAM — it survives deploys. The resume is a single `ainvoke` call with the same `thread_id`. Much cleaner than I expected.

**Append-only audit log** — Every invoice status transition is a new INSERT. Never an UPDATE. You can replay the full history of any invoice and it's tamper-evident. In production you'd `GRANT INSERT` only on this table, so even if the app is compromised, past audit records can't be modified.

**arq over Celery** — arq is asyncio-native. Since everything in this stack is async (FastAPI, SQLAlchemy async, httpx), it made sense to stay in the same event loop rather than bring in Celery's threading model.

---

## Tech stack

- **FastAPI** + Pydantic v2 — API and validation
- **LangGraph** — stateful pipeline with interrupt/resume for human approval
- **PostgreSQL** + SQLAlchemy async + Alembic — data, checkpoints, migrations
- **arq** + Redis — async task queue with retries and dead-letter handling
- **OpenAI GPT-4o / Anthropic Claude** — vision LLM for invoice extraction
- **Slack Block Kit** — interactive approve/reject messages
- **LangSmith** — LLM call tracing and observability
- **Docker + Railway** — local dev and production deploy

---

## Project structure

```
app/
├── api/
│   ├── invoices.py       # upload, status, audit endpoints
│   └── slack.py          # Slack button callback + graph resume
├── core/
│   ├── config.py         # typed settings via pydantic-settings
│   ├── llm.py            # LLM client factory (swap Anthropic ↔ OpenAI via env)
│   └── tracing.py        # LangSmith wiring
├── db/base.py            # async SQLAlchemy engine + session factory
├── graph/
│   ├── state.py          # LangGraph TypedDict state schema
│   ├── nodes.py          # extract, decide, execute, notify
│   └── graph.py          # graph definition + Postgres checkpointer
├── models/invoice.py     # Invoice + InvoiceAuditLog ORM models
├── schemas/invoice.py    # Pydantic v2 request/response + extraction schemas
├── services/
│   ├── ingestion.py      # SHA-256 hash, dedup, file persistence
│   ├── extraction.py     # LLM extraction with retry loop
│   ├── slack.py          # send/update Slack approval messages
│   └── audit.py          # centralised audit log writes
└── worker.py             # arq worker + process_invoice task

alembic/versions/         # DB migrations
tests/
├── test_ingestion.py     # integration tests
└── evals/
    └── test_extraction_eval.py  # field accuracy eval over 30 synthetic invoices
scripts/
└── generate_synthetic_invoices.py
```

---

## Running locally

```bash
# Start Postgres and Redis
docker-compose up db redis -d

# Set up Python environment
python -m venv .venv
source .venv/bin/activate  # Windows: .venv\Scripts\activate
pip install -r requirements.txt

# Configure — copy .env.example and fill in at minimum:
# DATABASE_URL, DATABASE_URL_SYNC, REDIS_URL, OPENAI_API_KEY or ANTHROPIC_API_KEY
cp .env.example .env

# Run migrations
alembic upgrade head

# Terminal 1 — API
uvicorn app.main:app --reload --port 8000

# Terminal 2 — Worker
python -m arq app.worker.WorkerSettings
```

Open `http://localhost:8000/docs`

---

## API

| Method | Endpoint | Description |
|--------|----------|-------------|
| `POST` | `/invoices/upload` | Upload PDF or image invoice |
| `POST` | `/invoices/webhook/email` | Email-service webhook (base64 attachment) |
| `GET` | `/invoices/{id}` | Status + extracted data |
| `GET` | `/invoices/{id}/audit` | Full audit trail |
| `POST` | `/slack/actions` | Slack approve/reject callback |
| `GET` | `/health` | Health check |

### Quick test

```bash
# Upload an invoice
curl -X POST https://invoice-automation-pipeline-production.up.railway.app/invoices/upload \
  -F "file=@invoice.pdf"
# → 202 with invoice_id

# Upload the same file again
curl -X POST https://invoice-automation-pipeline-production.up.railway.app/invoices/upload \
  -F "file=@invoice.pdf"
# → 409 Conflict (idempotency working)

# Check audit trail
curl https://invoice-automation-pipeline-production.up.railway.app/invoices/{id}/audit
```

---

## Risk policy

Applied in order, configurable via environment variables without redeploying:

1. Extraction failed → `reject`
2. Amount exceeds `RISK_AMOUNT_THRESHOLD` (default $10,000) → `needs_review`
3. Vendor not seen before → `needs_review`
4. Missing PO number on invoices over $500 → `needs_review`
5. Everything passes → `auto_approve` → payment executes immediately

---

## Environment variables

| Variable | Required | Notes |
|----------|----------|-------|
| `DATABASE_URL` | ✅ | asyncpg driver |
| `DATABASE_URL_SYNC` | ✅ | psycopg2, used by Alembic |
| `REDIS_URL` | ✅ | arq task queue |
| `OPENAI_API_KEY` | ✅* | or use Anthropic |
| `ANTHROPIC_API_KEY` | ✅* | or use OpenAI |
| `LLM_PROVIDER` | ✅ | `openai` or `anthropic` |
| `LLM_MODEL` | ✅ | e.g. `gpt-4o` or `claude-opus-4-8` |
| `SLACK_BOT_TOKEN` | ❌ | optional — enables real Slack notifications |
| `SLACK_SIGNING_SECRET` | ❌ | optional — Slack request verification |
| `LANGCHAIN_API_KEY` | ❌ | optional — LangSmith tracing |
| `RISK_AMOUNT_THRESHOLD` | ❌ | default `10000.00` |

---

## Tests

```bash
# Integration tests (needs Postgres)
pytest tests/test_ingestion.py -v

# Risk decision accuracy — no LLM or DB needed, runs in CI
pytest tests/evals/test_extraction_eval.py::test_risk_decision_accuracy -v

# Schema validation unit tests
pytest tests/evals/test_extraction_eval.py::test_pydantic_extraction_schema_validation -v

# Full extraction eval (generate fixtures first)
python scripts/generate_synthetic_invoices.py
pytest tests/evals/ -v
```

---

## Deploy

Deployed on Railway. Both `railway.toml` and `fly.toml` are included. The start command runs `alembic upgrade head` before starting the server, so migrations apply automatically on deploy.

```bash
# Railway
railway login && railway up

# Fly.io
fly launch && fly deploy
```

---

## What I'd add next

- Real payment API integration (Stripe or Plaid) instead of the mock
- Multi-page PDF handling with page-by-page extraction
- A simple dashboard to view invoice status without curl
- Confidence scores from the LLM to inform the risk policy

---

## License

MIT
