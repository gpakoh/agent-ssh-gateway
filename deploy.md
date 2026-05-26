# Web SSH Gateway — Deployment Guide

## Overview
Deploy Web SSH Gateway on NOD infrastructure (LXC 103 Docker Host, proxied via LXC 100 Nginx).

**Domain:** `gateway.example.com`
**Docker IP:** `10.0.0.10` (docker bridge)
**Target LXC:** node-1 (10.0.0.1) — Docker Host

---

## Prerequisites
- SSH access to LXC 103 (Debian 12, Docker installed)
- SSH access to LXC 100 (Nginx proxy, certbot installed)
- Authelia configured and running (10.0.0.5)

---

## Step 1: Copy Project to LXC 103

```bash
# From your workstation, copy project to Docker host
scp -r ./web-ssh-gateway/ root@10.0.0.1:/opt/

# Or on the host directly
cd /opt/
git clone <repo-url> web-ssh-gateway
```

---

## Step 2: Build and Start Container

```bash
ssh root@10.0.0.1
cd /opt/web-ssh-gateway

# Build image
docker compose -f docker/docker-compose.yml build

# Start
docker compose -f docker/docker-compose.yml up -d

# Verify it's running
docker compose -f docker/docker-compose.yml ps
docker compose -f docker/docker-compose.yml logs -f
```

The container should be accessible internally at `http://10.0.0.10:8085`.

---

## Step 3: Configure Nginx

```bash
ssh root@10.0.0.2

# Copy nginx config
cp /opt/web-ssh-gateway/nginx-gateway.conf.example /etc/nginx/sites-available/gateway.example.com

# Enable site
ln -s /etc/nginx/sites-available/gateway.example.com /etc/nginx/sites-enabled/

# Test config
nginx -t

# Reload
systemctl reload nginx
```

---

## Step 4: Obtain SSL Certificate

```bash
ssh root@10.0.0.2

# Add gateway.example.com to certbot
certbot --nginx -d gateway.example.com

# Or expand existing certificate
certbot --expand -d example.com,www.example.com,gateway.example.com

# Verify auto-renewal
certbot renew --dry-run
```

---

## Step 5: Verify Deployment

### 5.1 Check container health
```bash
# On Docker host
curl http://10.0.0.10:8085/health
# Expected: {"status":"ok"}
```

### 5.2 Check from Nginx
```bash
# On nginx host
curl -k https://localhost/health -H "Host: gateway.example.com"
# Expected: {"status":"ok"}
```

### 5.3 Check from outside (Authelia will redirect to login)
```bash
curl -I https://gateway.example.com
# Expected: 302 redirect to Authelia auth
```

### 5.4 Browser test
Open `https://gateway.example.com` in browser:
1. You should see Authelia login page
2. After SSO auth — the Web SSH Gateway terminal
    3. Test connection to a local server (e.g., 10.0.0.1)

---

## Step 6: Update / Redeploy

```bash
ssh root@10.0.0.1
cd /opt/web-ssh-gateway

# Pull latest code
git pull

# Rebuild and restart
docker compose -f docker/docker-compose.yml down
docker compose -f docker/docker-compose.yml build --no-cache
docker compose -f docker/docker-compose.yml up -d

# Check logs
docker compose -f docker/docker-compose.yml logs -f
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Container won't start | Check `docker-compose logs` for errors |
| 502 Bad Gateway | Verify container IP (10.0.0.10) is reachable from nginx: `ping 10.0.0.10` |
| WebSocket disconnects | Check Nginx config has Upgrade/Connection headers for /api/ssh/execute/stream |
| SSL error | Verify certbot generated certs for gateway.example.com: `certbot certificates` |
| Authelia blocks | Check authelia-authrequest.conf is included; verify Authelia config allows gateway.example.com |
| SSH connection fails | Target server must allow SSH from the gateway container's network |
| Can't reach gateway container | Verify docker network exists: `docker network ls` and check connectivity |

---

## Architecture Reminder

```
[Browser] → CDN → Firewall (:443)
    → Nginx (:443, SSL)
        → Authelia auth check
            → Proxy Pass → 10.0.0.10:8085 (Gateway Container)
                → FastAPI + Paramiko → SSH Target Server
```

---

## Files Summary

| File | Purpose |
|------|---------|
| `app/main.py` | FastAPI entry point |
| `app/ssh_manager.py` | SSH session management (Paramiko) |
| `app/models.py` | Pydantic request/response models |
| `app/config.py` | Settings (env vars) |
| `app/static/index.html` | Frontend page |
| `app/static/style.css` | Terminal theme |
| `app/static/app.js` | Frontend logic (API, WebSocket, terminal) |
| `docker/Dockerfile` | Container image |
| `docker/docker-compose.yml` | Docker Compose config |
| `docker/requirements.txt` | Python dependencies |
| `nginx-gateway.conf.example` | Nginx site config for LXC 100 |
| `.dockerignore` | Build exclusions |
| `deploy.md` | This file |
