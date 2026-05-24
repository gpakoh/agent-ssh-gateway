# Web SSH Gateway: практический гайд

Версия API: `1.0.0`
Обновлено: `2026-05-24 09:47 UTC`

## 1) Как подключаться

### 1.1 Доступ из локальной сети (без SSO)

- Базовый URL: `http://192.168.1.103:8085`
- `Authelia` не требуется.
- Рекомендуемый режим для внутренних агентов и CI в LAN.

### 1.2 Доступ из интернета (через Authelia SSO)

- Базовый URL API: `https://ssh.xloud.ru`
- Перед вызовами API нужна SSO-аутентификация в `Authelia` (cookie-сессия).
- Используйте только свои учетные данные; не храните логины/пароли в документации и скриптах.

Пример (интернет через SSO):

```bash
# 1) Логин в Authelia (сохраняем cookie)
curl -k -c cookies.txt -X POST https://auth.xloud.ru/api/firstfactor \
  -H "Content-Type: application/json" \
  -d '{
    "username": "<YOUR_LOGIN>",
    "password": "<YOUR_PASSWORD>",
    "request_method": "GET",
    "request_uri": "https://ssh.xloud.ru/"
  }'

# 2) Вызов API через ту же cookie-сессию
curl -k -b cookies.txt -X POST https://ssh.xloud.ru/api/ssh/connect \
  -H "Content-Type: application/json" \
  -d '{
    "host": "192.0.2.10",
    "port": 22,
    "username": "root",
    "password": "<SSH_PASSWORD>"
  }'
```

Пример (локальная сеть):

```bash
curl -X GET http://192.168.1.103:8085/health
```

## 2) Общие правила API

- Формат ошибок унифицирован: `detail.message`, `detail.code`, `detail.retryable`, `detail.hint`, `detail.http_status`.
- Для long-running задач используйте `/api/jobs/*` и SSE endpoints `/stream`/`/events`.
- Для `session_id` сначала создайте сессию через `POST /api/ssh/connect` или `POST /api/servers/{server_id}/connect`.
- `X-Request-ID` и rate-limit headers описаны в контракте для всех методов.

## 3) Карта API

- Всего методов: **98**
- Всего разделов: **11**

- `system`: 7 методов
- `ssh`: 10 методов
- `jobs`: 9 методов
- `files`: 16 методов
- `context`: 18 методов
- `git`: 8 методов
- `templates`: 4 методов
- `code`: 13 методов
- `servers`: 4 методов
- `snapshots`: 4 методов
- `webhooks`: 5 методов

## 4) Полный список методов

Формат записи:
- `METHOD PATH` — краткое назначение
- `Обязательные:` path/query/header + обязательные поля body
- `200:` content-type успешного ответа
- `Ошибки:` коды ошибок
- `Auth:` используемая схема авторизации из OpenAPI

### system

Системные endpoint'ы и инфраструктура (health, metrics, конфиг, SDK).

- `GET /` — Root
  Описание: Serve the main page.
  Обязательные: -
  200: text/html, application/json
  Ошибки: 422, 500
  Auth: нет
- `GET /api/circuit-breaker/stats` — Circuit Breaker Stats
  Описание: Get circuit breaker statistics.
  Обязательные: -
  200: application/json
  Ошибки: 422, 500
  Auth: нет
- `GET /api/config/session` — Get Session Config
  Описание: Get current session configuration.
  Обязательные: -
  200: application/json
  Ошибки: 422, 500
  Auth: нет
- `PATCH /api/config/session/timeout` — Update Session Timeout
  Описание: Update session timeout dynamically.
  Обязательные: body: application/json: timeout
  200: application/json
  Ошибки: 422, 500
  Auth: нет
- `GET /api/sdk/download` — Download Sdk
  Описание: Download Python SDK. Supports API key auth via query param or header.
  Обязательные: -
  200: text/x-python
  Ошибки: 422, 500
  Auth: ApiKeyQuery, ApiKeyHeader
- `GET /health` — Health Check
  Описание: Health check endpoint.
  Обязательные: -
  200: application/json
  Ошибки: 422, 500
  Auth: нет
- `GET /metrics` — Prometheus Metrics
  Описание: Prometheus metrics endpoint.
  Обязательные: -
  200: text/plain
  Ошибки: 422, 500
  Auth: нет

### ssh

Управление SSH-сессиями, команды и интерактивный PTY.

