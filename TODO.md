# Agent SSH Gateway — TODO

## ✅ Done

- **Issue #4 — RBAC + multi-tenant sessions** ✅ (`b4e388ff`): `app/rbac.py` — роли `admin`/`operator`/`viewer`/`custom` с permissions (connect/execute/upload/admin) и resource_selector (tenant labels); маппинг scope → permission; `default_role_for_scopes` (legacy `["*"]` → admin, backward compat для явных scopes); токены несут роль + labels (`agent_token:meta` в Redis, `POST /api/agent/token` принимает `role`/`labels`, refresh сохраняет); `require_scope`/`ws_auth_check` разрешают scope через роль; `ensure_session_owner` — admin видит все, tenant-overlap = cross-tenant grant; `GET /api/ssh/sessions` фильтрует по tenant для non-admin; сессии наследуют labels от токена; JWT-идентичности несут роль (create_jwt → `role` в payload). 45 новых тестов, полный прогон 3868 passed, ruff+mypy clean.
- **Issue #2 — OAuth2/SSO login** ✅ (`6eeb7b5c`): `app/oauth_sso.py` — провайдер-абстракция (built-in GitHub/GitLab/Google + generic OIDC через `/.well-known/openid-configuration` auto-discovery), PKCE S256 (code_verifier/code_challenge), state store с TTL; env-конфиг `OAUTH_PROVIDER`/`OAUTH_CLIENT_ID`/`OAUTH_CLIENT_SECRET`/`OAUTH_ISSUER_URL` (+`OAUTH_REDIRECT_URI`, `OAUTH_ALLOWED_EMAILS`); `GET /api/auth/oauth/authorize` (redirect к провайдеру) + `/api/auth/oauth/callback` (code → token → userinfo → gateway JWT той же sub/type="web-ui" модели через `create_jwt`); `/api/auth/oauth/config` для Web UI; кнопка SSO в login-форме (index.html + app.js), local login работает; callback отдаёт JSON (API) или HTML-страницу с httpOnly-кукой `auth_token` (браузер). 35 новых тестов, полный прогон 3821 passed, ruff+mypy clean, CI green оба remote.
- **Issue #5 — Connection pooling + pre-warming** ✅ (`9814e55e`): `ConnectionPool` (`app/ssh_pool.py`) — пул бездействующих SSH-транспортов по ключу (host, port, username, auth_method), LRU eviction + TTL expiry, включается через `SSH_CONNECTION_POOL_SIZE` (default 0 = выключен) + `SSH_CONNECTION_POOL_TTL_SECONDS` (default 60); `POST /api/ssh/prewarm` — session_id возвращается сразу, подключение в background; execute/execute-argv ждут prewarm-задачу; пул-метрики (idle/hits/misses/evictions) в `/metrics`; graceful shutdown. 18 новых тестов, полный прогон 3786 passed, ruff+mypy clean, CI green оба remote.
- **Issue #1 — Audit log persistence в PostgreSQL** ✅ (`f9d60ee9`): `audit_log` таблица (Alembic `002_audit_log`) + `AuditLogEntry` модель + `AuditLogStore` (`app/audit_store.py`); каждый `POST /api/ssh/execute` и `execute-argv` пишет строку (allowed/denied/async-job) с command/exit_code/duration_ms; `GET /api/audit` — фильтры session_id/event_type/decision/since/until + пагинация (limit/offset); ретенция по расписанию (`AUDIT_LOG_RETENTION_DAYS` default 90, `AUDIT_LOG_CLEANUP_INTERVAL_SECONDS`); обратная совместимость — выключен по умолчанию (`AUDIT_LOG_PERSIST_ENABLED=false`), JSONL-аудит не тронут, create_tables идемпотентен. 23 новых теста, полный прогон 3768 passed, ruff+mypy clean.
- Task tracking (old P1-P6, QoL, CI/DevOps) — все сделано и закоммичено
- **P7 Context Detection** — `app/context.py`, 23 тестов, span-классификация команд, known-safe wrappers
- **P8 AST Matching** — `app/ast_matcher.py`, 17 тестов, stdlib ast для Python, regex fallback
- **P9 Confidence v2** — `app/confidence.py`, 18 тестов, span-aware, сигналы (executed/data/comment/boosters)
- **P10 Simulate Mode** — `app/simulate.py`, 15 тестов, `gateway_simulate` MCP tool, три формата (plain/hook_json/decision_log)
- **P11 Suggest Allowlist Clustering** — `app/suggest.py`, 55 тестов, Jaccard-кластеризация, генерация паттернов, safety filter
- **P12 SQLite History** — `app/history.py`, 35 тестов, SQLite-логирование решений, batch write, prune, статистика

## 📋 Remaining (from DCG gap analysis)

