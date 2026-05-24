# Web SSH Gateway

Веб-терминал для выполнения SSH-команд через браузер.

- **Домен:** https://ssh.xloud.ru
- **Размещение:** Docker @ LXC 103 (10.0.0.145)
- **Прокси:** Nginx @ LXC 100 + Authelia SSO
- **Технологии:** FastAPI, Paramiko, vanilla JS

## Структура

```
web-ssh-gateway/
├── app/
│   ├── main.py              # FastAPI entry point
│   ├── ssh_manager.py       # SSH sessions via Paramiko
│   ├── models.py            # Pydantic models
│   ├── config.py            # Settings
│   └── static/
│       ├── index.html       # Web terminal UI
│       ├── style.css        # Dark terminal theme
│       └── app.js           # Frontend logic
├── docker/
│   ├── Dockerfile           # Container image definition
│   ├── docker-compose.yml   # Docker @ LXC 103, IP 10.0.0.145
│   └── requirements.txt     # Python dependencies
├── nginx-ssh.xloud.ru.conf  # Nginx config for LXC 100
├── deploy.md                # Deployment instructions
└── .dockerignore
```

## Быстрый старт (на LXC 103)

```bash
cd /media/1TB/Docker/web-ssh-gateway
docker compose -f docker/docker-compose.yml up -d
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| GET | /health | Health check |
| POST | /api/ssh/connect | Create SSH session |
| POST | /api/ssh/execute | Execute command |
| WS | /api/ssh/execute/stream | Stream command output |
| POST | /api/ssh/disconnect | Close session |
| GET | /api/ssh/sessions | List active sessions |

## Возможности

- Подключение по паролю или SSH-ключу (RSA/ED25519)
- Потоковый вывод команд через WebSocket
- Автоочистка неактивных сессий (5 мин)
- История команд в localStorage
- ANSI colors в терминале
- Authelia SSO/2FA аутентификация

## Deploy

См. [deploy.md](deploy.md) для полной инструкции по деплою на NOD.
