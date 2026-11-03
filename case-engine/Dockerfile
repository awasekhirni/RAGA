# C.A.S.E. engine — serving image (G16).
#
# Build:  docker build -t case-engine .
# Run:    docker run -p 8000:8000 -v case-audit:/data case-engine
#
# The image serves the FastAPI layer (case_engine/server.py) with the
# mock provider by default — swap provider/keys via env or a mounted
# config (see docker-compose.yml). Audit ledger and result store live
# on the /data volume so evidence survives container restarts.

FROM python:3.12-slim AS base

LABEL org.opencontainers.image.title="case-engine" \
      org.opencontainers.image.description="Deterministic, schema-gated mental-model reasoning (audit-chained, fail-closed)" \
      org.opencontainers.image.licenses="Apache-2.0 OR Commercial"

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    CASE_LEDGER_PATH=/data/case_audit.jsonl \
    CASE_RESULT_STORE_PATH=/data/case_results.sqlite3

WORKDIR /app

# Build stage deps only what the server extra needs; the engine itself
# has pydantic as its single hard dependency.
COPY pyproject.toml README.md LICENSE ./
COPY case_engine ./case_engine
RUN pip install --no-cache-dir ".[server]"

# Non-root runtime user; /data is the evidence volume.
RUN useradd --create-home --uid 10001 case && mkdir -p /data && chown case /data
USER case
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=3s --retries=3 \
  CMD python -c "import urllib.request,sys; \
sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"

# --factory: create_app() is called by uvicorn at startup (lazy FastAPI).
CMD ["uvicorn", "--factory", "case_engine.server:create_app", "--host", "0.0.0.0", "--port", "8000"]
