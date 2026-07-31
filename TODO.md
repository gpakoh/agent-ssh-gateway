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
- **secrets** (Vault: vault kv delete, delete secret; aws secretsmanager delete-secret; gcp secrets delete)
- **backup** (restic forget, borg delete/prune, restic remove, duplicity remove-older-than)
- **monitoring** (promtool delete series, influx delete, graphite whisper delete)
- **storage** (lvm lvremove, mdadm, zfs destroy, parted rm, fdisk delete)
- **cicd** (gh run delete, gitlab pipeline delete, circleci orphan-delete)
