# Katcha

Katcha is a standalone, automation-first media intelligence and production system for discovering, ingesting, understanding, producing, publishing, and learning from video content.

Katcha is deliberately independent from Aerith. Aerith will later control Katcha through versioned APIs and emitted domain events; Katcha does not require Aerith to operate.

## Foundation status

The first vertical slice is wired end to end:

`POST /v1/clips/ingest` → Temporal workflow → yt-dlp download → SHA-256 dedupe → S3-compatible object storage → PostgreSQL reconciliation → clip/source records.

This is the spine that later stages (vision, scoring, scripting, voice, rendering, review, YouTube publishing, analytics, and learning) plug into.

## Architecture

- **FastAPI** — control plane/API
- **Temporal** — durable workflows, retries, recovery, orchestration
- **PostgreSQL** — source, clip, workflow, event, and cost metadata
- **S3-compatible storage** — MinIO locally; R2/S3 in deployment
- **yt-dlp + FFmpeg/ffprobe** — media acquisition and inspection
- **Provider-routed AI layer** — task-based OpenAI/Gemini routing with cost telemetry
- **Transactional outbox** — future Aerith/event integrations without runtime coupling

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Quick start

1. Copy `.env.example` to `.env`.
2. Run `docker compose up --build`.
3. API docs: `http://localhost:8000/docs`
4. Temporal UI: `http://localhost:8080`
5. MinIO console: `http://localhost:9001`

Submit a URL:

```bash
curl -X POST http://localhost:8000/v1/clips/ingest \
  -H 'content-type: application/json' \
  -d '{"url":"https://example.com/video"}'
```

Then poll the returned source/clip identifiers through the API.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
pytest
uvicorn katcha.api.main:app --reload
python -m katcha.orchestration.worker
```

FFmpeg/ffprobe must be installed on the host for media inspection. The Docker image includes them.

## Design rules

- No LLM for deterministic work such as hashing, dedupe, probing, or state transitions.
- Every workflow activity must be retry-safe/idempotent.
- Provider-specific AI code stays behind the model router.
- Raw media is content-addressed by SHA-256.
- API contracts are versioned from the beginning.
- Aerith integration happens through APIs/events, not shared process state.
- Costs and model usage are first-class data, not logs we hope to reconstruct later.