### ✅ P13 — Docker pack patterns
Все DCG-паттерны уже присутствуют в `app/packs/docker.py` (9 docker + 4 compose).
Safe-паттерны (allow list для docker ps/logs/build...) — опционально.

### P14 — Heredoc scanning ✅ (mostly done)
`app/heredoc_scanner.py` уже существует:
- Tier 1: trigger detection (inline scripts, heredocs, here-strings, cmd subs)
- Tier 2: content extraction + recursive scan via `_check_all_destructive()`
- Интегрирован в pipeline как Gate 2b (command_policy.py:800-812)
- 52 теста в `tests/test_heredoc_scanner.py`
- Исправлено: `HEREDOC_BODY` добавлен в `should_check()` в context.py

### P15 — Inline script scanning ✅
`extract_inline_scripts` в `app/heredoc_scanner.py` работает для:
- bash -c, sh -c, python -c, ruby -e, perl -e, node -e, php -r ✅
- PowerShell -Command / -EncodedCommand ✅
- Исправлены вложенные/escaped ковычки через `_find_closing_quote()` ✅

### P16 — Ask mode (operator approval через Telegram) ✅
- `CommandPolicyMode.ASK` определён и обрабатывается в evaluate_command_policy
- `ApprovalRequest` store в `app/policy_ask.py` (in-memory, TTL 300s)
- SSH router возвращает HTTP 202 с approval_id при ASK mode
- `POST /api/admin/approval/decision` — эндпоинт для approve/deny per-command
- Telegram notifier поддерживает approval_id в action payloads:
  - С approval_id → POST в `/api/admin/approval/decision`
  - Без approval_id → POST в `/api/admin/access-control/decision` (как было)
- `approval_id` и `requires_approval` передаются в structured audit events
- Форматирование показывает approval_id в Telegram-сообщениях

### P17 — Suggestions при блокировке ✅
`DestructivePattern.suggestions` заполнен во ВСЕХ паках (300 паттернов):
- system.py (54), database.py (37), git_pack.py (16), firewall.py (13),
  loadbalancer.py (30), cloud.py (50), docker.py + compose (13 + остальные)
- Конвенция: 2 suggestions на паттерн — preview/check + safer alternative
- Отображается в ответе policy evaluation (decision.suggestion)

### P17a — Модель suggestions: kind + список + описания ✅
Апгрейд по образцу DCG (`src/suggestions.rs`):
- `SuggestionKind` enum (5 типов: preview_first, safer_alternative, workflow_fix, documentation, allow_safely)
- `PatternSuggestion` получил поле `kind` (478 записей во всех 9 паках)
- `DestructiveMatch.suggestions` / `ScanFinding.suggestions` — полный список с command+description+kind
- `CommandPolicyDecision.suggestions` — все suggestions строкой «cmd — описание»
- Обратная совместимость: `suggestion` (первый command) сохранён
- В serialize-точках: simulate.py, scan_project.py, heredoc_scanner.py
- +4 теста (kind default, scan findings, blocked decision)

### P18 — Больше pack-паттернов (K8s, AWS, GCP, БД, DNS...)
У DCG ~50 доменов, у нас ~10. Постепенное расширение по необходимости.

#### Итерация 1 — DNS + Package Managers ✅
- Новый пак **dns** (10 паттернов): nsupdate delete/local, dig AXFR/IXFR, Cloudflare (wrangler dns-records delete, API delete dns_record/zone, terraform destroy record), Route53 (change-resource-record-sets DELETE, delete health-check/query-logging/traffic-policy)
- Новый пак **package_managers** (15 паттернов): npm/yarn/pnpm publish + unpublish, pip uninstall/install-URL, apt remove/purge/autoremove, yum/dnf remove, cargo publish/yank, gem push, brew uninstall, poetry publish/remove, mvn deploy/release:perform, gradle publish
- Все паттерны с 2 suggestions (preview + alternative), dry-run не блокируется (negative lookahead)
- Регистрация в registry.py, 11 паков / 274 паттерна
- Тесты: +7 (pack_count 9→11, cross-section, smoke dns/pkg, suggestions-конвенция) — 3707 passed, ruff clean

#### Идеи следующих итераций
- Все DCG-домены портированы (dns, package_managers, secrets, backup, monitoring, storage, cicd) — P18 завершён ✅
- Идея: **DCG monitoring SaaS-паки** — datadog (4), newrelic (6), pagerduty (7), splunk (4) → расширение monitoring.py (после P18.7 — уже в работе)
- Идея: **MCP-портал в прод** — деплой `examples/mcp_server` как отдельный сервис (read/audit-first tools) — отдельная тема вне pack-системы (ВЫПОЛНЕНО: Dockerfile.mcp-server + compose-сервис mcp-server, streamable-http 8087, `13e8bc85`)