- `POST /api/ssh/connect` — Ssh Connect
  Описание: Create a new SSH session.
  Обязательные: body: application/json: host, username
  200: application/json
  Ошибки: 400, 401, 404, 422, 500, 502, 504
  Auth: нет
- `POST /api/ssh/disconnect` — Ssh Disconnect
  Описание: Close an SSH session.
  Обязательные: body: application/json: session_id
  200: application/json
  Ошибки: 400, 401, 404, 422, 500, 502, 504
  Auth: нет
- `POST /api/ssh/execute` — Ssh Execute
  Описание: Execute a command on an existing SSH session.
  Обязательные: body: application/json: session_id, command
  200: application/json
  Ошибки: 400, 401, 404, 422, 500, 502, 504
  Auth: нет
- `POST /api/ssh/heartbeat` — Ssh Heartbeat
  Описание: Refresh session timeout by touching it.
  Обязательные: body: application/json: session_id
  200: application/json
  Ошибки: 400, 401, 404, 422, 500, 502, 504
  Auth: нет
- `POST /api/ssh/pty/{session_id}/close` — Pty Close
  Описание: Close PTY session.
  Обязательные: params: path.session_id
  200: application/json
  Ошибки: 400, 401, 404, 422, 500, 502, 504
  Auth: нет
- `POST /api/ssh/pty/{session_id}/create` — Pty Create
  Описание: Create PTY session.
  Обязательные: params: path.session_id | body: application/json: model=PTYCreateRequest
  200: application/json
  Ошибки: 400, 401, 404, 422, 500, 502, 504
  Auth: нет
- `POST /api/ssh/pty/{session_id}/input` — Pty Input
  Описание: Send input to PTY.
  Обязательные: params: path.session_id | body: application/json: data
  200: application/json
  Ошибки: 400, 401, 404, 422, 500, 502, 504
  Auth: нет
- `GET /api/ssh/pty/{session_id}/output` — Pty Output
  Описание: Get PTY output.
  Обязательные: params: path.session_id
  200: application/json
  Ошибки: 400, 401, 404, 422, 500, 502, 504
  Auth: нет
- `GET /api/ssh/session/{session_id}/health` — Session Health
  Описание: Check session health and auto-reconnect if needed.
  Обязательные: params: path.session_id
  200: application/json
  Ошибки: 400, 401, 404, 422, 500, 502, 504
  Auth: нет
- `GET /api/ssh/sessions` — Ssh Sessions
  Описание: List all active SSH sessions.
  Обязательные: -
  200: application/json
  Ошибки: 400, 401, 404, 422, 500, 502, 504
  Auth: нет

### jobs

Фоновые задачи, статусы, результаты и SSE-потоки.

- `GET /api/jobs` — Jobs List
  Описание: List background jobs.
  Обязательные: -
  200: application/json
  Ошибки: 404, 422, 500
  Auth: нет
- `GET /api/jobs/queue/dead` — Jobs Dead Letter
  Описание: Get dead letter queue jobs.
  Обязательные: -
  200: application/json
  Ошибки: 404, 422, 500
  Auth: нет
- `GET /api/jobs/queue/stats` — Jobs Queue Stats
  Описание: Get Redis job queue statistics.
  Обязательные: -
  200: application/json
  Ошибки: 404, 422, 500
  Auth: нет
- `POST /api/jobs/run` — Jobs Run
  Описание: Start a background job on an SSH session.
  Обязательные: body: application/json: session_id, command
  200: application/json
  Ошибки: 404, 422, 500
  Auth: нет
- `POST /api/jobs/{job_id}/cancel` — Jobs Cancel
  Описание: Cancel a running job.
  Обязательные: params: path.job_id
  200: application/json
  Ошибки: 404, 422, 500
  Auth: нет
- `GET /api/jobs/{job_id}/events` — Jobs Events
  Описание: Alias for /api/jobs/{job_id}/stream — SSE job progress events.
  Обязательные: params: path.job_id
  200: text/event-stream
  Ошибки: 404, 422, 500
  Auth: нет
- `GET /api/jobs/{job_id}/result` — Jobs Result
  Описание: Get full job result.
  Обязательные: params: path.job_id
  200: application/json
  Ошибки: 404, 422, 500
  Auth: нет
- `GET /api/jobs/{job_id}/status` — Jobs Status
  Описание: Get job status.
  Обязательные: params: path.job_id
  200: application/json
  Ошибки: 404, 422, 500
  Auth: нет
