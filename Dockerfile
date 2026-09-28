# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app/src

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Third-party dependencies are keyed only by pyproject.toml. Katcha source changes
# therefore reuse this expensive layer instead of reinstalling dependencies.
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c 'import subprocess,tomllib; data=tomllib.load(open("pyproject.toml","rb")); deps=data["project"]["dependencies"]; subprocess.check_call(["python","-m","pip","install",*deps])'

RUN useradd --create-home --uid 10001 katcha \
    && mkdir -p /tmp/katcha \
    && chown -R katcha:katcha /tmp/katcha

# Runtime code is imported directly from /app/src. These COPY-only layers are the
# only layers invalidated by normal application edits.
COPY --chown=katcha:katcha alembic.ini ./
COPY --chown=katcha:katcha migrations ./migrations
COPY --chown=katcha:katcha src ./src

USER katcha
CMD ["uvicorn","katcha.api.main:app","--host","0.0.0.0","--port","8000"]
