# 🧾 Event-Driven Invoice Automation Pipeline

An autonomous, event-driven accounts-payable pipeline that ingests invoices, extracts structured data with a vision LLM, applies a risk policy, routes risky invoices to a human via Slack, executes a payment, and writes an immutable audit log — all without a chat interface.

Built with **FastAPI · LangGraph · PostgreSQL · arq · Slack · LangSmith**.

---

## Demo

```
POST /invoices/upload  →  202 Accepted  →  LLM extracts data  →  risk check
                                                                      │
                                              ┌───────────────────────┤
                                              ▼                       ▼
                                         auto-pay              Slack approval
                                              │                  (interrupt)
                                              └──────── resume on click ──→ pay
                                                                            │
                                                                     audit log
```

Upload the same invoice twice → **409 Conflict** (idempotency guarantee).

---

## Features

| Feature | How it works |
|---|---|
| **Idempotency** | SHA-256 content hash + DB `UNIQUE` constraint — duplicate invoices rejected even under concurrent load |
| **LLM extraction** | Vision-capable model (claude-opus-4-8 / GPT-4o) parses PDF → validated Pydantic model, with self-correcting retry loop |
| **Risk policy** | Configurable rules: amount threshold, new vendor, missing PO — no redeploy to change thresholds |
| **Human-in-the-loop** | LangGraph `interrupt()` + Postgres checkpointer — paused invoices survive process restarts |
| **Slack approvals** | Interactive approve/reject buttons; graph resumes on click |
| **Retries + dead-letter** | arq task queue, 3 retries with backoff, explicit dead-letter status |
| **Append-only audit log** | Every state transition = a new INSERT, never an UPDATE |
| **Observability** | LangSmith traces every LLM call; structlog for structured app logs |
| **Eval harness** | pytest accuracy measurement over 30 synthetic labeled invoices |

---

## Tech Stack

- **Python 3.12**
- **FastAPI** + Pydantic v2 — API layer and schema validation
- **LangGraph** — stateful pipeline with interrupt/resume
- **LangSmith** — LLM tracing and observability
- **PostgreSQL** + SQLAlchemy (async) + Alembic — data + checkpoints + migrations
- **arq** + Redis — async task queue, retries, dead-letter
- **Anthropic claude-opus-4-8** (or OpenAI GPT-4o) — vision invoice extraction
- **Slack Block Kit** — human approval interface
- **Docker + docker-compose** — local dev
- **GitHub Actions** — CI
- **Railway / Fly.io** — deploy targets

---

## Architecture

```
Inbound Invoice (PDF / Image)
        │
        ▼
FastAPI Endpoint
  ├── Computes SHA-256 content hash
  ├── Rejects duplicates (DB UNIQUE constraint) → 409
  └── Persists raw file + metadata → 202
        │
        ▼
arq Worker (async, retries × 3, dead-letter)
        │
        ▼
LangGraph Pipeline
  ┌─────────────────────────────────────────────┐
  │                                             │
  │  [extract]  Vision LLM → InvoiceExtracted   │
  │      │       (retry + self-correction loop) │
  │      ▼                                      │
  │  [decide]   Risk policy                     │
  │      │       amount > threshold?            │
  │      │       new vendor?                    │
  │      │       missing PO?                    │
  │      │                                      │
  │  ┌───┴──────────────┐                       │
  │  ▼                  ▼                       │
  │ [execute]       [notify]                    │
  │  │           Slack message                  │
  │  │               │                          │
  │  │         [interrupt] ← serialised to DB   │
  │  │               │      survives restart    │
  │  │         human clicks                     │
  │  │               │                          │
  │  │         [resume] → approve/reject        │
  │  │               │                          │
  │  └───────────────┘                          │
  │                                             │
  │  [execute]  Mocked payment + ledger entry   │
  └─────────────────────────────────────────────┘
        │
        ▼
Immutable Audit Log (append-only INSERT)
```

---

## Project Structure

