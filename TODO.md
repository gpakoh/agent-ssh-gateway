# Agent SSH Gateway — TODO

## 🧹 Решение по мёртвому коду: RedisJobQueue подключён, остальное удалено

По итогам предыдущего разбора мёртвого кода — пользователь решил:
RedisJobQueue подключить, остальное (genuinely dead) удалить вместе с
тестами.

1. ✅ **`RedisJobQueue` подключён.** JobManager выполняет джобы сразу
   in-process (asyncio task на джобу), а не через pending/processing
   zset-модель `enqueue()`/`dequeue()` — поэтому подключение
   одностороннее: на каждом терминальном переходе (completed/failed/
   policy-denied) `JobManager._persist_terminal_job()` best-effort
   зеркалирует джобу в Redis через новый `RedisJobQueue.save_terminal_job()`
   (пишет напрямую в то же хранилище, что читает `_get_job`/
   `get_dead_letter_jobs`/`get_queue_stats`, минуя очередь). Redis ошибка
   никогда не влияет на исход джобы (try/except, best-effort). Честные
   границы: сама джоба НЕ переживает рестарт (asyncio task/SSE-listeners
   физически не могут) — переживает только её история/результат,
   плюс `GET /api/jobs/queue/stats`/`GET /api/jobs/queue/dead` теперь
   показывают реальные данные вместо вечного нуля. `enqueue`/`dequeue`/
   `heartbeat`/`retry_job`/`recover_orphans` (pull-модель для гипотетического
   распределённого worker pool) оставлены нетронутыми — ими до сих пор
   никто не пользуется, это осознанно отдельная, не реализованная фича.
   9 новых тестов (`test_job_manager_redis_persistence.py`,
   `test_redis_queue_save_terminal_job.py`).
2. ✅ **`WorkspaceAuditLogger`/`AuditLogger`/`AuditEntry`** (app/workspace/
   snapshot.py) — удалены полностью (класс, dataclass, backward-compat
   alias, весь `TestAuditLogger` + `test_rollback_with_audit_logging` в
   tests/test_workspace_snapshot.py — 12 тестов). Ноль вызывающих нигде
   в кодовой базе.
3. ✅ **`ProjectRegistry`** (examples/mcp_server/project_registry.py) —
   файл удалён целиком вместе с `tests/test_project_registry.py`.
   Заодно удалена конфигурация, существовавшая только ради него:
   `PROJECT_MAP`/`_load_project_map()`/`_PROJECT_MAP_DEFAULT` и
   поддержка `MCP_PROJECT_MAP_JSON`/legacy `MCP_GATEWAY_PROJECT_ROOT` в
   `examples/mcp_server/config.py` (`ALLOWED_PROJECT_ROOTS` осталась —
   её использует `docker_client.py`).
4. ✅ **`edit_files_bulk()`** (app/bulk_operations_v2.py) — метод удалён;
   реальный роут `/api/bulk/edit` всегда использовал отдельную
   реализацию (`edit_many()`). Удалён `test_edit_files_bulk` из
   `test_batch_managers.py`, уточнён docstring модуля (сноска на
   files.py была неточной).

Полный набор (4113 тестов, было 4120 — минус 16 мёртвых, плюс 9 новых)
зелёный, `ruff`/`mypy` чистые (та же pre-existing ошибка в
`event_hook_delivery.py:340`).

## ⚠️ Коррекция: два "orphaned"-фикса были на самом деле live

По запросу пользователя перепроверил все находки этой сессии, помеченные
как "orphaned"/"не подключено ни к чему живому", через `git log -S` и
grep реальных вызывающих. Два из них оказались НЕВЕРНО охарактеризованы:

- **`app/workspace/scan_project.py`'s `scan_project()`** (T83, commit
  `8d4f5055`) — заявлено "не подключено ни к одному router/tool". НА
  САМОМ ДЕЛЕ: вызывается зарегистрированным MCP tool'ом
  `project_scan_destructive` (`examples/mcp_server/server.py`,
  добавлен commit `0658ae2f` от 2026-07-30 — РАНЬШЕ фикса от 2026-08-05)
  с полностью пользовательским параметром `pattern`. Symlink-escape был
  живым, эксплуатируемым через реально работающий mcp-server контейнер
  всё это время.
- **`app/services/project_search.py`'s `search_text()`** (T84, commit
  `237c517f`) — заявлено "search_text() нигде не импортируется". НА
  САМОМ ДЕЛЕ: вызывается через `mcp_client_tools.search_text()` →
  зарегистрированный MCP tool `search_text` (server.py, регистрация
  видна с commit `80601033` от 2026-07-29 — тоже раньше фикса).

