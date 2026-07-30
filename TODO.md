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

### P17 — Suggestions при блокировке
Уже есть в DestructivePattern.suggestions (все пакеты).
Отображается в ответе policy evaluation.

### P18 — Больше pack-паттернов (K8s, AWS, GCP, БД, DNS...)
У DCG ~50 доменов, у нас ~10. Постепенное расширение по необходимости.
