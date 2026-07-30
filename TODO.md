# Agent SSH Gateway — TODO

## ✅ Done (Phases 1-5f + DCG P1-P2)
- Phase 1: Docker/Compose destructive patterns (9+4)
- Phase 2: gateway_scan_command MCP tool
- Phase 3: Filesystem destructive patterns (10)
- Phase 4: Kubernetes destructive patterns (21)
- Phase 5: Cloud destructive patterns (52 — AWS/GCP/Azure)
- Phase 5b: Database destructive patterns (39 — PG/MySQL/SQLite/Mongo/Redis)
- Phase 5c: Git destructive patterns (16)
- Phase 5d: Firewall destructive patterns (13 — iptables/ufw/nftables)
- Phase 5e: Loadbalancer destructive patterns (30 — nginx/haproxy/traefik/ELB)
- Phase 5f: System destructive patterns (54 — disk/permissions/services)
- P1: Heredoc scanning — 2-tier detection (T1 triggers + T2 extractors), integrated into policy engine, 52 tests
- P2: DecisionMode Ask — operator approval flow with in-memory store, ASK mode in policy engine, 202 + approval_id API response, 19 tests
- P3: Agent profiles — TrustLevel, built-in profiles (chatgpt/claude-code/codex-cli/aider/cursor), env+proc detection, effective_packs(), 14 tests
- **Total: 204 destructive patterns, 3388 tests**

---

## 🔜 DCG-Inspired Features (приоритет)

### P1 — Heredoc scanning
DCG has 3-tier heredoc detection (src/heredoc.rs):
- Tier 1: Trigger detection (`<<`, `python -c`, `bash -c`, `eval`, `exec`, `$()`)
- Tier 2: Content extraction (inline scripts, herestrings, heredocs)
- Tier 3: AST analysis (future in DCG too)

**Why**: GPT-агенты постоянно шлют `python -c "..."` и `bash -c "..."` — метахар-проверка это пропускает. Нужно извлекать и сканировать вложенные команды.

**Files**: `app/heredoc_scanner.py` (new), `app/command_policy.py` (integrate into `_evaluate_enforce_decision`)
**Tests**: `tests/test_heredoc_scanner.py` (new)

### P2 — DecisionMode Ask (operator approval)
Add `ask` mode between deny and allow:
- Policy says "deny by default" + ASK mode = ask operator via Telegram
- Severity-based: Critical=deny, High=ask+TTL, Medium=warn, Low=log
- `CommandPolicyDecision` gets `decision_mode` field
- Наш access-control flow (confirm_operation) уже есть — связать

**Files**: `app/command_policy.py` (DecisionMode enum, evaluate), `app/policy_ask.py` (callback/TTL)
**Tests**: `tests/test_policy_ask.py`

### P3 — Agent profiles
DCG-style agent profiles instead of flat `chatgpt_safe`:
- `TrustLevel`: high/medium/low
- per-agent: disabled_packs, extra_packs, additional_allowlist
- Detection: env vars (CLAUDE_CODE, CODEX_CLI), env name

**Files**: `app/agent_profiles.py` (new), `app/command_policy.py` (integrate profile lookup)
**Tests**: `tests/test_agent_profiles.py`

### P4 — Pack system refactor
Current `command_policy.py` has flat tuples. DCG has proper Pack struct:
- `Pack`: id, keywords, destructive_patterns, safe_patterns
- `PackRegistry`: lazy-loaded packs, keyword-based quick reject
- `PackEntry`: metadata + builder fn

**Why**: Модульность, быстрый reject, возможность включать/выключать паки per-agent.

**Files**: `app/packs/` (directory with pack modules), `app/pack_registry.py`
**Tests**: `tests/test_pack_registry.py`

### P5 — MCP tools
Add 2 more MCP tools (DCG-style):
- `gateway_scan_file` — scan a file for destructive commands (like DCG `scan_file`)
- `gateway_explain_pattern` — explain a pattern by rule_id (like DCG `explain_pattern`)

**Files**: `app/mcp_tools.py` or `server.py`
**Tests**: `tests/test_mcp_tools.py`

### P6 — Allowlist layers
DCG has 4-layer allowlist: Agent > Project > User > System
- Hierarchical with TTL/expiration/session
- Selectors: rule_id, exact command, prefix, regex

**Files**: `app/allowlist.py` (new)
**Tests**: `tests/test_allowlist.py`

---

## 📋 Прочие задачи

### QoL
- [x] `project_scan_destructive` tool (scan git repo for destructive patterns — DCG-style)
- [ ] Suggestions в ответе при блокировке ("use chmod 755 instead of 777")
- [ ] Confidence scoring (regex vs AST weight)
- [ ] SARIF/JSON output for scan tool
- [ ] Policy mode per-agent (not global)

### CI/DevOps
- [ ] Gitea CI: coverage threshold (70% minimum)
- [ ] Gitea CI: test count sentinel (like DCG's 3700 min)
- [ ] pip-audit step in CI (currently failing in kojo, may need same fix)

### Docs/Examples
- [ ] MCP server README with tool examples
- [ ] command_policy.py module docstring with architecture overview
- [ ] Примеры ask-mode flow через Telegram