Оба бага исправлены тем же патчем независимо от (неверной) пометки
"orphaned" — уязвимость не осталась непропатченной. Но severity в
отчётах T83/T84 была занижена: эти пути были не "precautionary", а
реально достижимы через уже работающий MCP-сервер.

Перепроверены и подтверждены ДЕЙСТВИТЕЛЬНО orphaned (ноль вызывающих
нигде в кодовой базе): `app/workspace/snapshot.py`'s
`WorkspaceAuditLogger`, `examples/mcp_server/project_registry.py`'s
`ProjectRegistry`, `app/bulk_operations_v2.py`'s `edit_files_bulk()`
(T88 — единственный вызывающий это его собственный unit-тест,
`bulk_edit_files` роут всегда использовал отдельную реализацию,
позже вынесенную в `edit_many()`). `SnapshotStore` — не баг того же
класса: явно задокументирован в `api_help.py` как намеренно
Python-API-only, не REST/MCP feature, а не заброшенный код.

`RedisJobQueue.enqueue()` — тоже подтверждён genuinely dead (только
`get_queue_stats()`/`get_dead_letter_jobs()` read-only вызываются из
`jobs.py`). Класс полностью реализует persist/retry/dead-letter, но
никогда не подключён к реальному пути создания джобов (`JobManager` —
отдельный, несвязанный in-process менеджер). Единственный найденный
случай "полезная функциональность построена, но не подключена" в
буквальном смысле — не баг, а нереализованное решение о wiring,
которое стоит принять осознанно (подключить для durability джобов
через рестарт гейтвея, либо удалить как YAGNI).

Ничего полезного не было потеряно случайно: единственное реальное
удаление кода в этой сессии (`handle_webhook()`, T82) подтверждено
мёртвым по всей git-истории до удаления.

## 🔍 T88 — Седьмой круг: app/services

Контекст: T84 уже перепроверял `app/services` с паттерном symlink-escape,
найденным в T83 (`project_search.py`). Этот круг — полный свежий проход по
всем 7 файлам (~1050 строк) с накопленными за T82-T87 паттернами
(discarded-validation, sibling-endpoint divergence).

1. ✅ **`file_editing.py` — SERIOUS, живой guardrail bypass.** `edit_many()`
   принимал `validate: bool = False` — `PATCH /api/batch/edit` вызывал его
   с `validate=True`, а `POST /api/bulk/edit` (тот же `ssh:files` scope,
   не master key) — вообще без аргумента, т.е. `validate=False`. Это
   полностью пропускало `validate_path()`, единственный guardrail против
   FORBIDDEN_PATHS (`/etc/shadow`, `/root/.ssh`, ...) и traversal-сегментов
   на remote-таргете, т.к. `FileEditor.edit_file()` сам никакой валидации
   не делает. Тот же паттерн — сестринский `POST /api/bulk/read` (routers/
   files.py) делегирует в `BulkOperationsManager.read_files_bulk()`
   (app/bulk_operations_v2.py), который тоже вызывал `file_editor.read_file()`
   без вообще какой-либо валидации. Убран параметр `validate` — теперь
   `edit_many()` всегда валидирует; добавлен `validate_path()` в
   `read_files_bulk()` и (заодно, хоть и мёртвый код) в `edit_files_bulk()`.
2. ✅ **Проверено без новых находок**: `command_gate.py`, `project_search.py`
   (уже T84 — symlink-check на месте), `project_structure.py` (`shlex.quote()`
   передаётся напрямую в `{path}`-плейсхолдер без дополнительного
   ручного оборачивания в кавычки — корректно), `context_editing.py`
   (уже T82, `shlex.quote()` везде; `context_file_read`/`context_file_edit`
   роуты master-key-only, поэтому отсутствие `validate_path()` там — не
   privilege escalation, т.к. master key и так имеет полный доступ),
   `project_patch.py` (уже T82, `_resolve_within_root()` полностью
   резолвит symlinks перед containment-check — надёжно), `scaffolding.py`
   (уже T82, `shlex.quote()` корректно).

Regression: 4 новых теста в `test_files_path_validation.py`
(`TestBulkReadForbiddenPathsRejected`, `TestBulkEditForbiddenPathsRejected`)
— оба bulk-эндпоинта теперь корректно отклоняют `/etc/shadow`/
`/root/.ssh/authorized_keys`. Проверено на падение против pre-fix кода
через `git stash`. Полный набор (4120 тестов) зелёный, `ruff`/`mypy`
чистые.

