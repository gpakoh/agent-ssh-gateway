# Web SSH Gateway — Deployment Guide

## Overview
Deploy Web SSH Gateway on NOD infrastructure (LXC 103 Docker Host, proxied via LXC 100 Nginx).

**Domain:** `ssh.xloud.ru`
**Docker IP:** `10.10.10.145` (proxmox_macvlan)
**Target LXC:** LXC 103 (192.168.1.103) — Docker Host

---

## Prerequisites
- SSH access to LXC 103 (Debian 12, Docker installed)
- SSH access to LXC 100 (Nginx proxy, certbot installed)
- Authelia configured and running (10.10.10.106)

---

## Step 1: Copy Project to LXC 103

```bash
# From your workstation, copy project to LXC 103
scp -r ./web-ssh-gateway/ root@192.168.1.103:/media/1TB/Docker/

# Or on LXC 103 directly
cd /media/1TB/Docker/
git clone <repo-url> web-ssh-gateway
```

---

## Step 2: Build and Start Container

```bash
ssh root@192.168.1.103
cd /media/1TB/Docker/web-ssh-gateway

# Build image
docker-compose build

# Start
docker-compose up -d

# Verify it's running
docker-compose ps
docker-compose logs -f
```

The container should be accessible internally at `http://10.10.10.145:8080`.

---

## Step 3: Configure Nginx on LXC 100

```bash
ssh root@192.168.1.100

# Copy nginx config
cp /media/1TB/Docker/web-ssh-gateway/nginx-ssh.xloud.ru.conf /etc/nginx/sites-available/ssh.xloud.ru

# Enable site
ln -s /etc/nginx/sites-available/ssh.xloud.ru /etc/nginx/sites-enabled/

# Test config
nginx -t

# Reload
systemctl reload nginx
```

---

## Step 4: Obtain SSL Certificate

```bash
ssh root@192.168.1.100

# Add ssh.xloud.ru to certbot
certbot --nginx -d ssh.xloud.ru

# Or expand existing certificate
certbot --expand -d xloud.ru,www.xloud.ru,ssh.xloud.ru

# Verify auto-renewal
certbot renew --dry-run
```

---

## Step 5: Verify Deployment

### 5.1 Check container health
```bash
# On LXC 103
curl http://10.10.10.145:8080/health
# Expected: {"status":"ok"}
```

### 5.2 Check from Nginx
```bash
# On LXC 100
curl -k https://localhost/health -H "Host: ssh.xloud.ru"
# Expected: {"status":"ok"}
```

### 5.3 Check from outside (Authelia will redirect to login)
```bash
curl -I https://ssh.xloud.ru
# Expected: 302 redirect to Authelia auth
```

### 5.4 Browser test
Open `https://ssh.xloud.ru` in browser:
1. You should see Authelia login page
2. After SSO auth — the Web SSH Gateway terminal
3. Test connection to a local server (e.g., 192.168.1.103)

---

## Step 6: Update / Redeploy

```bash
ssh root@192.168.1.103
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
| 502 Bad Gateway | Verify container IP (10.10.10.145) is reachable from LXC 100: `ping 10.10.10.145` |
| WebSocket disconnects | Check Nginx config has Upgrade/Connection headers for /api/ssh/execute/stream |
| SSL error | Verify certbot generated certs for ssh.xloud.ru: `certbot certificates` |
| Authelia blocks | Check authelia-authrequest.conf is included; verify Authelia config allows ssh.xloud.ru |
| SSH connection fails | Target server must allow SSH from 10.10.10.145 (LXC 103 Docker network) |
| Can't reach 10.10.10.145 | Verify proxmox_macvlan network exists: `docker network ls` and check subnet |

---

## Architecture Reminder

```
[Browser] → DDOS-GUARD → Tenda (:443)
    → Nginx LXC 100 (:443, SSL)
        → Authelia auth check
            → Proxy Pass → 10.10.10.145:8080 (Docker LXC 103)
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
| `docker-compose.yml` | Docker Compose config (macvlan 10.10.10.145) |
| `nginx-ssh.xloud.ru.conf` | Nginx site config for LXC 100 |
| `requirements.txt` | Python dependencies |
| `.dockerignore` | Build exclusions |
| `deploy.md` | This file |
