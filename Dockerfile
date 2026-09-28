# syntax=docker/dockerfile:1.7
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Third-party dependencies are keyed only by pyproject.toml. Normal Katcha source
# edits therefore reuse this expensive layer instead of reinstalling dependencies.
COPY pyproject.toml ./
RUN --mount=type=cache,target=/root/.cache/pip \
    python -c 'import subprocess,tomllib; data=tomllib.load(open("pyproject.toml","rb")); deps=data["project"]["dependencies"]; subprocess.check_call(["python","-m","pip","install",*deps])'

# Install the Katcha package itself only after dependencies are cached.
COPY README.md alembic.ini ./
COPY migrations ./migrations
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/pip \
    python -m pip install --no-deps -e .

RUN useradd --create-home --uid 10001 katcha \
    && mkdir -p /tmp/katcha \
    && chown -R katcha:katcha /app /tmp/katcha
USER katcha

CMD ["uvicorn","katcha.api.main:app","--host","0.0.0.0","--port","8000"]
