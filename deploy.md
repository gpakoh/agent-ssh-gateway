# agent-ssh-gateway — Deployment Guide

> Real infrastructure details (domains, IPs, internal architecture) are kept
> in a private repository. This file contains a generic deployment walkthrough.
> See `deploy.example.md` for a public-friendly version.

## Overview

Deploy agent-ssh-gateway behind an Nginx reverse proxy with optional SSO/mTLS.
The gateway runs as a Docker container with Redis for session and token storage.

## Prerequisites

- Docker host with Docker Compose
- Nginx reverse proxy (separate host or same)
- SSL certificate for your domain (certbot / ACME)
- Optional: Authelia or another SSO provider
- Optional: mTLS CA certificate for agent authentication

## Quick Start

```bash
# 1. Clone the repository
git clone <repo-url> agent-ssh-gateway
cd agent-ssh-gateway

# 2. Configure environment
cp .env.example .env
# Edit .env with your secrets

# 3. Configure target networks
# See docker-compose.yml — create the required Docker networks
# for your infrastructure.

# 4. Build and start
docker compose -f docker/docker-compose.yml up -d --build

# 5. Verify
curl http://localhost:8085/health
```

## Nginx Configuration

See `nginx-gateway.conf.example` for SSL + mTLS configuration template.
Real deploy configs use CI template injection with placeholders replaced.

## CI/CD

The `.gitea/workflows/deploy.yml` workflow:
1. Runs tests
2. Replaces template placeholders (`__DOMAIN__`, `__BACKEND_IP__`, `__API_KEY__`)
3. Copies nginx config to the proxy host
4. Deploys the Docker stack to the backend host

Required Gitea variables:
- `NGINX_HOST` — Nginx proxy host IP
- `BACKEND_HOST` — Backend Docker host IP
- `DOMAIN` — Your domain (e.g., `gateway.example.com`)
- `API_KEY` — (as secret) API authentication key
