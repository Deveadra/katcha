# Katcha

Katcha is a standalone, automation-first video intelligence, production, publishing, analytics, and channel-learning system.

It turns canonical source clips into Shorts and long-form compilations, publishes through durable YouTube workflows, measures real outcomes, and feeds those outcomes back into channel-specific ranking, scheduling, budget, and automation policy.

Katcha is deliberately independent from Aerith. Aerith or another controller can operate it through versioned HTTP APIs and cursor-based domain events; Katcha does not require Aerith to run.

## Current system

Katcha now includes:

- URL ingestion, ffprobe inspection, SHA-256 canonicalization, and S3-compatible storage;
- local transcription, keyframes/contact sheets, perceptual similarity, and routed vision analysis;
- deterministic clip scoring and channel-specific learned ranking;
- AI-hosted Short production with stable TTS voice, captions, Remotion rendering, and human review;
- performance-aware long-form compilation with deterministic sequencing, editor/critic passes, 16:9 reconstruction, and review/regeneration;
- encrypted YouTube OAuth, resumable upload/scheduling, analytics, retention, and revenue snapshots;
- per-channel strategy, base/hard budgets, capped reinvestment, spend reservations, burn-rate telemetry, and budget-aware model/TTS routing;
- publish-window recommendations with sample size/confidence and fallback/blackout support;
- evidence-gated automation promotion plus automatic safety demotion;
- a dedicated Temporal intelligence refresh worker;
- Aerith-safe event consumption with secret scrubbing and channel-bound cursors.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full contracts and safety model.

## Architecture

- **FastAPI** — versioned control plane
- **Temporal** — durable ingest, analysis, production, long-form, publishing, analytics, and intelligence workflows
- **PostgreSQL + Alembic** — lineage, policy versions, observations, economics, costs, and events
- **S3-compatible storage** — MinIO locally; R2/S3-compatible deployment
- **yt-dlp + FFmpeg/ffprobe** — media acquisition/inspection
- **Remotion** — Short and long-form rendering
- **OpenAI/Gemini routing** — task/channel policy, quality floors, spend reservations, and usage accounting
- **Transactional domain events** — external control without runtime coupling

## Quick start

1. Copy `.env.example` to `.env`.
2. Run the core stack:

```bash
docker compose up --build
```

3. To start the complete local backend, including discovery, trend, and intelligence workers:

```bash
docker compose \
  -f docker-compose.yml \
  -f docker-compose.discovery.yml \
  -f docker-compose.trends.yml \
  -f docker-compose.intelligence.yml \
  up --build
```

Katcha builds its local MinIO server from the pinned upstream source release in
`Dockerfile.minio` instead of depending on retired public MinIO container images.
Override the pinned source version only when needed with `KATCHA_MINIO_VERSION`.
Bucket initialization is performed by Katcha itself through the S3 API, so no
separate MinIO client image is required.

4. API docs: `http://localhost:8000/docs`
5. Temporal UI: `http://localhost:8080`
6. MinIO console: `http://localhost:9001`
7. Trend explorer: `http://localhost:8000/explorer`
8. Editing control center: `http://localhost:8000/editing`

The editing control center uses the same control-plane token and channel-scoped APIs as
the Trend Explorer. It shows versioned editing blueprints, measured performance,
recent ranked episodes, render attempts, and dead-letter recovery. Its staged-brand
preview lab can render an inactive brand version against an existing frozen ranked
episode or standalone Production, stream the verified preview privately for visual
inspection, and activate the candidate only as a separate explicit operator action. Blueprint or brand activation
changes future work only; existing productions keep their stored snapshots.

See [`docs/TREND_EXPLORER.md`](docs/TREND_EXPLORER.md) for private access,
shared AI-controller APIs, and evidence-grounded editorial handoff.

Submit a source URL:

```bash
curl -X POST http://localhost:8000/v1/clips/ingest \
  -H 'content-type: application/json' \
  -d '{"url":"https://example.com/video"}'
```

After connecting a YouTube channel, create a channel profile through `POST /v1/channels`. Channel-scoped production endpoints then preserve budget, learning, review, and publication isolation from the first editorial action onward. Before using launch media, follow [`docs/LIVE_ACCEPTANCE.md`](docs/LIVE_ACCEPTANCE.md) for the private synthetic end-to-end acceptance. See [`docs/YOUTUBE.md`](docs/YOUTUBE.md) for the authenticated local OAuth/bootstrap sequence.

For the first live channel acceptance, [`docs/RANKSNAXX_ACCEPTANCE.md`](docs/RANKSNAXX_ACCEPTANCE.md) provides a repeatable five-clip synthetic flow through discovery, rights gating, AI editorial, rendering, and an explicitly approved **private-only** YouTube upload.

## Budget safety

`KATCHA_AI_BUDGET_USD_MONTHLY` is the **deployment-wide emergency ceiling**.

Each channel separately has a versioned:

- base monthly allocation;
- absolute hard monthly ceiling;
- reinvestment rate and cap;
- routing policy/quality floors.

Earned reinvestment can raise a channel's active allocation only up to its hard ceiling. Paid channel calls reserve estimated headroom transactionally before provider execution, so concurrent workers cannot spend the same remaining budget.

## Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -e '.[dev]'
ruff check src tests
python -m compileall -q src
pytest -q
uvicorn katcha.api.main:app --reload
```

FFmpeg/ffprobe must be installed on the host for media inspection. The Docker image includes them.

## Design rules

- No LLM for deterministic ranking math, scheduling statistics, hashing, dedupe, probing, or state transitions.
- Every workflow side effect must be retry-safe/idempotent.
- Ambiguous paid calls fail visibly rather than silently retrying and risking duplicate charges.
- Every paid model/TTS response is cost-accounted.
- Channel-scoped paid calls reserve budget before execution.
- Learned ranking cannot overpower the deterministic baseline when evidence is sparse or validation is poor.
- Raw canonical media can be shared; channel budgets, strategy, outcomes, and editorial state cannot.
- Automation starts at human review and advances only through explicit evidence-gated promotion.
- Phase 5 does not silently enable automatic public publishing.
- Aerith integration happens only through stable APIs/events, never shared process state or secret-bearing payloads.