#### Итерация 2 — Secrets ✅
- Новый пак **secrets** (23 паттерна): Vault (secrets disable, kv destroy/metadata-delete/delete, delete, policy delete, auth disable, token/lease revoke), AWS (secretsmanager delete-resource-policy/remove-regions, ssm delete-parameter(s)), Doppler (secrets/projects/environments/configs delete), 1Password (op item/document/vault/user/group/connect-token delete)
- GCP (gcloud secrets delete, kms versions destroy) — уже были в cloud.py, НЕ дублированы
- Все паттерны с 2 suggestions (preview + alternative); read/list операции не блокируются
- Регистрация в registry.py, 12 паков / 297 паттернов
- Тесты: +4 (pack_count 11→12, cross-section, smoke secrets, read-ops-not-blocked) — 3711 passed, ruff clean

#### Итерация 3 — Backup ✅
- Новый пак **backup** (22 паттерна): borg (delete/prune/compact/recreate/break-lock), restic (forget/prune/key remove/unlock --remove-all/cache --cleanup), rclone (sync/delete/deletefile/purge/cleanup/dedupe/move), velero (backup/schedule/restore delete), duplicity (remove-older-than/delete)
- Все паттерны с 2 suggestions (preview + alternative); list/info/check/snapshots/dry-run не блокируются
- Регистрация в registry.py, 13 паков / 319 паттернов
- Тесты: +4 (pack_count 12→13, cross-section, smoke backup, reads-not-blocked) — 3715 passed, ruff clean

#### Итерация 4 — Monitoring ✅
- Новый пак **monitoring** (17 паттернов): promtool (tsdb delete/remove-limits, rules-file delete), Prometheus API (delete_series), Grafana (CLI plugins uninstall / admin reset-admin-password, API delete dashboard/datasource), InfluxDB (delete/bucket delete/org delete/task delete, 1.x DROP DATABASE/SERIES), whisper (delete/clean), kubectl delete PrometheusOperator resources
- Все паттерны с 2 suggestions (preview + alternative); list/get/query/check/dry-run не блокируются
- Регистрация в registry.py, 14 паков / 336 паттернов
- Тесты: +4 (pack_count 13→14, cross-section, smoke monitoring, reads-not-blocked) — 3719 passed, ruff clean

#### Итерация 5 — Storage ✅
- Новый пак **storage** (21 паттерн): ZFS (destroy/rollback/zpool destroy/zpool remove), S3 (sync --delete, s3api delete-objects), GCS (gcloud storage buckets/objects delete, storage rm, gsutil rsync -d), MinIO (mc rb/rm/admin bucket delete/mirror --remove/admin user/policy), Azure (storage account/container delete, blob delete-batch, azcopy remove/sync --delete-destination)
- Все паттерны с 2 suggestions (preview + alternative); list/describe/ls/dry-run/-n не блокируются
- Регистрация в registry.py, 15 паков / 357 паттернов
- Тесты: +4 (pack_count 14→15, cross-section, smoke storage, reads-not-blocked) — 3723 passed, ruff clean

#### Итерация 6 — CI/CD ✅
- Новый пак **cicd** (23 паттерна): GitHub Actions (gh secret/variable delete, workflow disable, run cancel/delete, api DELETE secrets/variables), GitLab (glab variable/ci delete, api DELETE variables, gitlab-runner unregister), CircleCI (context delete/remove-secret, orb delete, namespace delete, pipeline delete, api DELETE envvar), Jenkins (jenkins-cli delete-job/node/credentials/builds/view, curl doDelete)
- gh run delete добавлен вручную (в DCG только run cancel)
- Все паттерны с 2 suggestions (preview + alternative); list/view/status/verify не блокируются
- Регистрация в registry.py, 16 паков / 380 паттернов
- Тесты: +4 (pack_count 15→16, cross-section, smoke cicd, reads-not-blocked) — 3727 passed, ruff clean

## 🔧 Аудит архитектуры (P19, по architecture-code-rules.md) — Jul 2026

Критический аудит по правилам `/media/1TB/Python/architecture-code-rules.md`.
Статусы: **исправить** / исключение / отложить.