```
├── app/
│   ├── api/
│   │   ├── invoices.py       # Upload, status, audit endpoints
│   │   └── slack.py          # Slack interactive action callback
│   ├── core/
│   │   ├── config.py         # pydantic-settings — all env vars typed
│   │   ├── llm.py            # LLM client factory (Anthropic / OpenAI)
│   │   └── tracing.py        # LangSmith setup
│   ├── db/
│   │   └── base.py           # Async SQLAlchemy engine + session
│   ├── graph/
│   │   ├── state.py          # LangGraph TypedDict state
│   │   ├── nodes.py          # extract, decide, execute, notify nodes
│   │   └── graph.py          # Graph definition + Postgres checkpointer
│   ├── models/
│   │   └── invoice.py        # Invoice + InvoiceAuditLog ORM models
│   ├── schemas/
│   │   └── invoice.py        # Pydantic v2 API + extraction schemas
│   ├── services/
│   │   ├── ingestion.py      # SHA-256 hash, file persist, idempotency
│   │   ├── extraction.py     # LLM vision extraction + retry loop
│   │   ├── slack.py          # Send / update Slack approval messages
│   │   └── audit.py          # Centralised audit log writes
│   ├── worker.py             # arq worker settings + process_invoice task
│   └── main.py               # FastAPI app factory
├── alembic/                  # Database migrations
│   └── versions/
│       └── 0001_initial_schema.py
├── tests/
│   ├── test_ingestion.py     # Integration tests (idempotency, upload, audit)
│   └── evals/
│       └── test_extraction_eval.py  # Accuracy eval harness
├── scripts/
│   └── generate_synthetic_invoices.py  # Generate 30 labeled PDF fixtures
├── .github/workflows/ci.yml  # GitHub Actions CI
├── docker-compose.yml
├── Dockerfile
├── railway.toml              # Railway deploy config
├── fly.toml                  # Fly.io deploy config
└── .env.example
```

---

## Quick Start

### Option A — Docker (recommended)

```bash
git clone https://github.com/your-username/invoice-automation-pipeline.git
cd invoice-automation-pipeline

cp .env.example .env
# Edit .env — fill in ANTHROPIC_API_KEY at minimum

docker-compose up -d
```

App is live at `http://localhost:8000/docs`

### Option B — Local (VS Code)

```bash
# 1. Start only the infrastructure
docker-compose up db redis -d

# 2. Create virtual environment
python -m venv .venv
.venv\Scripts\activate        # Windows
# source .venv/bin/activate   # Mac/Linux

pip install -r requirements.txt

# 3. Configure environment
cp .env.example .env
# Edit .env:
#   DATABASE_URL=postgresql+asyncpg://invoice:invoice@localhost:5432/invoice_db
#   DATABASE_URL_SYNC=postgresql+psycopg2://invoice:invoice@localhost:5432/invoice_db
#   REDIS_URL=redis://localhost:6379/0
#   ANTHROPIC_API_KEY=sk-ant-...

# 4. Run migrations
alembic upgrade head

# 5. Start the app (Terminal 1)
uvicorn app.main:app --reload --port 8000

# 6. Start the worker (Terminal 2)
python -m arq app.worker.WorkerSettings
```

---

## API Endpoints

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/invoices/upload` | Upload invoice PDF/image |
| `POST` | `/invoices/webhook/email` | Simulated email-webhook ingestion |
| `GET` | `/invoices/{id}` | Status + extracted data |
| `GET` | `/invoices/{id}/audit` | Full immutable audit trail |
| `POST` | `/slack/actions` | Slack approve/reject callback |
| `GET` | `/health` | Health check |
| `GET` | `/docs` | Swagger UI |

### Example — upload an invoice

```bash
curl -X POST http://localhost:8000/invoices/upload \
  -F "file=@invoice.pdf"

# Response 202
{
  "invoice_id": "3f7a1c2e-...",
  "content_hash": "a3f9...",
  "status": "received",
  "message": "Invoice received. Extraction will begin shortly."
}

# Upload the same file again → 409
{
  "detail": {
    "message": "Duplicate invoice — this file has already been submitted.",
    "existing_invoice_id": "3f7a1c2e-..."
  }
}
```

### Example — check audit trail

```bash
curl http://localhost:8000/invoices/3f7a1c2e-.../audit

