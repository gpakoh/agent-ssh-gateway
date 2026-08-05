# Agent SSH Gateway — TODO

## 🔍 T84 — Третий круг: app/services повторно, с новым паттерном из T83

Контекст: T83 нашёл паттерн "glob()/rglob() следует symlink, если паттерн
явно называет symlink-сегмент" (`search.py`, `scan_project.py` в
`app/workspace/`). Этого паттерна не было в голове во время T82's прохода
по `app/services` — перепроверил все 7 файлов заново с этим паттерном.

Найдено: ✅ **`project_search.py`** — `root_path.rglob(iter_pattern)`,
тот же баг, что в `workspace/search.py`/`scan_project.py`. Эмпирически
подтверждено (regression-тест падает до фикса). Не подключено ни к
одному router (`search_text()` нигде не импортируется — живой search
endpoint использует `workspace/search.py`, уже исправлен) — то же
orphaned-feature, что и `scan_project.py`/`SnapshotStore`. Исправлено
всё равно. Commit `237c517f`.

Остальные 6 файлов (`file_editing.py`, `command_gate.py`,
`project_structure.py`, `context_editing.py`, `scaffolding.py`,
`project_patch.py`) — без новых находок. `project_structure.py` работает
через SSH с `shlex.quote()`, локальный диск вообще не трогает — не
подвержен этому классу бага структурно.

## 🔍 T83 — Второй круг: app/workspace/* и patch_apply.py

Контекст: T82 нашёл path traversal в `project_patch.py` (`app/services/`) —
`f["path"]` из тела патча резолвился за пределы project root без проверки,
доступно со scope `project:patch` (не master key). `app/workspace/*` —
пакет, который РЕЗОЛВИТ project root и project:read/write scope для всей
остальной системы (`policy.py`, `registry.py`), то есть ровно тот же класс
риска, только на уровень выше. Не аудирован вообще. Плюс сам алгоритм
применения патчей (`patch_apply.py`) — раньше проверялись только
traversal/forbidden-ops, не корректность хуnков.

1. ✅ **`app/workspace/policy.py`** — `validate_write()` сама по себе (без
   парной `_symlink_safe_preflight()`, которую каждый текущий вызывающий
   добавляет отдельно) пропускала symlink-escape через ещё-не-существующий
   промежуточный путь (symlink не непосредственный parent, а предок выше).
   Сейчас не эксплуатируемо (все реальные вызывающие уже парят обе
   проверки), но собственный docstring модуля заявляет symlink-safety,
   которой не было при использовании класса отдельно — тот же паттерн,
   что обернулся реальным багом ниже. Консолидировано в
   `_check_no_symlink_components()`. Заодно: `SECRET_FILE_PATTERNS` для
   `id_rsa`/`id_ed25519`/`id_ecdsa` — точное совпадение без wildcard
   (в отличие от `*.pem`/`*.key`) — `id_rsa.bak` проходил незамеченным.
   Commit `c43eaa8c`.
2. ✅ **`app/workspace/registry.py`** — чисто. `validate_read` (используется
   для tree browsing) уже symlink-safe; `_build_tree` явно не рекурсирует
   в symlink-директории.
3. ✅ **`app/workspace/edit.py` + `files.py`** — чисто. Оба правильно парят
   `validate_write`/`validate_read` с `_symlink_safe_preflight`;
   `project_apply_patch` берёт путь из явного параметра, а не из тела
   патча (в отличие от `services/project_patch.py`), избегая всего класса
   traversal-бага структурно.
4. ✅ **`git.py`** — чисто, эталонно (fixed argv, `shell=False`, `--`
   separator). **`snapshot.py`** — `WorkspaceAuditLogger`'s in-memory cap
   (`_max_in_memory`) тихо становился `0` при заданном `log_path`, отключая
   cap-check вовсе (`if 0 and ...` всегда falsy) — unbounded memory growth
   для long-running процесса. Не подключено ни к одному REST endpoint
   (как `RedisJobQueue`), но баг реальный и дёшево чинится. Commit `b3d203bb`.
