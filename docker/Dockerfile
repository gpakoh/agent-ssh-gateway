# agent-ssh-gateway — Hardened Docker Image
FROM python:3.12-slim
LABEL description="agent-ssh-gateway — SSH control plane"

ARG BUILD_SHA
ARG BUILD_TIME

ENV BUILD_SHA=${BUILD_SHA}
ENV BUILD_TIME=${BUILD_TIME}

# Security: Install only necessary packages
RUN apt-get update && apt-get install -y --no-install-recommends \
    openssh-client \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

# Security: Create non-root user with restricted shell
RUN groupadd -r -g 1000 appuser && \
    useradd -r -u 1000 -g appuser -d /app -s /sbin/nologin appuser

# Set working directory
WORKDIR /app

# Security: Install Python dependencies from frozen lockfile
COPY pyproject.toml uv.lock ./
RUN python -m pip install --default-timeout=120 --no-cache-dir uv && \
    uv sync --frozen --no-dev --no-install-project && \
    rm -rf /root/.cache/pip /root/.cache/uv && \
    chmod -R 755 /app/.venv
ENV PATH="/app/.venv/bin:$PATH"

# Security: Copy application with proper ownership
COPY --chown=appuser:appuser app/ ./app/

# Copy SDK for download endpoint
COPY --chown=appuser:appuser sdk/ ./sdk/

# Copy Alembic for database migrations
COPY --chown=appuser:appuser alembic.ini ./alembic.ini
COPY --chown=appuser:appuser alembic/ ./alembic/

# Copy the authenticated black-box smoke check deploy-from-registry.sh
# runs (via `docker exec`) after a real deploy -- P1 BLOCKER audit
# finding: the only prior post-deploy check was this image's own
# HEALTHCHECK below, proving process readiness, not that the real
# authenticated API surface works. Only this one script, not the whole
# scripts/ directory -- most of it is MCP-specific and not meaningful
# (or importable, given this image's leaner dependency set) here.
COPY --chown=appuser:appuser scripts/gateway_black_box_smoke.py ./scripts/gateway_black_box_smoke.py

# Security: Create necessary directories
RUN mkdir -p /app/ssh_keys /app/logs /app/data /tmp && \
    chown -R appuser:appuser /app /tmp

# Security: Set restrictive permissions
RUN chmod -R 755 /app && \
    chmod 700 /app/ssh_keys

# Switch to non-root user
USER appuser

# Security: Read-only filesystem (except /tmp and /app/logs)
VOLUME ["/tmp", "/app/logs", "/app/data"]

# Health check
# /health always answers HTTP 200 (its body carries status/ready for callers
# to interpret) -- a plain urlopen() only proves the process accepted a
# connection, not that Redis/Postgres/SSH are actually working, so a
# deployment could sit "healthy" while genuinely degraded. Parse the body
# and only pass when status == "ok" (M17).
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import json, sys, urllib.request; d = json.load(urllib.request.urlopen('http://localhost:8085/health', timeout=4)); sys.exit(0 if d.get('status') == 'ok' else 1)"

# Expose port
EXPOSE 8085

# Security: Drop all capabilities and run
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8085", "--proxy-headers"]