- `GET /api/jobs/{job_id}/stream` — Jobs Stream
  Описание: Stream job output via Server-Sent Events.
  Обязательные: params: path.job_id
  200: text/event-stream
  Ошибки: 404, 422, 500
  Auth: нет

### files

Чтение, запись, патчи, загрузка/выгрузка и дерево файлов.

- `PATCH /api/batch/edit` — Batch Edit
  Описание: Edit multiple files in a single request.
  Обязательные: body: application/json: session_id, files
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/batch/execute` — Batch Execute
  Описание: Execute multiple file operations in a single transaction.
  Обязательные: body: application/json: context_id, operations
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/batch/read` — Batch Read
  Описание: Read multiple files in a single request.
  Обязательные: body: application/json: session_id, paths
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/bulk/edit` — Bulk Edit Files
  Описание: Edit multiple files concurrently. Example: { "session_id": "...", "files": [ { "path": "app/main.py", "operations": [ {"type": "replace", "old": "def old():", "new": "def new():"} ] }, { "path": "app/config.py", "operations": [ {"type": "replace", "old": "DEBUG = True", "new": "DEBUG = False"} ] } ] }
  Обязательные: body: application/json: session_id, files
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/bulk/execute` — Bulk Execute
  Описание: Execute multiple commands concurrently.
  Обязательные: body: application/json: session_id, commands
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/bulk/read` — Bulk Read Files
  Описание: Read multiple files concurrently.
  Обязательные: body: application/json: session_id, paths
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `GET /api/file/download` — File Download
  Описание: Download file from remote server.
  Обязательные: params: query.session_id, query.path
  200: application/octet-stream
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `PATCH /api/file/edit` — File Edit
  Описание: Edit a remote file using patch operations.
  Обязательные: body: application/json: session_id, path, operations
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/file/patch` — File Patch
  Описание: Apply a unified diff patch.
  Обязательные: body: application/json: session_id, patch
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `GET /api/file/raw` — File Raw
  Описание: Read a remote file and return raw content as text/plain. Supports Range header (bytes=start-end) or offset/limit query params.
  Обязательные: params: query.session_id, query.path
  200: text/plain
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/file/read` — File Read
  Описание: Read a file from a remote server.
  Обязательные: body: application/json: session_id, path
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/file/upload` — File Upload
  Описание: Upload file to remote server (base64 encoded via query params).
  Обязательные: params: query.session_id, query.path, query.content
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/file/upload/json` — File Upload Json
  Описание: Upload file via JSON body (base64 encoded). Preferred for large files (>2KB) where query params may fail.
  Обязательные: body: application/json: session_id, path, content
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/file/upload/stream` — File Upload Stream
  Описание: Upload file using multipart/form-data for large files (1MB+).
  Обязательные: params: query.session_id, query.path | body: multipart/form-data: file
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/file/write` — File Write
  Описание: Write file via JSON body (atomic, no heredoc escaping). Use for Python code with quotes, special chars, or large content. Mode: 'write' (overwrite) or 'append' (append to end).
  Обязательные: body: application/json: session_id, path, content
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/tree` — Get File Tree
  Описание: Get directory tree structure.
  Обязательные: body: application/json: session_id, path
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет

### context

Умный контекст проекта, вкладки, курсор, история, аналитика.