5. ✅ **`search.py` — SERIOUS, живой, эксплуатируемый info-disclosure.**
   `Path.glob()` следует symlink, когда паттерn явно называет
   symlink-сегмент (`file_glob="escape_link/*"`), даже не спускаясь в них
   для `**`. Единственная проверка (`relative_to(project_root)`) чисто
   строковая — не ловит это. Подтверждено живьём: `GET
   /api/workspace/projects/{id}/search` с `file_glob`, называющим
   существующий symlink, возвращал реальный контент (с previews) СНАРУЖИ
   project root. Обычный symlink в проекте (node_modules/.bin, venv,
   deploy "current" — не экзотика) + `project:read` scope = exfiltration
   произвольных файлов, которые может прочитать процесс. Исправлено —
   resolve + containment check перед чтением контента. Commit `d4e1175b`.
   `preview.py`, `receipts.py` — чисто (read-only, уже symlink-safe).
6. ✅ **`scan_project.py`** — тот же баг, что в `search.py` (`root.rglob(pattern)`).
   Не подключено ни к одному router/tool (orphaned, как и `SnapshotStore`),
   но тот же баг, тот же двухстрочный фикс — исправлено. Commit `8d4f5055`.
   `tools.py`, `models.py`, `__init__.py` — чисто (чистые re-export/data
   structures).
7. ✅ **`app/patch_apply.py` — silent data corruption, живой endpoint.**
   `_apply_in_memory()` никогда не проверял, что context/removed строки
   хуnка реально совпадают с текущим содержимым файла — просто доверял
   `hunk.source_start` и слепо удалял/вставлял. `expected_hashes` —
   опционален и только на уровне всего файла. Эмпирически подтверждено:
   patch, ожидающий "bbb" на строке 2 при реальном "ZZZ", молча произвёл
   "aaa\nBBB_NEW\nccc\n" — удалив реальный контент без единой ошибки. Это
   transactional-write путь за `POST /api/projects/{project}/apply-patch`
   (scope `project:patch`, не master key) — живой, достижимый endpoint.
   Добавлен `_check_line_matches()`, вызывается для каждой context/removed
   строки. Commit `b156c44d`.

## Итог T83

Все 7 пунктов закрыты. Самая серьёзная находка — **живой symlink-escape
info-disclosure в `project_search_text`** (не требует привилегий выше
`project:read`, эксплуатируется через самый обычный symlink в проекте).
Второй по значимости — silent data corruption в `patch_apply.py`
(живой `project:patch`-scoped endpoint). Плюс defense-in-depth фикс в
`policy.py` (предотвращает regression того же класса, что уже дал
реальный баг в T82) и три orphaned-feature бага (не эксплуатируемы
сейчас, исправлены всё равно — дёшево и корректно). Все фиксы — с
regression-тестами, проверенными на падение до фикса (в т.ч. эмпирически,
через реальный subprocess/filesystem, не только сравнение строк). Всё,
что живёт в `web-ssh-gateway` контейнере, задеплоено.

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
   - ✅ **`webhook_manager.py` — решено: удалить, не достраивать.**
     `api_help.py` документирует только явный `POST /api/webhooks/{id}/deploy`
     (authenticated API call), README вообще не упоминает webhooks —
     несуществующий incoming-receiver никогда не был частью заявленного API.
     Достраивать его (raw-body HMAC per-provider, новый authless-by-design
     endpoint) — это новая фича, а не багфикс, и противоречит позиционированию
     проекта (API-first, не raw internet exposure). Удалены `handle_webhook()`
     и теперь полностью мёртвое поле `secret` (`WebhookConfig`, `add_webhook()`,
     `CreateWebhookRequest`, `api_help.py`). Попутно нашёлся второй, реально
     достижимый баг, который `handle_webhook()` маскировал: `execute_deploy()`
     (единственный реальный путь триггера) никогда не писал в `_deployments` —
     `GET /api/webhooks/{id}/deployments` (документирован, достижим) всегда
     возвращал пустой список для ЛЮБОГО реального деплоя. Исправлено —
     `execute_deploy()` теперь пишет историю. Заодно `shlex.quote()` для
     `target_path` в той же shell-команде (тот же паттерн, что в
     scaffolding.py/context_editing.py). Тестов на модуль не было вообще.
     Commit `9f1be72e`.
