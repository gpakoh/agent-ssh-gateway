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
4. ✅ **Непроверенные роутеры** — прошёл все 15. Реальные баги, все исправлены
   с regression-тестами (проверены на падение до фикса), задеплоено:
   - `admin_access.py`: `ttl_seconds=0` в decision endpoint тихо заменялся
     дефолтным TTL (`or` вместо `is not None`). Commit `e6ed8bf5`.
   - `event_hooks.py`/`event_hook_delivery.py`: SSRF — `validate_webhook_url()`
     проверяет только literal-IP хосты, хостнейм, резолвящийся в
     127.0.0.1/169.254.169.254/RFC1918, никогда не перепроверялся перед
     реальным исходящим запросом. `validate_destination_ip()` существовал,
     но был dead code. Commit `846d07b8`.
   - `git.py`: `recovery_backup`/`recovery_restore` звали
     `assert_workspace_writable()` без аргументов — блок работал, но аудит-
     событие терял actor_fingerprint/route. Commit `ef0d0510`.
   - `diagnostics.py`: `POST /api/session/check` вообще не звал
     `ensure_session_owner` (в отличие от идентичного по смыслу
     `GET /api/ssh/session/{id}/health`) — любой agent-токен с
     `diagnostics:read` мог узнать статус ЧУЖОЙ сессии по id. Commit `bbb40637`.
   - `batch.py`: `/api/batch/execute` вообще не звал
     `assert_workspace_writable()` — mutating-операции (edit/create/delete/
     rename/copy) полностью игнорировали `WORKSPACE_READONLY=true`.
     Commit `97e6d133`.
   - `servers.py`, `snapshots.py`, `templates.py`, `system.py`, `logs.py`,
     `code.py`, `project_inspection.py`, `search_replace.py`,
     `admin_approval.py`: чисто, без новых находок.
   - **Не исправлено, требует решения пользователя**: `webhook_manager.py`
     (роутер `webhooks.py`, отдельная от `event_hooks.py` старая система
     "webhook → auto-deploy") — `handle_webhook()` содержит комментарий
     "Verify Secret (simple Check) — In Production, Use HMAC Signature
     Verification" и **никогда фактически не проверяет secret**. Сейчас не
     эксплуатируемо: `handle_webhook()` не подключён ни к одному роутеру
     (dead code — нет входящего endpoint'а для приёма реального GitHub/Gitea
     webhook). Решить: реализовать нормальную HMAC-проверку и подключить
     receiver, или убрать функциональность целиком, если не используется.
5. ✅ **Redis job queue / dead-letter** — перепроверено: `RedisJobQueue.enqueue()`
   до сих пор нигде не вызывается в production-коде (`grep -rn "\.enqueue("` по
   `app/` находит только `event_hook_emitter.py`'s `ds.enqueue()` —
   `DeliveryService` из `event_hooks.py`, не связанный класс). T81.4's
   предположение подтверждено, всё ещё верно. `jobs.py`'s
   `get_queue_stats()`/`get_dead_letter_jobs()` — read-only, всегда пусто на
   практике; сам `RedisJobQueue` построен полностью (persist/retry/dead-letter),
   но никогда не подключён к реальному пути исполнения джобов
   (`_state.job_manager` — другой, отдельный in-process менеджер). Не баг,
   просто неиспользуемая фича — как и `webhook_manager.py` (см. пункт 4).
6. ✅ **Agent handoff / worktree** — прошёл все 5 файлов. Реальные баги
   исправлены с regression-тестами:
   - `agent_tasks.py`: `read_agent_task_file()`'s `filename` интерполировался
     в shell-команду без вообще какой-либо валидации/escaping — рядом лежал
     неиспользуемый `_shell_escape()` helper, явно предназначенный для этого,
     но никогда не применённый. Все текущие вызовы (`server.py`) передают
     hardcoded literal, поэтому сейчас не эксплуатируемо, но сама функция
     была минным полем для любого будущего вызывающего. Добавлен
     `validate_filename()`. Commit `ed9de99f`.
   - `scripts/opencode_runner_wrapper.py`: `resolved_cmd.split()[1:]` —
     наивный split по пробелам без понятия о кавычках — дефолтная
     авто-сгенерированная команда содержит quoted multi-word prompt-аргумент,
     который наивный split разбивал на десятки отдельных токенов вместо
     одного аргумента. Заменено на `shlex.split()` (вынесено в
     `build_opencode_args()`). Не было покрыто тестами: весь файл тестов
     skip'ается без реального opencode-бинаря, а единственные проходящие
     тесты используют dry_run, который возвращается раньше, чем код с багом
     вообще выполняется — новый тест лежит в отдельном файле без skip.
     Commit `ed9de99f`.
   - `handoff.py`, `mimo_tools.py`, `opencode_tools.py`: чисто, без новых
     находок (`resolve_file_path` уже блокирует traversal корректно; mimo/
     opencode tools — намеренно hard-blocked stubs).
7. **`app/services/*.py`** (вынесены в P19.2: `file_editing.py`,
   `project_patch.py`, `context_editing.py`, `scaffolding.py`,
   `project_structure.py`) — рефакторены ради структуры, не seam-тестированы
   на реальные баги с тех пор.