## 🔍 T87 — Шестой круг: app/routers

Контекст: все 24 файла `app/routers/` (~6200 строк) — первый круг, который
целится именно в роутеры целиком, а не только в отдельные файлы, задетые
попутно другими кругами. Приоритет отдавался эндпоинтам на `require_scope`
(доступны agent-токенам), а не только `require_master_key` (там privilege
escalation не работает, т.к. master key уже имеет полный доступ).

1. ✅ **`files.py` — SERIOUS, живая кросс-тенантная утечка.** `GET
   /api/project/tree` вообще не вызывал `ensure_session_owner`/
   `_check_session_ownership` — в отличие от почти идентичного соседа `POST
   /api/project/files/structure`, который эту проверку делает. Любой
   `ssh:files`-scoped agent-токен мог передать чужой `session_id` и получить
   листинг файлов ЧУЖОЙ SSH-сессии. Добавлен `request: Request` параметр +
   `await _check_session_ownership(session_id, request)`.
2. ✅ **`diagnostics.py` — SERIOUS, живая кросс-тенантная утечка в файле,
   который T82 уже частично чинил.** `GET /api/diagnostics/latency`
   итерировался по ВСЕМ джобам в `job_manager._jobs` без вообще какой-либо
   фильтрации по владельцу — в отличие от `GET /api/jobs`, который
   фильтрует через `job_visible_to()`. Любой `diagnostics:read`-scoped
   agent-токен видел `job_id`/тайминги ВСЕХ джобов всех тенантов. Добавлена
   фильтрация через `job_visible_to()`, консистентно с `jobs.py`.
3. ✅ **`context.py` — тот же паттерн unescaped shell interpolation, что
   чинился по всей кодовой базе весь этот сеанс.** `POST
   /api/templates/render` строил `f"cat > '{req.target_path}' << ..."` без
   `shlex.quote()` — литеральная одинарная кавычка в `target_path` выходит
   за пределы quote и инжектит произвольные shell-команды. Endpoint
   `require_master_key`-only (эскалации привилегий сегодня нет), но тот же
   паттерн в других местах этого сеанса (scaffolding.py, context_editing.py)
   оказался реальным scope bypass — исправлено для консистентности.
   Добавлена проба в `test_seam_shell_injection_matrix.py`.
4. ✅ **Проверено без новых находок**: `auth.py`, `admin_approval.py`,
   `admin_access.py` (уже T82), `project_inspection.py`, `logs.py`,
   `search_replace.py`, `servers.py`, `templates.py` (unescaped
   `command.replace()` в `run_template`, но `require_master_key`-only —
   не новая привилегия), `known_hosts.py`, `oauth.py`, `system.py`,
   `code.py`, `event_hooks.py`, `snapshots.py`, `git.py` (уже T82),
   `batch.py` (уже T82), `webhooks.py` (уже T82), `audit.py`, `jobs.py`
   (`job_visible_to`/`_get_owned_job` консистентно применены),
   `workspace.py` (scope-based `registry_for_identity()` корректно
   применяется к write/preview роутам; read-роуты намеренно
   `require_master_key`-only), `ssh.py` (весь файл, 1256 строк —
   `ensure_session_owner` консистентно применён везде, включая оба
   WebSocket-эндпоинта).

Regression-тесты: `test_session_ownership.py` (3 новых теста для
`/api/project/tree`), `test_diagnostics_latency.py` (2 новых теста для
кросс-тенантной фильтрации), `test_seam_shell_injection_matrix.py`
(новая проба для `render_template`). Все три находки проверены на падение
против pre-fix кода через `git stash`. Полный набор (4116 тестов) зелёный,
`ruff`/`mypy` чистые (та же pre-existing mypy-ошибка в
`event_hook_delivery.py:340`, не связана с этой работой).

## 🧪 Тесты на стыках (seam-test matrices)

Контекст: T82-T86 нашли один и тот же класс бага в РАЗНЫХ реализациях
одной концептуальной операции по несколько раз подряд (symlink/glob
escape — 4 раза; any()-vs-all() по multi-scope — 1 раз, но паттерн
воспроизводим для любого будущего multi-scope tool; unescaped shell
interpolation — 5 раз). Обычный regression-тест на конкретный найденный
баг не ловит следующее похожее место — каждый раз нужен был отдельный
аудиторский круг, чтобы заметить тот же паттерн снова. Эти три файла
целятся не в конкретный баг, а в КЛАСС бага: набор "проб", каждая из
которых дергает реальную функцию из кодовой базы, против одной и той же
атакующей батареи — так что следующая функция того же семейства,
добавленная кем угодно, попадает под тот же чек автоматически, если её
добавить в соответствующий список проб.