- **P19.1 — Batch-менеджеры: контракт-тесты + чистка (исправить)**: аудит показал «дубликат», но при разборе выяснилось: `BatchOperationsManager` (транзакционный файловый батч, routers/batch.py) и `BulkOperationsManager` (конкурентный исполнитель, routers/jobs.py + files.py) — разные обязанности (SRP), слияние ухудшило бы код. Реально убрано: мёртвый `self._semaphore` в `bulk_operations_v2.py` + добавлены контракт-тесты `tests/test_batch_managers.py` (11 тестов: execute_bulk, execute_batch_commands, read/edit_files_bulk, execute_batch read/edit/unknown/stop/commit), чтобы пути не разошлись. Асимметрия policy-check: v1 проверяет в `_execute_command`, v2 — только в роутере — оставлено, роутерная проверка покрыта test_command_policy_enforcement.py.
- **P19.2 — Сервис-слой для god-объектов (исправить)**: `routers/files.py` (1277 строк), `routers/context.py` (700), `routers/jobs.py` (452), `admin_access.py` (262). Бизнес-логика в роутерах; `app/services/` почти пуст.
  - ✅ `app/services/command_gate.py` создан: единая цепочка access-control → effective profile → policy decision → audit (SEC-07). `routers/jobs.py` переведён на него (jobs_run + bulk_execute, −80 строк дубликата).
  - ✅ Миграция завершена: `routers/ssh.py` (execute, execute-argv, WS execute/stream) и `routers/batch.py` (execute-ops) переведены на единый gate; WS ходит через `request=None + source_ip`, ASK-mode 202 добавлен в execute-argv для консистентности; `resolve_effective_profile_with_access_gate` выделен как общий core. Аудит-формат для batch/WS унифицирован (route=...). Тесты: патчи метрик переведены на `app.services.command_gate.metrics`, argv-тест — на `CommandGateDecision`.
  - ✅ Контракт: 3744 passed, ruff+mypy clean. Осталось (след. шаг P19.2): `routers/files.py` / `routers/context.py` — вынос бизнес-логики в сервисы.
  - ✅ Вынос завершён: 5 сервисов в `app/services/` — `file_editing.py` (edit_many), `project_patch.py` (apply_project_patch, транзакционный патч с rollback), `project_structure.py` (scan_project_structure/tree), `context_editing.py` (edit_file_with_context: backup → edit → diff → auto-commit → validation), `scaffolding.py` (scaffold_python_class). `routers/files.py` 1277 → 912 строк, `routers/context.py` 700 → 484. Полный прогон 3744 passed, ruff+mypy clean.
- **P19.3 — Чекер границ импортов (исправить)**: ARC-31 рекомендует лёгкий AST/import-чекер первым шагом. `tests/test_architecture_boundaries.py` (3 теста): packs не импортируют routers/main/services; services не импортируют routers/main/packs; packs не импортируют друг друга. Текущее состояние чистое, работает в CI через pytest. ✅
- **P19.4 — Тест-инвариант авторизации (дыра)**: `tests/test_auth_invariant.py` (3 теста): все роуты кроме ALWAYS_PUBLIC/PUBLIC_AUTH_PATHS/static возвращают 401/403 без ключа; /health публичен; auth-эндпоинты не получают 503/401/403. ✅
- **P19.5 — Разбить test_pack_system.py (отложить)**: монолит 84 теста после P18; разбить на `tests/packs/test_<pack>.py` без изменения ассертов. ✅ Сделано: 13 per-pack тестов (dns/package_managers/secrets/backup/storage/cicd/monitoring, включая reads_not_blocked и suggestions) перенесены дословно в `tests/packs/test_<pack>.py`; в `test_pack_system.py` остались инфраструктурные (Pack, PackRegistry, singleton, smoke, cross-section). Итого 84 → 71 (core) + 13 (packs), 3744 passed.
- **P19.6 — Плоская структура app/ (исключение)**: 60+ модулей без слоёв; полный переход на Clean Architecture — вне скоупа (legacy «MUST NOT worsen»); новый код класть в `app/services/`.
- **P19.7 — Версии зависимостей (АКТИВИРОВАН, вариант Б)**: ✅ `uv.lock` сгенерирован (92 пакета, universal для 3.11+3.12); `pip-audit` добавлен в dev-extra; CI переведён на `uv sync --frozen --extra dev` + `uv lock --check` (drift-gate) + `uv run`-префикс для ruff/mypy/pytest/pip-audit; `.github/dependabot.yml` (github-actions weekly; pip-экосистема УБРАНА — dependabot не обновляет uv.lock при бампах pyproject → PR всегда падал на `uv lock --check`; Python-зависимости обновляются вручную: `uv lock --upgrade <pkg>` + полный прогон). Docker-образ переведён на frozen install (`uv sync --frozen --no-dev` в Dockerfile, `docker/requirements.txt` удалён как дубль pyproject; psycopg2-binary перенесён в `[project.dependencies]` — нужен для sync-миграций Alembic в образе; сборка + smoke /health проверены). Полный прогон на закреплённом окружении: **3744 passed** (pytest 9.1, paramiko 5.0, cryptography 50, redis 8.1, mypy 2.3 — большие мажорные прыжки без поломок), CI-эквивалент 3724 passed / **72.50%** (гейт 69%), ruff+mypy clean, pip-audit `--strict` — 0 известных уязвимостей.

## 🔒 Аудит безопасности (T79, по architecture-code-rules.md §12) — Aug 2026

Аудит перед запуском на реальный сервер (пользователь: «не хотелось бы быть взломанным школьником с python»). Формат: `[RULE-ID] BLOCKER|MAJOR|MINOR` + `path:line` + фикс. Решение пользователя: чинить **все BLOCKER+MAJOR** сейчас (T79), MINOR — отдельными задачами позже.

