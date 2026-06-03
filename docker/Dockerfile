# agent-ssh-gateway — Hardened Docker Image
FROM python:3.12-slim
LABEL description="agent-ssh-gateway — SSH control plane"

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

# Security: Install Python dependencies
COPY docker/requirements.txt .
RUN pip install --default-timeout=120 --no-cache-dir -r requirements.txt && \
    rm -rf /root/.cache/pip

# Security: Copy application with proper ownership
COPY --chown=appuser:appuser app/ ./app/

# Copy SDK for download endpoint
COPY --chown=appuser:appuser sdk/ ./sdk/

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
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8085/health')" || exit 1

# Expose port
EXPOSE 8085

# Security: Drop all capabilities and run
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8085", "--proxy-headers"]