1. ✅ **`tests/test_seam_path_containment_matrix.py`** — symlink/glob escape
   (6 проб: `workspace/search.py`, `workspace/scan_project.py`,
   `services/project_search.py`, `mcp_client_tools.list_files`,
   `mcp_client_tools._safe_glob` как control, `workspace/policy.py`'s
   `validate_write`) + URL-path escape (3 пробы: `gitea_client.py`/
   `github_client.py`). Проверено против pre-fix версий через `git checkout
   <parent> -- <path>` — 5 из 10 тестов падали корректно на старом коде.
2. ✅ **`tests/test_seam_scope_combinatorial_matrix.py`** — данные берутся
   прямо из `TOOL_SCOPES` (не хардкод списка тулов), генерирует все partial-
   overlap подмножества для каждого multi-scope tool. Проверено против
   pre-fix `tool_scopes.py` — все 10 partial-overlap кейсов падали
   корректно.
3. ✅ **`tests/test_seam_shell_injection_matrix.py`** — 5 проб
   (`scaffolding.scaffold_python_class`, `context_editing.edit_file_with_context`,
   `webhook_manager.execute_deploy`, `agent_tools._read_task_json`,
   `agent_tasks.read_agent_task_file`). Полезная нагрузка комбинирует ДВЕ
   формы бага в одной строке — `$(touch marker_a)` для случая "вообще без
   кавычек" (старый `webhook_manager.py`: `f"cd {target_path} && ..."`) и
   `'; touch marker_b; echo '` для случая "обёрнуто в ручные одинарные
   кавычки" (старый `scaffolding.py`: `f"mkdir -p '{module_dir}'"`) — первая
   версия payload'а (только quote-breakout) давала ложноотрицательный
   результат на `webhook_manager.py` и `agent_tasks.py`, пока не проверил
   явно против pre-fix кода и не увидел, что они "проходят" даже с
   незакрытой уязвимостью. После усиления payload — все 5 проб падают
   корректно на своих pre-fix версиях (проверено через `git checkout
   <parent-of-fix> -- <path>`), все проходят на текущем (исправленном) коде.

Полный набор (4111 тестов) зелёный, `ruff`/`mypy` чистые (одна
pre-existing mypy-ошибка в `event_hook_delivery.py:340`, не связана с этой
работой — подтверждено на чистом `master` без изменений). Один Selenium
E2E-тест (`test_append_line_system_type_escapes_html`) — таймингово
нестабилен (2 из 3 прогонов в изоляции проходят), не связан с этой
работой, не трогал.

## 🔍 T86 — Пятый круг: examples/mcp_client_remote

Контекст: "fleet" MCP-сервер (Gitea/GitHub/Docker/Postgres/Context7),
частично аудирован в более ранней сессии (SSRF, connection leaks,
URL-leak sanitization, XSS на /oauth/consent — все уже исправлены). Этот
круг — все 14 файлов (~2900 строк) заново, с паттернами, накопленными за
T83-T85.

1. ✅ **`gitea_client.py`/`github_client.py` — SERIOUS, живой path traversal
   мимо `ALLOWED_ENDPOINTS`.** Оба клиента строили путь запроса через
   `"/repos/{owner}/{repo}/...".format(owner=owner, repo=repo)` без вообще
   какой-либо валидации `owner`/`repo`/`path` (MCP tool signature — голый
   `str`, никаких паттернов). httpx нормализует `..`-сегменты в итоговом
   пути ОТНОСИТЕЛЬНО ВСЕГО URL (включая путь самого `base_url`) —
   эмпирически подтверждено: `client.build_request("GET",
   "/repos/foo/../../admin/x/branches")` при `base_url="https://host/api/v1"`
   резолвится в `"https://host/api/v1/admin/x/branches"`. `owner =
   "foo/../../admin"` полностью убегал из намеченной структуры
   `/repos/{owner}/{repo}/...` и попадал на ПРОИЗВОЛЬНЫЕ другие endpoint'ы
   API — включая реальный `/api/v1/admin/*` у Gitea — используя собственный
   `GITEA_TOKEN`/`GITHUB_TOKEN` адаптера. Проверка `ALLOWED_ENDPOINTS`
   валидирует только фиксированный TEMPLATE, никогда не финальный,
   подставленный путь. Добавлены `validate_repo_owner_or_name()`/
   `validate_repo_path()` в `shared.py`, применены централизованно в
   `_get()` обоих клиентов. Commit `b755e25e`.