- `POST /api/context/bookmark` — Context Add Bookmark
  Описание: Add bookmark.
  Обязательные: body: application/json: context_id, path, line
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `DELETE /api/context/bookmark` — Context Remove Bookmark
  Описание: Remove bookmark.
  Обязательные: params: query.context_id, query.path, query.line
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/context/command` — Context Add Command
  Описание: Add command to history.
  Обязательные: body: application/json: context_id, command
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/context/create` — Context Create
  Описание: Create a new development context with git awareness.
  Обязательные: body: application/json: session_id, path
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/context/cursor` — Context Update Cursor
  Описание: Update cursor position in file.
  Обязательные: body: application/json: context_id, path, line
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/context/file/close` — Context File Close
  Описание: Close file in smart context (closes tab).
  Обязательные: body: application/json: context_id, path
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `PATCH /api/context/file/edit` — Context File Edit
  Описание: Edit a file with context awareness (auto-commit, validation).
  Обязательные: body: application/json: context_id, path, operations
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/context/file/open` — Context File Open
  Описание: Open file in smart context (creates tab).
  Обязательные: body: application/json: context_id, path
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/context/file/read` — Context File Read
  Описание: Read a file using context (session_id extracted from context).
  Обязательные: body: application/json: session_id, path
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `GET /api/context/list` — Context List
  Описание: List all active contexts.
  Обязательные: -
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/context/search` — Context Add Search
  Описание: Add search query to history.
  Обязательные: body: application/json: context_id, query
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `GET /api/context/{context_id}` — Context Get
  Описание: Get context details.
  Обязательные: params: path.context_id
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `DELETE /api/context/{context_id}` — Context Delete
  Описание: Delete a context.
  Обязательные: params: path.context_id
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `GET /api/context/{context_id}/state` — Context Get State
  Описание: Get smart context state.
  Обязательные: params: path.context_id
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/recovery/backup` — Recovery Create Backup
  Описание: Create a backup before making changes.
  Обязательные: body: application/json: context_id
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `GET /api/recovery/backups` — Recovery List Backups
  Описание: List available backups.
  Обязательные: params: query.context_id
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/recovery/restore` — Recovery Restore Backup
  Описание: Restore from backup.
  Обязательные: body: application/json: context_id
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/validate` — Validate Context
  Описание: Run validation pipeline (mypy + pytest) for context.
  Обязательные: body: application/json: context_id
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет

### git

Git-операции и diff в рамках сессии/контекста.

- `POST /api/git/backup` — Git Backup
  Описание: Create a git stash backup.
  Обязательные: params: query.context_id
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/git/commit` — Git Commit
  Описание: Create a git commit for context.
  Обязательные: body: application/json: context_id, message
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `GET /api/git/diff` — Git Diff
  Описание: Get git diff for context.
  Обязательные: params: query.context_id
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/git/diff` — Git Diff
  Описание: Get git diff for working directory or staged changes.
  Обязательные: body: application/json: session_id
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/git/init` — Git Init
  Описание: Initialize git repository for context.
  Обязательные: body: application/json: context_id
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/git/restore` — Git Restore
  Описание: Restore from stash.
  Обязательные: params: query.context_id
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `GET /api/git/simple-status` — Git Simple Status
  Описание: Simple git status — branch, modified, staged, untracked files.
  Обязательные: params: query.session_id
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/git/status` — Git Status
  Описание: Refresh git status for context.
  Обязательные: params: query.context_id
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет

### templates

Шаблоны и рендеринг кода/файлов.

- `POST /api/scaffold/python-class` — Scaffold Python Class
  Описание: Scaffold a Python class + test file from template.
  Обязательные: body: application/json: session_id, module_path, class_name
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `GET /api/templates` — List Templates
  Описание: List all available code templates.
  Обязательные: -
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/templates/render` — Render Template
  Описание: Render template and save to file.
  Обязательные: body: application/json: context_id, template_id, target_path
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `GET /api/templates/{template_id}` — Get Template
  Описание: Get template details.
  Обязательные: params: path.template_id
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет

### code

Поиск по коду, вставки, генерация и автодополнение.