### BLOCKER (чинятся в T79)

- **T79.1 [SEC-01] Register first-user-wins = захват свежего деплоя**: `app/auth_middleware.py:276-284` (register в PUBLIC_AUTH_PATHS) + `app/user_auth.py:176-181` (первый зарегистрированный = admin при users_count==0). Нет setup-token; `_register_lock` :173 защищает только от гонки двух регистраций. **Фикс**: env-токен первичной установки (SETUP_TOKEN), регистрация первого админа только с ним. ✅ **ЗАВЕРШЁН**: SETUP_TOKEN-гейт (503 при unset / 403 при неверном) + compare_digest; verify 53 auth/oauth + 3 E2E.
- **T79.2 [SEC-02] Нет TLS в live-деплое — нарушает собственный SECURITY.md**: `docker/docker-compose.live.yml` (gateway → macvlan 10.10.10.145) + `UVICORN_HOST=0.0.0.0` (`app/config.py:12`) → API+Web UI plain-HTTP прямо на LAN: SSH-пароли, private keys, API_KEY, JWT перехватываемы. SECURITY.md: «Run behind TLS». **Фикс**: nginx/caddy+TLS (или только localhost+reverse-proxy) перед шлюзом в live-деплое; SECURITY.md-чеклист в compose. ✅ **ЗАВЕРШЁН**: Caddy TLS-прокси в `docker/docker-compose.live.example.yml` + `docker/Caddyfile.live.example` (tracked); приватный `docker/docker-compose.live.yml` + `docker/Caddyfile.live` (gitignored) — gateway только на internal_net, macvlan только у caddy; compose config OK.
- **T79.3 [SEC-01] OAuth fail-open**: `app/oauth_sso.py:276-281` — пустой `OAUTH_ALLOWED_EMAILS` → `is_email_allowed` = True. С публичным провайдером любой с GitHub/Google-аккаунтом = admin JWT. **Фикс**: при включённом OAuth пустой allowlist = конфиг-ошибка (fail-closed), явный deny. ✅ **ЗАВЕРШЁН**: пустой allowlist → deny-all 403; verify 54 passed.

### MAJOR (чинятся в T79)

- **T79.4 [SEC-01] login/register без rate-limit**: `app/user_auth.py:202/:165`; `Limiter` (`app/security.py:33`, `app/main.py:895`) без `_default_limits` и без RateLimitExceeded-хендлера (при срабатывании лимита 500 вместо 429). **Фикс**: лимиты на auth-эндпоинты + хендлер 429. ✅ **ЗАВЕРШЁН**: register 5/min + login 10/min + структурный 429-хендлер с Retry-After (GRANULARITY); conftest limiter.reset(); verify 78 passed.
- **T79.5 [RBAC] Web-UI JWT → scopes `("*",)`**: `app/auth_middleware.py:398-412` + bypass :553-555 = master-эквивалент (цепочка с T79.1 = полный контроль). **Фикс**: ограниченные scopes для web-ui-идентичностей. ✅ **ЗАВЕРШЁН**: `scopes_for_role(role)` применён в :409/:508; verify 162 passed.
- **T79.6 [DoS] execute без усечения stdout/stderr**: `app/routers/ssh.py:626-631` (execute-argv cap 10MB :754-756 — разнобой) + `ExecuteRequest.command` без `max_length` (`app/models.py:59`). **Фикс**: единый cap вывода + max_length на command. ✅ **ЗАВЕРШЁН**: cap 10MB + max_length 65536; 6 passed.
- **T79.7 [SEC-04] trusted_proxy_cidrs default включает 172.16.0.0/12**: `app/config.py:111-114` — весь Docker-bridge доверен для X-Forwarded-For → подделка XFF обходит allowed_client_cidrs. **Фикс**: сузить default (127.0.0.1/8 или явный конфиг). ✅ **ЗАВЕРШЁН**: default → `127.0.0.1/32,::1/128`; 63 passed.
- **T79.8 [CON] max_sessions_per_ip не исполняется**: `app/config.py:126` — только в конфиг-ответах, в `create_session` нет проверки. **Фикс**: проверка лимита в create_session. ✅ **ЗАВЕРШЁН**: SessionLimitError + гейт в create_session (ДО коннекта, под `self._lock`, skip при source_ip=None) + 429-маппинг; 20 passed pool / 141 combined.

### MINOR (отложены, отдельные задачи)