2. ✅ **`docker_client.py`** — `compose_up`/`compose_restart`/`compose_build`/
   `compose_logs`/`compose_down` не клэмпили `timeout`, в отличие от
   `stop`/`restart` (уже клэмпят к `[1, 120]`). Неограниченный
   caller-supplied timeout уходил прямо в `asyncio.wait_for()`, а для
   `compose_down` — ещё и в саму `-t` argv. Тот же паттерн, что уже
   применялся к `stop`/`restart`, просто непоследовательно. Commit
   `cb490999`.
3. ✅ **`server.py`** — главный entry point, публичный OAuth-прокси. Уже
   выигрывает от фикса `has_required_scope` (T85 п.2) — это ИМЕННО тот
   живой enforcement path, который его использует
   (`_check_tool_scope` → `has_required_scope`). Fail-open путь для
   не-`tools/call` тел (в т.ч. гипотетический JSON-RPC batch-массив)
   проверен — не эксплуатируем, т.к. `mcp` SDK's `JSONRPCMessage.model_validate()`
   отклоняет list-shaped тела до того, как что-либо выполнится — корректный
   backstop на уровне транспорта.
4. ✅ **Проверено без новых находок**: `postgres_client.py` (SQL guardrails
   подкреплены `default_transaction_read_only=on` на уровне соединения —
   даже пробел в regex-blocklist перехватывается самим Postgres для
   любого DML/DDL; comment-based bypass не работает, т.к. Postgres
   токенизирует комментарии как разделители, а не склеивает соседние
   идентификаторы), `postgres_server.py` (уже пользуется исправленным
   connection-pool-leak фиксом), `docker_server.py`/`gitea_server.py`/
   `github_server.py` (только регистрация уже проверенных клиентов),
   `context7_server.py` (уже пользуется исправленным subprocess-leak
   фиксом), `gitea_mcp_stdio.py`, `run_docker_server.py`,
   `run_postgres_server.py` (тривиальные launcher-скрипты).

## Итог T86

2 реальных бага, оба с regression-тестами, проверенными на падение до
фикса. Path traversal в gitea/github клиентах — самая серьёзная находка
этого круга: живой, реально эксплуатируемый уход за пределы
`ALLOWED_ENDPOINTS` с использованием собственного токена адаптера.
Полный набор тестов (4047) зелёный.

## 🔍 T85 — Четвёртый круг: examples/mcp_server

Контекст: аудит всех 23 файлов `examples/mcp_server/` (~8300 строк) —
MCP-сервер, отдельный от основного REST API. Найдено 5 реальных багов,
включая самую серьёзную находку всей серии аудитов (T82-T85):
**PKCE bypass в OAuth**.

1. ✅ **`oauth_provider.py` — full PKCE bypass.** `exchange_code_for_token()`
   вызывал `_verify_pkce()`, но НИКОГДА не проверял её bool return value —
   только пойманное исключение (bad length). Любой `code_verifier`
   ПРАВИЛЬНОЙ длины (43-128 симв.), независимо от того, совпадает ли он с
   `code_challenge`, успешно завершал token exchange — полностью
   уничтожая смысл PKCE (защита от authorization-code-interception).
   Эмпирически подтверждено. Существующий тест использовал
   `"wrong_verifier"` (14 символов) — отклонялся ДЛИНОЙ, реальная
   сверка challenge никогда не выполнялась — тест выглядел как проверка
   этого свойства, но не проверял. Единственный вызывающий
   (`exchange_authorization_code`, реальный FastMCP-flow) передаёт
   `code_verifier=""`, поэтому баг не был достижим через живой флоу
   СЕГОДНЯ — но это публичный метод класса с прямым тестовым покрытием,
   и молчаливо отброшенный результат security-проверки — ровно та мина,
   что взрывается при любом будущем вызывающем. Commit `235a11d5`.