- `POST /api/analytics` — Get Project Analytics
  Описание: Analyze project and return metrics.
  Обязательные: body: application/json: session_id, path
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/ast/analyze` — Ast Analyze
  Описание: Analyze Python code structure using AST.
  Обязательные: body: application/json: session_id, path
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/ast/extract` — Ast Extract
  Описание: Extract a block of code into a new function.
  Обязательные: body: application/json: session_id, path, start_line, end_line, func_name
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/ast/rename` — Ast Rename
  Описание: Rename a symbol (function, class, variable) using AST. Supports single file ('path') or multiple files ('files' array).
  Обязательные: body: application/json: session_id, old_name, new_name
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/code/complete` — Code Complete
  Описание: Suggest code completion.
  Обязательные: body: application/json: session_id, path, partial_code
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/code/generate` — Code Generate
  Описание: Generate code based on natural language instruction.
  Обязательные: body: application/json: instruction
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/code/insert` — Code Insert
  Описание: Intelligently insert code based on natural language instruction.
  Обязательные: body: application/json: context_id, path, instruction
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/code/search` — Code Search
  Описание: Search for code pattern in project.
  Обязательные: body: application/json: session_id, path, query
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/project/structure` — Project Structure
  Описание: Get project structure with metadata and git status.
  Обязательные: body: application/json: session_id, path
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `GET /api/project/tree` — Project Tree
  Описание: Simple project tree — list files and directories. Returns flat list with type, path, size for quick introspection.
  Обязательные: params: query.session_id
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/refactor/rename` — Refactor Rename
  Описание: Alias for /api/ast/rename — AST-aware symbol renaming.
  Обязательные: body: application/json: session_id, old_name, new_name
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/replace/global` — Global Replace
  Описание: Replace across all project files.
  Обязательные: body: application/json: session_id, path, search
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/search/global` — Global Search
  Описание: Search across all project files.
  Обязательные: body: application/json: session_id, path, query
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет

### servers

Реестр серверов и быстрые подключения.

- `GET /api/servers` — List Servers
  Описание: List all configured servers.
  Обязательные: -
  200: application/json
  Ошибки: 400, 404, 409, 422, 500
  Auth: нет
- `POST /api/servers` — Add Server
  Описание: Add a new server.
  Обязательные: body: application/json: id, name, host
  200: application/json
  Ошибки: 400, 404, 409, 422, 500
  Auth: нет
- `DELETE /api/servers/{server_id}` — Remove Server
  Описание: Remove a server.
  Обязательные: params: path.server_id
  200: application/json
  Ошибки: 400, 404, 409, 422, 500
  Auth: нет
- `POST /api/servers/{server_id}/connect` — Connect Server
  Описание: Connect to a server and return session.
  Обязательные: params: path.server_id | body: application/json: model=ConnectServerRequest
  200: application/json
  Ошибки: 400, 404, 409, 422, 500
  Auth: нет

### snapshots

Снимки состояния проекта и восстановление.

- `GET /api/snapshots` — List Snapshots
  Описание: List all snapshots for context.
  Обязательные: params: query.context_id
  200: application/json
  Ошибки: 404, 422, 500
  Auth: нет
- `POST /api/snapshots` — Create Snapshot
  Описание: Create a snapshot of current project state.
  Обязательные: body: application/json: context_id, name
  200: application/json
  Ошибки: 404, 422, 500
  Auth: нет
- `POST /api/snapshots/restore` — Restore Snapshot
  Описание: Restore project from snapshot.
  Обязательные: body: application/json: context_id, snapshot_id
  200: application/json
  Ошибки: 404, 422, 500
  Auth: нет
- `DELETE /api/snapshots/{snapshot_id}` — Delete Snapshot
  Описание: Delete a snapshot.
  Обязательные: params: path.snapshot_id, query.context_id
  200: application/json
  Ошибки: 404, 422, 500
  Auth: нет

### webhooks

Webhook-конфигурации и деплой-триггеры.

- `GET /api/webhooks` — List Webhooks
  Описание: List all webhooks.
  Обязательные: -
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/webhooks` — Create Webhook
  Описание: Create a new webhook for auto-deployment.
  Обязательные: body: application/json: name, target_path, deploy_command, context_id
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `DELETE /api/webhooks/{webhook_id}` — Delete Webhook
  Описание: Delete a webhook.
  Обязательные: params: path.webhook_id
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `POST /api/webhooks/{webhook_id}/deploy` — Trigger Deploy
  Описание: Manually trigger deployment.
  Обязательные: params: path.webhook_id | body: application/json: session_id
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет
- `GET /api/webhooks/{webhook_id}/deployments` — List Deployments
  Описание: List deployment history.
  Обязательные: params: path.webhook_id
  200: application/json
  Ошибки: 400, 404, 422, 500
  Auth: нет

## 5) Минимальный рабочий сценарий

1. `POST /api/ssh/connect` — получить `session_id`.
2. `POST /api/ssh/execute` или `POST /api/jobs/run` — выполнить команду.
3. Для фоновых задач: `GET /api/jobs/{job_id}/status` и `GET /api/jobs/{job_id}/result`.
4. Для файлов: `POST /api/file/read`, `POST /api/file/write`, `POST /api/file/patch`.
5. По завершении: `POST /api/ssh/disconnect`.

## 6) Рекомендации для агентных клиентов

- Всегда обрабатывайте `detail.code` и `retryable`, а не только HTTP-статус.
- Для нестабильных ошибок делайте backoff-retry, для `VALIDATION_ERROR` — не ретраить.
- Для больших операций используйте jobs + SSE вместо polling в tight-loop.
- В проде логируйте `X-Request-ID` для трассировки.