- ✅ **T79.9 [SEC-02] openapi-claim неточен**: `app/main.py:881-886` — «всё кроме /health+/api/capabilities требует X-API-Key», auth-эндпоинты публичны по дизайну. Фикс: уточнить описание.
- ✅ **T79.10 [log-утечка] JWT в `?token=` WS-query**: `app/auth_middleware.py:492` — попадает в access-логи. Фикс: cookie/header вместо query.
- ✅ **T79.11 [CON] rate_limit_requests/window — мёртвый конфиг**: `app/config.py:127-128` не читается (лимиты хардкожены в декораторах). Фикс: подключить конфиг или удалить.
- ✅ **T79.12 [SEC-03-смеж.] command_output_redaction_enabled default False**: `app/config.py:62-64`, `app/output_redaction.py:10` — вывод команд с секретами не редактируется по умолчанию. Фикс: default True.
- ✅ **T79.13 [CON] .env.example placeholder'ы без валидации**: `API_KEY=change-me-...`, `JWT_SECRET=change-me-...` — деплой с примером = известные секреты. Фикс: валидация в config (reject change-me).
- ✅ **T79.14 [ERR] `str(exc)` в detail**: `app/main.py:980-994` (SSHManagerError), `app/routers/oauth.py:80/:136`; эхо `input_val` `app/main.py:1013-1014` — утечка внутренних деталей. Фикс: generic detail + полный текст в лог.
- ✅ **T79.15 [DoS] check-port без rate-limit**: `app/routers/ssh.py:1137`. Фикс: rate_limit.
- ✅ **T79.16 [DoS] PTY WS без лимитов input/size**: `app/routers/ssh.py:1103-1118`. Фикс: лимиты на размер сообщений.
- ✅ **T79.17 [тайминг] Энмумерация логинов**: `app/user_auth.py:206-212` — bcrypt только для существующих юзеров. Фикс: dummy-bcrypt для несуществующих.
- ✅ **T79.18 [SEC-03-смеж.] JWT в localStorage Web UI**: `app/routers/oauth.py:169-178`. Фикс: httpOnly-cookie (архитектурное изменение, отложить).

### Подтверждено (PASS)

Пустой API_KEY → fail-closed 503 (auth_middleware.py:327-338); SSRF-защита (connect/prewarm/check-port); constant-time compare; командная граница command_gate+shlex; credentials SecretStr+Fernet; audit-log command_root-only; strict_host_key default True → RejectPolicy; docker-compose.yml ports 127.0.0.1-only + read_only+cap_drop ALL+no-new-privileges; event hooks HTTPS-only+HMAC+redact+64KB cap; upload_ssh_key master-only+64KB+resolve+chmod600; password policy 8+upper+digit+special; OAuth PKCE S256+state TTL600; **pip-audit `--strict` — 0 известных уязвимостей**.

## 🔒 Аудит безопасности (T80, follow-up после 106 коммитов новых фич) — Aug 2026

Независимый аудит новых подсистем, добавленных после T79 (RBAC, OAuth SSO, connection pooling, persistent audit log, Web UI, jobs API) — не покрытых T79. Формат тот же. Три параллельных под-аудита (Web UI; OAuth+RBAC; pooling+audit-log) + собственная проверка автора.

### BLOCKER (исправлены)

- **T80.1 [AUTH] SSH connection pool — обход аутентификации**: `app/ssh_manager.py:305-306` (было) — `pool_key = (host, port, username, auth_method)`, без материала credential. При pool hit (:334) весь блок проверки пароля/ключа (`client.connect(...)`) пропускался целиком. Любой вызывающий, знающий/угадавший host+port+username с уже запуленным соединением, получал чужую аутентифицированную сессию, предъявив **любой** пароль. Активно только при `SSH_CONNECTION_POOL_SIZE > 0` (default 0/выкл), но дыра реальна для любого включённого мульти-тенантного деплоя. **Фикс** (`b48df22a`): `pool_key` теперь включает one-way SHA-256 fingerprint предъявленного credential (`_credential_fingerprint`) — разный пароль/ключ = разный ключ пула = промах, обычная аутентификация. 2 regression-теста.
- **T80.2 [IDOR] jobs.py — отсутствие per-owner авторизации**: только `jobs_wait` проверял владение (`identity_sub == job.owner_id`); `GET /api/jobs`, `GET/POST /api/jobs/{id}/{status,result,cancel,stream,events}` — только scope-проверка, без владения. Любой вызыватель с `jobs:read` видел **все** джобы всех тенантов (включая stdout/stderr с секретами), мог отменять чужие running-джобы (DoS), live-стримить чужой вывод. `POST /api/jobs/run` и `POST /api/bulk/execute` вообще не проверяли владение session_id — можно было выполнять команды в чужой SSH-сессии. **Фикс** (`e856dd1a`): новый `rbac.job_visible_to()` (по образцу `session_visible_to`) + `_get_owned_job()` во всех read/cancel/stream эндпоинтах, фильтрация `jobs_list`, `ensure_session_owner()` в `jobs_run`/`bulk_execute`. Попутно: `jobs_events`-алиас не передавал свою identity в `jobs_stream` (второй мелкий баг, тот же фикс). Юнит + router-level regression тесты.
- **T80.3 [RBAC] OAuth SSO — все логины получают role=admin**: `app/routers/oauth.py:160` (было) — `create_jwt(username=..., user_id=uid)` без `role=`, наследует admin-дефолт `create_jwt()`. В отличие от local register() (fail-safe: только один админ по конструкции), SSO мультипользовательский по дизайну (любой email из allowlist) — вся система ролей RBAC (operator/viewer/custom) была недостижима через SSO, каждый allowlisted email получал master-эквивалент. **Фикс** (`12e88541`): новый `OAUTH_DEFAULT_ROLE` (default `operator`, НЕ `admin`), невалидные значения → fail-safe откат на `operator`. 3 regression-теста.