2. ✅ **`tool_scopes.py` — full mcp:project bypass для "viewer" профиля,
   живой баг.** `has_required_scope()` использовал `any()` по списку
   required scopes инструмента. `list_files`/`info`/`scan_command`/
   `list_tree` требуют `["mcp:read", "mcp:project"]` — по модели данных
   это означает ОБА (mcp:project — отдельный scope, используется
   самостоятельно у множества других инструментов). Так как ВСЕ профили
   с `mcp:project` также имеют `mcp:read`, `any()` делал `mcp:project`
   половину требования бессмысленной — токен профиля "viewer" (mcp:read,
   mcp:repo, mcp:docs — mcp:project намеренно исключён) проходил ЛЮБОЙ
   multi-scope инструмент через один `mcp:read`. Эмпирически подтверждено.
   Это реальный, живой enforcement path для публичного ChatGPT-facing
   сервера — `mcp_client_remote/server.py` импортирует и вызывает
   `has_required_scope` на КАЖДЫЙ входящий `tools/call`. Исправлено:
   `any()` → `all()`. Commit `0550fdf8`.
3. ✅ **`command_policy.py` — `find` bypass, живой баг.** Denylist блокировал
   несколько конкретных `-exec` целей (`rm`/`mv`/`chmod`/`chown` как literal
   substrings), но не сами деструктивные примитивы `find`. `find . -delete`
   не требует exec-цели вообще. `find . -exec <что угодно> +` (используя
   `+`-терминатор вместо `\;`, обходя отдельную проверку на `;`) запускает
   ПРОИЗВОЛЬНУЮ команду, которую find сам никогда не инспектирует — не
   только rm/mv/chmod/chown. Оба начинаются с разрешённого префикса
   `"find "` и не матчили ни один denied substring, несмотря на то, что
   модуль сам себя называет "Read-only command policy". Живой путь:
   `gateway_client.py`'s `execute_restricted()` — MCP-инструмент
   "restricted"-исполнения — вызывает `validate_readonly_command()`
   напрямую. Добавлены `-exec`/`-execdir`/`-ok`/`-okdir`/`-delete`/
   `-fprint`/`-fls` в denylist. Commit `68440d48`.
4. ✅ **`mcp_client_tools.py::list_files` — symlink-escape info leak,
   живой баг.** Тот же паттерн, что в T83/T84: `project_dir.rglob(pattern)`
   следует symlink, если паттерн явно называет symlink-сегмент, `_safe_glob`
   (используется `find_files`) уже делает это правильно (resolve перед
   containment check), `list_files` — нет. Живой MCP-инструмент
   `list_files`. Commit `235a11d5`.
5. ✅ **`agent_tools.py::_read_task_json`** — `task_id` интерполировался в
   shell-команду без экранирования, в отличие от соседней
   `_read_current_plan` (использует `shlex.quote`). Единственный
   вызывающий уже валидирует `task_id` через `validate_task_id()` (safe-
   charset regex) — не эксплуатируемо сегодня, исправлено для
   консистентности/defense-in-depth. Commit `68440d48`.

**Проверено, без находок**: `server.py` (3414 строк — в основном wiring,
рискованные операции делегированы уже проверенным модулям), `git.py`
(exemplary — fixed argv, shell=False), `agent_backend_router.py`
(FAILED-статус не авто-восстанавливается по истечении cooldown — defensible
design, не баг), `tool_modes.py` (перепроверил `MCP_CLIENT_BLOCKED_TOOLS`
против полного `mcp_client` tool set — пробелов нет), `tool_results.py`
(redaction regex не покрывает `/proc`/`/sys`/`/srv`/`/boot` — minor,
не покрывает ничего реально используемого в этом деплойменте),
`docker_confirm.py`, `mcp_audit.py` (ring buffer корректно bounded —
не тот баг, что в T83's `WorkspaceAuditLogger`), `latency_metrics.py`,
`tools_manifest.py`, `self_test.py`, `token_store.py`.

**Orphaned (не подключено ни к чему живому)**: `project_registry.py`/
`ProjectRegistry` — ноль вызывающих во всём `examples/mcp_server/`, тот
же паттерн, что `RedisJobQueue`/`SnapshotStore`/`scan_project.py`. Не
исправлял (dead code, нет живого пути).

## Итог T85

5 реальных багов, все с regression-тестами, проверенными на падение
до фикса. `has_required_scope`'s AND/OR баг и `find`-bypass — оба живые,
реально достижимые через публичный MCP-сервер. PKCE bypass — самая
серьёзная находка по механизму (полностью уничтожает security-свойство),
хоть и не достижима через СЕГОДНЯШНИЙ живой флоу конкретно этого
деплоймента. Полный набор тестов (4033) зелёный.

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
