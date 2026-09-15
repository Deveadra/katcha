FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md alembic.ini ./
COPY migrations ./migrations
COPY src ./src
RUN pip install --upgrade pip && pip install -e .

RUN useradd --create-home --uid 10001 katcha \
    && mkdir -p /tmp/katcha \
    && chown -R katcha:katcha /app /tmp/katcha
USER katcha

CMD ["uvicorn", "katcha.api.main:app", "--host", "0.0.0.0", "--port", "8000"]
