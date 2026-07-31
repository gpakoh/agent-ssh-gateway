# Agent SSH Gateway — TODO

## ✅ Done

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
  - Контракт: 3744 passed, ruff clean. Осталось (след. шаг P19.2): `routers/files.py` / `routers/context.py` — вынос бизнес-логики в сервисы.
- **P19.3 — Чекер границ импортов (исправить)**: ARC-31 рекомендует лёгкий AST/import-чекер первым шагом. `tests/test_architecture_boundaries.py` (3 теста): packs не импортируют routers/main/services; services не импортируют routers/main/packs; packs не импортируют друг друга. Текущее состояние чистое, работает в CI через pytest. ✅
- **P19.4 — Тест-инвариант авторизации (дыра)**: `tests/test_auth_invariant.py` (3 теста): все роуты кроме ALWAYS_PUBLIC/PUBLIC_AUTH_PATHS/static возвращают 401/403 без ключа; /health публичен; auth-эндпоинты не получают 503/401/403. ✅
- **P19.5 — Разбить test_pack_system.py (отложить)**: монолит 84 теста после P18; разбить на `tests/packs/test_<pack>.py` без изменения ассертов.
- **P19.6 — Плоская структура app/ (исключение)**: 60+ модулей без слоёв; полный переход на Clean Architecture — вне скоупа (legacy «MUST NOT worsen»); новый код класть в `app/services/`.
- **P19.7 — Версии зависимостей (отложить)**: security-раннер (trivy/pip-audit) уже в CI; локально не пинать.
