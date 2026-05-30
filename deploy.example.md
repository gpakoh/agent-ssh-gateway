# Web SSH Gateway — Deployment Guide (Example)

## Overview
Deploy Web SSH Gateway behind an Nginx reverse proxy with optional SSO/mTLS.

> Replace all `example.com`, `10.0.0.x`, and `192.168.1.x` values with your actual infrastructure.

---

## Architecture

```
[Client] → Nginx (:443, SSL)
    → Authelia / SSO auth check (optional)
        → Proxy Pass → 10.0.0.10:8085 (Docker host)
            → FastAPI + Paramiko → SSH Target Server
```

---

## Step 1: Prepare Docker Host

```bash
# Create required Docker networks
docker network create --driver macvlan \
  --subnet=10.0.0.0/24 --gateway=10.0.0.1 \
  -o parent=eth0 proxy_net
docker network create internal_net
```

## Step 2: Build and Start

```bash
# On Docker host
cd /path/to/web-ssh-gateway

# Create .env with your secrets
cp .env.example .env

# Build and start
docker compose -f docker/docker-compose.yml up -d --build

# Verify
curl http://localhost:8085/health
```

## Step 3: Configure Nginx

See `nginx-gateway.conf.example` for a sample SSL + mTLS configuration.

## Step 4: Security Checklist

- [ ] Generate strong API_KEY (64+ random chars)
- [ ] Generate Fernet ENCRYPTION_KEY
- [ ] Configure ALLOWED_CLIENT_CIDRS to your network
- [ ] Set SSH_STRICT_HOST_KEY_CHECKING=true
- [ ] Rotate all secrets before production
- [ ] Enable mTLS or SSO for internet-facing deployments
- [ ] Configure target allowlist (ALLOWED_TARGET_CIDRS)

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Container won't start | Check `docker compose logs` |
| 502 Bad Gateway | Verify container IP is reachable from Nginx host |
| WebSocket disconnects | Check Upgrade/Connection headers in Nginx config |
| SSH connection fails | Target must allow SSH from Docker host network |