### MAJOR (исправлено)

- **T80.4 [AUTH] Коллизия OAuth uid между провайдерами**: `app/routers/oauth.py:157` (было) — `uid = sha256(subject)[:4]` без имени провайдера. Два разных провайдера с одинаковым raw `sub`/`id` (частое дело для маленьких/последовательных числовых id) схлопывались в один gateway uid — один реальный пользователь неотличим от другого. **Фикс** (`12e88541`): хэш `f"{provider}:{subject}"`. 1 regression-тест.

### T80.5 — CI-флейк: WebSocketDisconnect в WS-тестах

Обнаружено при разборе двух неожиданных провалов CI на несвязанных коммитах (один — чисто docs). История прогонов Gitea Actions за 30 июля — 4 августа показала 4 отдельных провала с одинаковой сигнатурой `starlette.websockets.WebSocketDisconnect` в трёх разных тестах (`test_websocket_authorization.py::test_readonly_policy_denies_systemctl`, `::test_execute_stream_allowed_with_correct_scope`, `test_c3_command_policy_contract.py::test_denied_command_returns_policy_denied_code`), на обеих версиях Python.

**Расследование**: не воспроизводится локально (30/30 повторов конкретного теста, 5/5 прогонов всех WS-тестов вместе). Механизм очереди Starlette `WebSocketTestSession` (`anyio.create_memory_object_stream` с бесконечным буфером) гарантирует FIFO-порядок сообщений — гипотеза «гонка между send и close» не подтвердилась при разборе исходников testclient.py. `AuditEventLogger.append()` (вызывается перед отправкой ответа во всех трёх упавших тестах) — уже exception-safe (try/except OSError + catch-all). Похоже на ресурсно-зависимую нестабильность именно shared CI-раннера (2 CPU/6GB на джобу), не воспроизводимую на менее нагруженной машине; точный корневой механизм внутри Starlette/anyio не установлен с уверенностью.

**Фикс — целевой retry, не общее подавление**: добавлен `pytest-rerunfailures` (dev-зависимость, `uv.lock` обновлён). CI: `--reruns 2 --reruns-delay 2 --only-rerun 'WebSocketDisconnect'` — повторяет **только** тесты, упавшие именно с этой сигнатурой; настоящая регрессия в любом другом тесте по-прежнему валит сборку с первой попытки.

### Отложено / не исправлено (сознательно)

- **Redis dead-letter queue без owner-фильтрации** (`GET /api/jobs/queue/dead`) — нет концепции owner_id, но `redis_queue.enqueue()` не вызывается вообще ни из одного места в кодовой базе — очередь всегда пуста на практике, не активный вектор.

### T79.10 — доведён до конца (по запросу пользователя)

Изначально оставлен как «остаточный риск» — заявленная угроза (утечка в access-логи) была покрыта `_RedactTokenFilter`, но `?token=` в WS-хендшейке всё равно проверялся РАНЬШЕ httpOnly cookie, вопреки собственному комментарию в коде («cookie is the primary channel»). Реальный браузерный Web UI (`app.js`) уже не отправляет `?token=` вовсе — только cookie (браузер сам прикладывает её к WS-хендшейку, как к любому same-origin запросу). **Фикс** (`app/auth_middleware.py`): порядок проверки поменян — cookie теперь первична, `?token=` — fallback только для нехроможих клиентов без поддержки cookie/заголовков (задокументированное поведение `tests/test_webui.py` сохранено, не сломано). В обычном браузерном случае query-ветка теперь вообще не достигается — остаточный риск (утечка в историю браузера/логи промежуточных прокси) снят для реального трафика. 1 новый regression-тест: валидная cookie + мусорный `?token=` → аутентификация проходит по cookie.

### T80.6 — Timing-баг в consent-пароле + 3 CVE в отдельном venv MCP-коннектора

