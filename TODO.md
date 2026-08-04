# Agent SSH Gateway — TODO

## 🔍 T82 — Аудит зон, не пройденных сегодняшним seam-аудитом

Контекст: сессия seam-аудита (после T79/T80/T81) нашла реальные, часто серьёзные
баги буквально в каждой зоне, которую проверяли по-настоящему (реальными
объектами / живым трафиком, не моками) — ядро SSH-шлюза, все 5 fleet MCP-адаптеров
(Gitea/GitHub/Postgres/Docker/Context7), основной OAuth-прокси. Список ниже —
зоны, которые в этот аудит **не попали вообще**. Порядок — по риску/вероятности
живых багов, иду сверху вниз.

1. ✅ **Web UI (фронтенд)** — `appendLine(content, 'system')` рендерил через
   unescaped `innerHTML` — self-XSS, тот же класс риска, что и `/oauth/consent`.
   Исправлено (`escapeHtml()`), e2e-тест на реальном headless Chromium
   (`test_append_line_system_type_escapes_html`), задеплоено. Commit `57a3b8a0`.
2. ✅ **SDK** (`sdk/session.py`) — `AsyncGatewaySession` был гарантированным
   `TypeError` с любым реальным клиентом (`await` на синхронный `GatewayClient`);
   собственные тесты мокали `AsyncMock()`, скрывая ровно это несоответствие.
   Исправлено через `asyncio.to_thread`, тесты переписаны на `MagicMock` +
   добавлен end-to-end тест на настоящем `GatewayClient`. Commit `0e603e79`.
3. ✅ **`scripts/` в целом** — `mcp_client_runtime_preflight.py`: лишняя запятая
   превращала третий аргумент `check()` (detail-сообщение) в отдельное
   отброшенное tuple-выражение — diagnostic detail (`value=...`/`missing`)
   молча терялся. Существующий тест проверял только наличие label, не detail —
   не поймал бы баг. Исправлено, добавлены 2 регрессионных теста (проверены на
   падение против до-фикс кода). Commit `ee5c6d77`.
4. **Непроверенные роутеры** — структурно тронуты в P19 (вынос в сервисы), но
   не seam-аудированы на реальные баги в этой сессии: `admin_access.py`,
   `admin_approval.py` (ASK-mode approval flow — auth-смежный риск),
   `event_hooks.py` (доставка вебхуков, HMAC-подпись — тот же класс, что утечка
   URL у Gitea/GitHub), `git.py`, `servers.py`, `snapshots.py`, `templates.py`,
   `system.py`, `diagnostics.py`, `logs.py`, `code.py`, `project_inspection.py`,
   `search_replace.py`, `batch.py`, `webhooks.py`.
5. **Redis job queue / dead-letter** — T81.4 добавил owner-фильтрацию в
   предположении, что `redis_queue.enqueue()` нигде не вызывается (очередь
   всегда пуста на практике). Проверить, что это предположение всё ещё верно;
   если что-то теперь реально кладёт туда джобы — весь dead-letter путь нужно
   заново проверить живьём.
6. **Agent handoff / worktree** (`examples/mcp_server/handoff.py`,
   `agent_tasks.py`, `opencode_runner_wrapper.py`, `mimo_tools.py`,
   `opencode_tools.py`) — не тронуто в этой сессии вообще.
7. **`app/services/*.py`** (вынесены в P19.2: `file_editing.py`,
   `project_patch.py`, `context_editing.py`, `scaffolding.py`,
   `project_structure.py`) — рефакторены ради структуры, не seam-тестированы
   на реальные баги с тех пор.
