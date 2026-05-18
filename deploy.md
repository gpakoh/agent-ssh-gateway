# Web SSH Gateway — Deployment Guide

## Overview
Deploy Web SSH Gateway on NOD infrastructure (LXC 103 Docker Host, proxied via LXC EXAMPLE Nginx).

**Domain:** `gateway.example.com`
**Docker IP:** `10.255.255.145` (docker_macvlan_example)
**Target LXC:** LXC 103 (10.255.255.101) — Docker Host

---

## Prerequisites
- SSH access to LXC 103 (Debian 12, Docker installed)
- SSH access to LXC EXAMPLE (Nginx proxy, certbot installed)
- Authelia configured and running (10.0.0.106)

---

## Step 1: Copy Project to LXC 103

```bash
# From your workstation, copy project to LXC 103
scp -r ./web-ssh-gateway/ root@10.255.255.101:/media/1TB/Docker/

# Or on LXC 103 directly
cd /media/1TB/Docker/
git clone <repo-url> web-ssh-gateway
```

---

## Step 2: Build and Start Container

```bash
ssh root@10.255.255.101
cd /media/1TB/Docker/web-ssh-gateway

# Build image
docker-compose build

# Start
docker-compose up -d

# Verify it's running
docker-compose ps
docker-compose logs -f
```

The container should be accessible internally at `http://10.255.255.145:8080`.

---

## Step 3: Configure Nginx on LXC EXAMPLE

```bash
ssh root@192.0.2.10

# Copy nginx config
cp /media/1TB/Docker/web-ssh-gateway/nginx-gateway.example.com.conf /etc/nginx/sites-available/gateway.example.com

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
ssh root@192.0.2.10

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
# On LXC 103
curl http://10.255.255.145:8080/health
# Expected: {"status":"ok"}
```

### 5.2 Check from Nginx
```bash
# On LXC EXAMPLE
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
3. Test connection to a local server (e.g., 10.255.255.101)

---

## Step 6: Update / Redeploy

```bash
ssh root@10.255.255.101
cd /media/1TB/Docker/web-ssh-gateway

# Pull latest code
git pull

# Rebuild and restart
docker-compose down
docker-compose build --no-cache
docker-compose up -d

# Check logs
docker-compose logs -f
```

---

## Troubleshooting

| Problem | Solution |
|---------|----------|
| Container won't start | Check `docker-compose logs` for errors |
| 502 Bad Gateway | Verify container IP (10.255.255.145) is reachable from LXC EXAMPLE: `ping 10.255.255.145` |
| WebSocket disconnects | Check Nginx config has Upgrade/Connection headers for /api/ssh/execute/stream |
| SSL error | Verify certbot generated certs for gateway.example.com: `certbot certificates` |
| Authelia blocks | Check authelia-authrequest.conf is included; verify Authelia config allows gateway.example.com |
| SSH connection fails | Target server must allow SSH from 10.255.255.145 (LXC 103 Docker network) |
| Can't reach 10.255.255.145 | Verify docker_macvlan_example network exists: `docker network ls` and check subnet |

---

## Architecture Reminder

```
[Browser] → DDOS-GUARD → Tenda (:443)
    → Nginx LXC EXAMPLE (:443, SSL)
        → Authelia auth check
            → Proxy Pass → 10.255.255.145:8080 (Docker LXC 103)
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
| `Dockerfile` | Container image |
| `docker-compose.yml` | Docker Compose config (macvlan 10.255.255.145) |
| `nginx-gateway.example.com.conf` | Nginx site config for LXC EXAMPLE |
| `requirements.txt` | Python dependencies |
| `.dockerignore` | Build exclusions |
| `deploy.md` | This file |