[
  { "event": "invoice_received",   "from_status": null,        "to_status": "received"  },
  { "event": "extraction_started", "from_status": "received",  "to_status": "extracting" },
  { "event": "payment_executed",   "from_status": "executing", "to_status": "paid",
    "extra_data": { "payment_reference": "PAY-A1B2C3D4E5F6" } }
]
```

---

## Environment Variables

Copy `.env.example` to `.env` and fill in:

| Variable | Required | Description |
|---|---|---|
| `DATABASE_URL` | ✅ | Async PostgreSQL URL (asyncpg driver) |
| `DATABASE_URL_SYNC` | ✅ | Sync PostgreSQL URL (for Alembic) |
| `REDIS_URL` | ✅ | Redis URL for arq task queue |
| `ANTHROPIC_API_KEY` | ✅* | Anthropic API key (*or OpenAI) |
| `OPENAI_API_KEY` | ✅* | OpenAI API key (*or Anthropic) |
| `LLM_PROVIDER` | ✅ | `anthropic` or `openai` |
| `LLM_MODEL` | ✅ | Model string — see links below |
| `SLACK_BOT_TOKEN` | ❌ | Optional — enables real Slack approvals |
| `SLACK_SIGNING_SECRET` | ❌ | Optional — Slack request verification |
| `SLACK_APPROVAL_CHANNEL` | ❌ | e.g. `#invoice-approvals` |
| `LANGCHAIN_API_KEY` | ❌ | Optional — enables LangSmith tracing |
| `RISK_AMOUNT_THRESHOLD` | ❌ | Default `10000.00` — invoices above this need review |

> Always verify the latest model string before deploying:
> - Anthropic: https://docs.anthropic.com/en/docs/about-claude/models
> - OpenAI: https://platform.openai.com/docs/models

---

## Running Tests

```bash
# Integration tests (requires Postgres running)
pytest tests/test_ingestion.py -v

# Decision accuracy eval — no LLM needed, runs in CI
pytest tests/evals/test_extraction_eval.py::test_risk_decision_accuracy -v

# Schema validation unit tests — no DB or LLM needed
pytest tests/evals/test_extraction_eval.py::test_pydantic_extraction_schema_validation -v

# Full LLM extraction eval (generate fixtures first)
python scripts/generate_synthetic_invoices.py
pytest tests/evals/ -v
```

---

## Deploy

### Railway

```bash
railway login
railway new
railway up
# Set all .env variables in the Railway dashboard under Variables
```

### Fly.io

```bash
fly launch
fly secrets set ANTHROPIC_API_KEY=sk-ant-...
fly deploy
```

Both configs (`railway.toml`, `fly.toml`) are included. The app runs `alembic upgrade head` automatically on startup.

---

## Risk Policy

Rules applied in order (configurable via `.env`, no redeploy needed):

1. **Extraction failed** → `reject`
2. **Amount > `RISK_AMOUNT_THRESHOLD`** → `needs_review` (Slack approval)
3. **New vendor** → `needs_review`
4. **Missing PO number** on invoices over $500 → `needs_review`
5. All checks pass → `auto_approve` → execute immediately

---

## Key Design Decisions

**Why SHA-256 content hash for idempotency?**
The DB `UNIQUE` constraint fires atomically even under concurrent requests. Application-level dedup (check-then-insert) has a race condition window. The hash also means a renamed duplicate file is still caught.

**Why LangGraph `interrupt` instead of polling?**
State is serialised to Postgres, not RAM. A paused invoice survives a process crash or deploy. The resume is a single `ainvoke` call with the same `thread_id` — no polling loop, no lost approvals.

**Why append-only audit log?**
Any mutable log can be silently edited to hide mistakes. An insert-only table with a DB-level `GRANT INSERT` makes the audit trail tamper-evident. Every state in the invoice's history is always queryable.

**Why arq over Celery?**
arq is asyncio-native — no thread pool overhead, same event loop as the FastAPI app. For this workload (async everywhere, moderate throughput) it's simpler and faster. Celery would be appropriate if you needed cross-language workers or complex workflow DAGs.

---

## CI

GitHub Actions runs on every push to `main`:
- Spins up Postgres + Redis services
- Installs dependencies
- Runs `alembic upgrade head`
- Runs integration tests (excluding LLM eval)
- Runs decision accuracy eval (no API key needed)

See `.github/workflows/ci.yml`.

---

## License

MIT