5. ✅ **Redis job queue / dead-letter** — перепроверено: `RedisJobQueue.enqueue()`
   до сих пор нигде не вызывается в production-коде (`grep -rn "\.enqueue("` по
   `app/` находит только `event_hook_emitter.py`'s `ds.enqueue()` —
   `DeliveryService` из `event_hooks.py`, не связанный класс). T81.4's
   предположение подтверждено, всё ещё верно. `jobs.py`'s
   `get_queue_stats()`/`get_dead_letter_jobs()` — read-only, всегда пусто на
   практике; сам `RedisJobQueue` построен полностью (persist/retry/dead-letter),
   но никогда не подключён к реальному пути исполнения джобов
   (`_state.job_manager` — другой, отдельный in-process менеджер). Не баг,
   просто неиспользуемая фича.
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
7. ✅ **`app/services/*.py`** — прошёл все 7 файлов. Самая серьёзная находка
   всей сессии T82:
   - **`project_patch.py` — path traversal, реально эксплуатируемо БЕЗ
     master key.** `full_path = project_root / f["path"]`, где `f["path"]`
     берётся прямо из заголовка source-file unified-диффа (полностью
     контролируется атакующим через тело патча), без какой-либо traversal-
     фильтрации в `patch_apply.py`. `../../../etc/passwd` (или просто
     абсолютный путь — pathlib's `/` тихо отбрасывает `project_root`
     целиком для абсолютного операнда) резолвился за пределы project root,
     и запись проходила безусловно. Эмпирически подтверждено: до фикса
     traversal-патч применялся без единой ошибки, тестов на этот модуль не
     было вообще. `POST /api/projects/{project}/apply-patch` требует только
     scope `project:patch`, НЕ master key — то есть узко-скоуп agent-токен
     мог выйти за пределы своего project-сэндбокса полностью. Добавлен
     `_resolve_within_root()` (resolve + containment check), применён и в
     dry-run, и в реальном apply. Commit `acf2b359`.
   - `scaffolding.py` (`mkdir -p '{module_dir}'`) и `context_editing.py`
     (`git ls-files/show '{path}'`) — тот же паттерн неэкранированного
     single-quote — литеральная одинарная кавычка в module_path/path
     выходит за пределы quote и инжектит произвольные shell-команды. Оба
     endpoint'а сейчас требуют master key (уже полный доступ к командам
     через другие роуты), поэтому эскалации привилегий сегодня нет, но тот
     же паттерн выше оказался реальным scope bypass. Исправлено `shlex.quote()`,
     консистентно с `git.py`/`logs.py`/`templates.py`/`project_structure.py`.
     Commit `acf2b359`.
   - `file_editing.py`, `project_search.py`, `project_structure.py`,
     `command_gate.py`: чисто, без новых находок (`project_structure.py` уже
     использует `shlex.quote()` корректно; `project_search.py` — чистый
     Python без shell вообще).

## Итог T82

Все 7 пунктов полностью закрыты, ничего не оставлено на потом. Реальных
находок: 14 (XSS, broken async SDK, swallowed diagnostic detail, TTL
bypass, SSRF, audit-attribution gap, cross-tenant session leak, readonly
bypass, latent shell-injection ×3, **path traversal с обходом
project-scope** — самая серьёзная, dead unauthenticated-webhook-receiver
stub, broken deployment history). Все исправлены с regression-тестами,
проверенными на падение до фикса; всё, что живёт в
`web-ssh-gateway`/`mcp-server` контейнерах, задеплоено и подтверждено
healthy.
