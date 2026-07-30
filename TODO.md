# Agent SSH Gateway — TODO

## ✅ Done

- Task tracking (old P1-P6, QoL, CI/DevOps) — все сделано и закоммичено
- **P7 Context Detection** — `app/context.py`, 23 тестов, span-классификация команд, known-safe wrappers
- **P8 AST Matching** — `app/ast_matcher.py`, 17 тестов, stdlib ast для Python, regex fallback
- **P9 Confidence v2** — `app/confidence.py`, 18 тестов, span-aware, сигналы (executed/data/comment/boosters)
- **P10 Simulate Mode** — `app/simulate.py`, 15 тестов, `gateway_simulate` MCP tool, три формата (plain/hook_json/decision_log)
- **P11 Suggest Allowlist Clustering** — `app/suggest.py`, 55 тестов, Jaccard-кластеризация, генерация паттернов, safety filter
- **P12 SQLite History** — `app/history.py`, 35 тестов, SQLite-логирование решений, batch write, prune, статистика

## 🔜 DCG-Inspired Features (to port)

### ~~P7 — Context Detection (span-level command classification)~~ ✅ Done
DCG `src/context.rs` (4124 строк)

`app/context.py` — shell-парсер, SpanKind (EXECUTED/DATA/COMMENT/ARGUMENT/...),
known-safe wrappers, `check_context_filter()`, `compute_span_confidence()`.

---

### ~~P8 — AST Matching for Embedded Code~~ ✅ Done
DCG `src/ast_matcher.rs` (4297 строк)

`app/ast_matcher.py` — stdlib `ast` для Python, regex для bash/javascript/ruby/typescript.
`_PYTHON_MODULE_FUNC` dict: (module, func) → severity. Импорты отслеживаются через AST Walk.
17 тестов.

---

### ~~P9 — Confidence Scoring v2 (span-aware)~~ ✅ Done
DCG `src/confidence.rs` (419 строк)

`app/confidence.py` — ConfidenceSignal (10 видов), `compute_match_confidence()`,
`compute_span_confidence()`. Интегрирован в `Pack.check()` → confidence каждого
DestructiveMatch учитывает span-контекст, операторы рядом, позицию команды.
18 тестов.

---

### ~~P10 — Simulate Mode (command replay)~~ ✅ Done
DCG `src/simulate.rs` (1819 строк)

`app/simulate.py` — пропускает лог команд через policy engine для тестирования.
Три формата: plain text, hook JSON, DCG decision log.
`gateway_simulate` MCP tool.
15 тестов.

---