Найдено при финальном самостоятельном аудите готовности. `examples/mcp_client_remote/server.py` (`consent_handler`) сравнивал `MCP_AUTHORIZE_PASSWORD` через `!=` вместо constant-time — timing side-channel на общий пароль consent-экрана публичного OAuth. **Фикс**: `secrets.compare_digest`. Отдельно: собственный venv MCP-коннектора (свой `requirements.txt`, не `uv.lock`, никогда не проверялся `pip-audit`) содержал 9 известных уязвимостей — `cryptography` (CVE-2026-69247), `mcp` (PYSEC-2026-3483), `pydantic-settings` (GHSA-4xgf-cpjx-pc3j), `pip`. **Фикс**: обновлено + запинено `mcp>=1.28.1,<2.0.0` (внимание: голый `mcp` без верхней границы резолвится в `2.0.0`, несовместимую с FastMCP по коммиту `05122780`), `cryptography>=50.0.0`, `pydantic-settings>=2.14.2` в обоих requirements.txt (`mcp_client_remote` + `mcp_server`). Живой venv обновлён, сервис перезапущен, `pip-audit --strict` → 0 known vulnerabilities (было 9).

### Метод

3 параллельных суб-аудита (Web UI: session manager/file browser/terminal — чисто, кроме T80.2; OAuth+RBAC: T80.3+T80.4, остальное подтверждено PASS; pooling+audit log: T80.1, audit_store.py — чисто, SQLi/DoS/auth не найдено) + собственная проверка автора (uv sync --frozen, pytest 3915 passed, ruff+mypy clean, pip-audit --strict — 0 known vulnerabilities).

## 📋 T81 — Backlog после T79+T80 (по важности)

Найдено при разборе «что осталось несовершенным» после T80. Порядок — от важного к неважному; работаю сверху вниз.

1. ✅ **[СИСТЕМНО] `examples/mcp_server` и `examples/mcp_client_remote` вне периметра CI** — `pyproject.toml: testpaths = ["tests"]` не видит их вообще; ни ruff, ни mypy, ни pip-audit, ни один тест не проверяет эту ветку кода в CI. Именно поэтому T80.6 (timing-баг + 9 CVE) просидел незамеченным до ручного аудита. **Реальная причина не «нет CI-шага», а «отдельный, вручную управляемый venv»**: `examples/mcp_client_remote/.venv` жил своей жизнью, никогда не пересобирался через `uv`, дрейфовал от актуальных версий в `pyproject.toml`/`uv.lock` (где `mcp>=1.28.0,<2.0.0`, `cryptography`, `pydantic-settings` уже были правильно запинены и покрыты pip-audit — просто этот venv их не использовал). **Фикс**: все 5 живых systemd-сервисов (`agent-ssh-gateway-mcp`, `agent-mcp-docker`, `agent-mcp-context7`, `agent-mcp-gitea`, `agent-mcp-github`) переведены на общий `../../.venv` (главный uv-managed venv — все нужные пакеты там уже были, проверено импортом перед переключением); отдельный `mcp_client_remote/.venv` больше не источник правды (оставлен на диске неудалённым, requirements.txt помечены как deprecated). `examples/mcp_client_remote` добавлен в ruff+mypy CI команды (`.github/workflows/ci.yml`) — pip-audit отдельно не нужен, теперь покрывается основным шагом `pip-audit --strict .`, так как зависимости общие. `agent-mcp-postgres.service` использует отдельный `/media/1TB/Anaconda/bin/python3` — вне скоупа этого фикса, не тронут.
2. **`gateway-notifier` в вечном restart-loop** — сервис корректно завершается как «disabled», но docker `restart:` политика гоняет его по кругу бесконечно (шумно в логах, не опасно). **Фикс**: не запускать контейнер вовсе, когда фича выключена (compose profile или entrypoint-guard), либо `restart: no`.
3. **`build_sha`/`build_time` всегда "unknown" в `/health`** — нет способа узнать, какой коммит реально задеплоен, без ручного `git log` на хосте. **Фикс**: прокидывать `BUILD_SHA`/`BUILD_TIME` как build-args в Dockerfile из CI.
4. **Redis dead-letter queue без owner-фильтрации** (`GET /api/jobs/queue/dead`) — не активный вектор (`enqueue()` не вызывается), но структурно не защищён по образцу `job_visible_to()`. **Фикс**: применить тот же паттерн, что и в T80.2, на случай если очередь когда-нибудь подключат.
5. **Слабая валидация путей в файловом браузере Web UI** (`app/security.py: validate_path()` — только substring-проверка `..`/`~`, не `resolve()+relative_to()` как везде в остальном коде) — не эксплуатируется (нет sandbox-границы, работает через уже аутентифицированную SSH-сессию с полным доступом), но стилистически не согласовано с остальной базой.
6. **Координация по `MCP_SCOPE_ENFORCEMENT`/`MCP_CLIENT_SAFE_MODE`** — переключены на живом сервисе (audit→enforce, safe_mode=true) кем-то из другого агента без явной синхронизации; не баг, но стоит подтвердить, что это осознанное и протестированное решение.
