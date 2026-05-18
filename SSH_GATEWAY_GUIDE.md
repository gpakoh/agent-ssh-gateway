# SSH GATEWAY — ПОЛНАЯ ИНСТРУКЦИЯ

## 1. БЫСТРЫЙ СТАРТ (30 секунд)
import requests, urllib3, json, time
urllib3.disable_warnings()

# Step 1: Authelia SSO
s = requests.Session()
s.verify = False
s.post("https://auth.xloud.ru/api/firstfactor", json={
    "username": "agent",
    "password": "NpvSBhi7ag1X8stB3kFMnCcxM9PKE9R",
    "request_method": "GET",
    "request_uri": "https://ssh.xloud.ru/"
})

# Step 2: SSH Connect
r = s.post("https://ssh.xloud.ru/api/ssh/connect", json={
    "host": "10.0.1.103",
    "port": 22,
    "username": "root",
    "password": "CHANGEME"
})
session_id = r.json()["session_id"]

# Step 3: Heartbeat (каждые 30 сек)
s.post("https://ssh.xloud.ru/api/ssh/heartbeat", json={"session_id": session_id})

# Готово! Можно работать.

**Session ID** — главная переменная. Все API требуют её.

## 2. API ENDPOINTS — ПОЛНЫЙ СПИСОК

### 2.0 Context Management (Git-aware)

**Создать контекст разработки:**
```python
r = s.post("https://ssh.xloud.ru/api/context/create", json={
    "session_id": session_id,
    "name": "gateway_refactor",
    "path": "/media/1TB/Python/NOD_gateway/gateway_client",
    "branch": "feature/new-api",  # опционально
    "auto_commit": True,          # автокоммит после изменений
    "auto_validate": True         # автопроверка mypy+tests
})
context_id = r.json()["context_id"]
# → {"context_id": "...", "git": {"status": "clean|not_initialized", ...}}
```

**Git-статус:**
- `clean` — всё ок, можно коммитить
- `not_initialized` — проект не в Git, работа продолжается
- `has_changes` — есть незакоммиченные изменения

**Инициализировать Git (если не был):**
```python
r = s.post("https://ssh.xloud.ru/api/git/init", json={
    "context_id": context_id,
    "remote_url": "https://github.com/user/repo.git"  # опционально
})
```

**Создать коммит:**
```python
r = s.post("https://ssh.xloud.ru/api/git/commit", json={
    "context_id": context_id,
    "message": "Add new feature",
    "files": ["app/main.py"]  # опционально, по умолчанию все
})
```

**Бэкап / восстановление:**
```python
s.post("https://ssh.xloud.ru/api/git/backup", params={"context_id": context_id, "backup_name": "before_refactor"})
s.post("https://ssh.xloud.ru/api/git/restore", params={"context_id": context_id})
```

**Работа с файлами через контекст:**
```python
# Чтение
r = s.post("https://ssh.xloud.ru/api/context/file/read", json={
    "session_id": context_id,  # ← используем context_id как session_id
    "path": "app/main.py"
})

# Редактирование с автокоммитом
r = s.patch("https://ssh.xloud.ru/api/context/file/edit", json={
    "context_id": context_id,
    "path": "app/main.py",
    "operations": [{"type": "replace", "old": "...", "new": "..."}],
    "commit_message": "Update main.py",  # опционально
    "run_validation": True               # опционально
})
# → {"success": true, "git_commit": "abc123", "warning": null}
```

### 2.1 SSH Sessions

| Method | Path | Описание |
| POST | /api/ssh/connect | Создать SSH-сессию |
| POST | /api/ssh/disconnect | Закрыть сессию |
| POST | /api/ssh/heartbeat | Продлить сессию (каждые 30с) |
| GET | /api/ssh/sessions | Список активных сессий |

**Connect:**
r = s.post("https://ssh.xloud.ru/api/ssh/connect", json={
    "host": "10.0.1.103",
    "port": 22,
    "username": "root",
    "password": "CHANGEME"
})
session_id = r.json()["session_id"]

**Heartbeat:**
r = s.post("https://ssh.xloud.ru/api/ssh/heartbeat", json={"session_id": session_id})
# → {"status": "ok", "idle_time": 0.5}

### 2.2 Background Jobs API
| Method | Path | Описание |
| POST | /api/jobs/run | Запуск команды в фоне |
| GET | /api/jobs/{id}/status | Статус выполнения |
| GET | /api/jobs/{id}/result | Полный результат |
| GET | /api/jobs/{id}/stream | SSE поток вывода |
| POST | /api/jobs/{id}/cancel | Отмена задачи |

**Запуск:**
r = s.post("https://ssh.xloud.ru/api/jobs/run", json={
    "session_id": session_id,
    "command": "cd /media/1TB/Python/NOD_gateway/gateway_client && python -m pytest tests/ -x -q",
    "timeout": 300,
    "description": "pytest gateway"
})
job_id = r.json()["job_id"]

**Статус:**
r = s.get(f"https://ssh.xloud.ru/api/jobs/{job_id}/status")
# → {"job_id": "...", "status": "running|completed|failed|cancelled", "duration": 45.2}

**Результат:**
r = s.get(f"https://ssh.xloud.ru/api/jobs/{job_id}/result")
# → {"status": "completed", "exit_code": 0, "output": "...", "duration": 120.5}

**SSE Stream (реальное время):**
r = s.get(f"https://ssh.xloud.ru/api/jobs/{job_id}/stream", stream=True)
for line in r.iter_lines():
    if line:
        data = json.loads(line.decode('utf-8').replace('data: ', ''))
        print(data.get("output", ""), end="")

**Отмена:**
s.post(f"https://ssh.xloud.ru/api/jobs/{job_id}/cancel")
# → {"status": "cancelled"}


### 2.3 File Edit API
| Method | Path | Описание |
| POST | /api/file/read | Чтение файла (JSON) |
| GET | /api/file/raw | Чтение файла (text/plain, Range support) |
| POST | /api/batch/read | Массовое чтение файлов |
| PATCH | /api/file/edit | Операции: replace/insert_after/insert_before/delete/append |
| POST | /api/file/patch | Unified diff patch |

**Чтение (JSON):**
r = s.post("https://ssh.xloud.ru/api/file/read", json={
    "session_id": session_id,
    "path": "/media/1TB/Python/NOD_gateway/gateway_client/app/main.py",
    "offset": 1,
    "limit": 30
})
# → {"path": "...", "total_lines": 150, "content": "строка целиком\nвторая строка\n..."}

**Чтение RAW (text/plain):**
r = s.get("https://ssh.xloud.ru/api/file/raw", params={
    "session_id": session_id,
    "path": "/media/1TB/Python/NOD_gateway/gateway_client/app/main.py"
})
# → text/plain, без JSON-обёртки

**Чтение с Range (часть файла):**
r = s.get("https://ssh.xloud.ru/api/file/raw", params={
    "session_id": session_id,
    "path": "/media/1TB/Python/NOD_gateway/gateway_client/app/main.py",
    "offset": 100,
    "limit": 50
})
# → строки 100-150

**Массовое чтение (до 20 файлов):**
r = s.post("https://ssh.xloud.ru/api/batch/read", json={
    "session_id": session_id,
    "paths": [
        "app/main.py",
        "app/config.py",
        "app/models.py"
    ]
})
# → {"files": {"app/main.py": "...", ...}, "errors": {}}

**Редактирование:**
r = s.patch("https://ssh.xloud.ru/api/file/edit", json={
    "session_id": session_id,
    "path": "/media/1TB/Python/NOD_gateway/gateway_client/app/core/config.py",
    "operations": [
        {"type": "replace", "old": "def get(self, key):", "new": "def get(self, key, default=None) -> Any:"},
        {"type": "insert_after", "after": "class Settings:", "text": "    # validated"},
        {"type": "delete", "old": "# TODO: remove this line"},
        {"type": "append", "text": "# End of file"}
    ]
})
# → {"status": "success", "operations_applied": 4, "lines_changed": 4}


### 2.4 PTY (интерактивный терминал)
| Method | Path | Описание |
| POST | /api/ssh/pty/{session_id}/create | Создать PTY |
| POST | /api/ssh/pty/{session_id}/input | Отправить ввод |
| GET | /api/ssh/pty/{session_id}/output | Получить вывод |
| POST | /api/ssh/pty/{session_id}/close | Закрыть PTY |

**Когда использовать:** Интерактивные программы (htop, vim, opencode)

s.post(f"https://ssh.xloud.ru/api/ssh/pty/{session_id}/create")
time.sleep(2)

s.post(f"https://ssh.xloud.ru/api/ssh/pty/{session_id}/input", json={"data": "docker ps\n"})
time.sleep(1)

r = s.get(f"https://ssh.xloud.ru/api/ssh/pty/{session_id}/output")
print(r.json().get("output", ""))


### 2.5 Exec Command (одиночные команды)
| Method | Path | Описание |
| POST | /api/ssh/execute | Выполнить команду, получить результат |

r = s.post("https://ssh.xloud.ru/api/ssh/execute", json={
    "session_id": session_id,
    "command": "docker ps --format '{{.Names}}'",
    "timeout": 30
})
print(r.json()["stdout"])

**ВАЖНО:** Таймаут API = 60 секунд. Для длительных команд — Background Jobs API!

### 2.4 Validation API

**Запустить валидацию (mypy + pytest):**
```python
r = s.post("https://ssh.xloud.ru/api/validate", json={
    "context_id": context_id,
    "run_mypy": True,      # типизация
    "run_tests": True      # тесты
})
result = r.json()
print(result["summary"])
# → "✅ Валидация пройдена: 2 шагов, 0 ошибок"
# или
# → "❌ Валидация не пройдена: 3 ошибок"

# Детали по шагам:
for step in result["steps"]:
    print(f"  {step['name']}: {step['status']} ({step['duration']}s)")
    if step['errors'] > 0:
        print(f"    {step['output'][:500]}")
```

**Автовалидация при редактировании:**
```python
r = s.patch("https://ssh.xloud.ru/api/context/file/edit", json={
    "context_id": context_id,
    "path": "app/main.py",
    "operations": [...],
    "run_validation": True,  # ← запустить mypy+tests после изменений
    "commit_message": "Fix bug"
})
result = r.json()
# Если валидация не пройдена — коммит не создаётся!
if result.get("validation_result"):
    print(result["validation_result"]["summary"])
```

**Автовалидация в контексте:**
```python
# При создании контекста
r = s.post("https://ssh.xloud.ru/api/context/create", json={
    "session_id": session_id,
    "name": "my_project",
    "path": "/path/to/project",
    "auto_validate": True  # ← валидация после каждого edit
})
```

### 2.5 Smart Context (состояние работы)

**Открыть файл (создать вкладку):**
```python
s.post("https://ssh.xloud.ru/api/context/file/open", json={
    "context_id": context_id,
    "path": "app/main.py"
})
```

**Закрыть файл:**
```python
s.post("https://ssh.xloud.ru/api/context/file/close", json={
    "context_id": context_id,
    "path": "app/main.py"
})
```

**Обновить позицию курсора:**
```python
s.post("https://ssh.xloud.ru/api/context/cursor", json={
    "context_id": context_id,
    "path": "app/main.py",
    "line": 42,
    "column": 5
})
```

**Добавить команду в историю:**
```python
s.post("https://ssh.xloud.ru/api/context/command", json={
    "context_id": context_id,
    "command": "docker ps",
    "directory": "/media/1TB/Python/NOD_gateway"
})
```

**Добавить поиск в историю:**
```python
s.post("https://ssh.xloud.ru/api/context/search", json={
    "context_id": context_id,
    "query": "class Context",
    "path": "app"
})
```

**Добавить закладку:**
```python
s.post("https://ssh.xloud.ru/api/context/bookmark", json={
    "context_id": context_id,
    "path": "app/main.py",
    "line": 42,
    "note": "Важная функция"
})
```

**Удалить закладку:**
```python
s.delete("https://ssh.xloud.ru/api/context/bookmark", params={
    "context_id": context_id,
    "path": "app/main.py",
    "line": 42
})
```

**Получить состояние контекста:**
```python
r = s.get(f"https://ssh.xloud.ru/api/context/{context_id}/state")
state = r.json()
print(f"Открытые файлы: {[t['path'] for t in state['tabs']]}")
print(f"Активный файл: {state['active_tab']}")
print(f"Последняя команда: {state['command_history'][-1] if state['command_history'] else 'нет'}")
print(f"Закладки: {len(state['bookmarks'])}")
```

## 2.6 Workflow с Context API (рекомендуемый)

```python
import requests, urllib3
urllib3.disable_warnings()

# 1. Авторизация
s = requests.Session()
s.verify = False
s.post("https://auth.xloud.ru/api/firstfactor", json={
    "username": "agent",
    "password": "NpvSBhi7ag1X8stB3kFMnCcxM9PKE9R",
    "request_method": "GET",
    "request_uri": "https://ssh.xloud.ru/"
})

# 2. SSH Connect
r = s.post("https://ssh.xloud.ru/api/ssh/connect", json={
    "host": "10.0.1.103",
    "port": 22,
    "username": "root",
    "password": "CHANGEME"
})
session_id = r.json()["session_id"]

# 3. Создать контекст (один раз на проект!)
r = s.post("https://ssh.xloud.ru/api/context/create", json={
    "session_id": session_id,
    "name": "nod_gateway",
    "path": "/media/1TB/Python/NOD_gateway/gateway_client",
    "auto_commit": True,
    "auto_validate": False  # пока не реализовано
})
ctx = r.json()
context_id = ctx["context_id"]

# 4. Проверить git-статус
if ctx["git"]["status"] == "not_initialized":
    print("⚠️ Проект не в Git. Инициализируем...")
    s.post("https://ssh.xloud.ru/api/git/init", json={
        "context_id": context_id,
        "remote_url": "https://github.com/user/repo.git"  # если есть
    })

# 5. Работа с файлами через контекст
r = s.patch("https://ssh.xloud.ru/api/context/file/edit", json={
    "context_id": context_id,
    "path": "app/main.py",
    "operations": [
        {"type": "insert_after", "after": "from fastapi import", "text": "    BackgroundTasks,"}
    ],
    "commit_message": "Add BackgroundTasks import"
})
result = r.json()
print(f"✅ Коммит: {result.get('git_commit', 'нет')}")

# 6. Завершение
s.delete(f"https://ssh.xloud.ru/api/context/{context_id}")
s.post("https://ssh.xloud.ru/api/ssh/disconnect", json={"session_id": session_id})
```

### 2.7 Batch Operations (множественные операции)

**Выполнить несколько операций за один запрос:**
```python
r = s.post("https://ssh.xloud.ru/api/batch/execute", json={
    "context_id": context_id,
    "operations": [
        {
            "type": "read",
            "path": "app/main.py"
        },
        {
            "type": "edit",
            "path": "app/models.py",
            "operations": [
                {"type": "replace", "old": "class OldName", "new": "class NewName"}
            ]
        },
        {
            "type": "create",
            "path": "app/new_module.py",
            "content": "# New module\nprint('Hello')\n"
        },
        {
            "type": "execute",
            "command": "python -m pytest tests/ -x"
        }
    ],
    "auto_commit": True,
    "commit_message": "Refactor: rename classes",
    "run_validation": True
})
result = r.json()
print(result["summary"])
# → "✅ Все 4 операций выполнены успешно"
print(f"Коммит: {result.get('git_commit', 'нет')}")

# Детали по каждой операции:
for op in result["operations"]:
    status = "✅" if op["success"] else "❌"
    print(f"  {status} {op['operation']}: {op['path']} ({op['duration']}s)")
```

**Типы batch операций:**
- `read` — прочитать файл
- `edit` — отредактировать файл (с operations)
- `create` — создать новый файл
- `delete` — удалить файл
- `rename` — переименовать файл (new_path)
- `copy` — скопировать файл (dest_path)
- `execute` — выполнить shell команду

**Ошибки и continue_on_error:**
```python
{
    "type": "edit",
    "path": "app/main.py",
    "operations": [...],
    "continue_on_error": True  # ← продолжить даже если эта операция упадёт
}
```

## 3. КОГДА ЧТО ИСПОЛЬЗОВАТЬ
| Задача | API |
| Работа с проектом (рекомендуется) | `POST /api/context/create` → `context_id` |
| Чтение файла через контекст | `POST /api/context/file/read` |
| Редактирование с автокоммитом | `PATCH /api/context/file/edit` |
| Валидация (mypy + pytest) | `POST /api/validate` |
| Множественные операции (batch) | `POST /api/batch/execute` |
| Рефакторинг нескольких файлов | `POST /api/batch/execute` (auto_commit) |
| Поиск кода в проекте | `POST /api/code/search` |
| Генерация кода по описанию | `POST /api/code/generate` |
| Умная вставка кода | `POST /api/code/insert` |
| Шаблоны кода | `GET /api/templates` + `POST /api/templates/render` |
| Бэкап перед изменениями | `POST /api/recovery/backup` |
| Восстановление из бэкапа | `POST /api/recovery/restore` |
| Метрики проекта | `POST /api/analytics` |
| Инициализировать git | `POST /api/git/init` |
| Создать коммит | `POST /api/git/commit` |
| Бэкап перед рефакторингом | `POST /api/git/backup` |
| Прочитать файл (legacy) | `POST /api/file/read` |
| Отредактировать файл (legacy) | `PATCH /api/file/edit` |
| Запустить pytest (5 мин) | `POST /api/jobs/run` + `/result` |
| Смотреть логи в реальном времени | `GET /api/jobs/{id}/stream` |
| Интерактивная программа | `PTY` |
| Простая команда (< 30с) | `POST /api/ssh/execute` |
| Docker команды | `POST /api/ssh/execute` |
| Работать с opencode | `PTY` |
| Перезапустить сервис | `POST /api/jobs/run` |
| Проверить типизацию | `POST /api/validate` (mypy) |
| Проверить тесты | `POST /api/validate` (pytest) |
| Поиск по всему проекту | `POST /api/search/global` |
| Глобальная замена | `POST /api/replace/global` |
| Дерево файлов | `POST /api/tree` |
| Управление серверами | `GET /api/servers` |
| Подключиться к серверу | `POST /api/servers/{id}/connect` |
| Снапшот проекта | `POST /api/snapshots` |
| Восстановить снапшот | `POST /api/snapshots/restore` |
| CI/CD Webhook | `POST /api/webhooks` |
| Автодеплой | `POST /api/webhooks/{id}/deploy` |
| Редактор с подсветкой | Monaco Editor в UI |


## 4. ОПАСНЫЕ ПАТТЕРНЫ (не делай!)
| Не делай | Делай вместо |
| `cat > file << 'EOF'` | `PATCH /api/context/file/edit` |
| `sed -i` без проверки | `POST /api/context/file/read` потом `PATCH` |
| `exec_command` на 5 минут | `POST /api/jobs/run` |
| `time.sleep(120)` в коде | SSE stream + polling status |
| Не делать heartbeat | `POST /api/ssh/heartbeat` каждые 30с |
| Править тесты без проверки | `POST /api/validate` после изменений |
| Править без контекста | `POST /api/context/create` + работать через context_id |
| Забывать коммитить | Включить `auto_commit: true` в контексте |
| Потерять изменения | `POST /api/git/backup` перед рефакторингом |
| Не проверять типизацию | `POST /api/validate` (mypy) |
| Коммитить с ошибками | `auto_validate: true` в контексте |


## 5. ПРОКСИ ДЛЯ OPENCODE/Бесплатная модель BIG PICKLE
### Постоянные прокси (3 штуки):
| Прокси | Назначение |
| `http://10.0.1.199:3128` | Основной — ротируемый (адреса меняются при исчерпании лимитов) |
| `http://10.0.1.199:20170` | Запасной #1 |
| `http://10.0.1.103:20190` | Запасной #2 |

### Использование:
# Пробуй по очереди, если один пишет о конце лимита — переключайся
PROXIES = [
    "http://10.0.1.199:3128",   # Основной (ротируемый)
    "http://10.0.1.199:20170",  # Запасной #1
    "http://10.0.1.103:20190",  # Запасной #2
]
def run_opencode_with_proxy(question: str):
    for proxy in PROXIES:
        env = {
            "HTTP_PROXY": proxy,
            "HTTPS_PROXY": proxy,
            "http_proxy": proxy,
            "https_proxy": proxy,
        }
        try:
            r = s.post("https://ssh.xloud.ru/api/jobs/run", json={
                "session_id": session_id,
                "command": f"cd /media/1TB/Python/NOD_gateway && HTTP_PROXY={proxy} HTTPS_PROXY={proxy} http_proxy={proxy} https_proxy={proxy} opencode run '{question}'",
                "timeout": 600,
                "description": "opencode analysis"
            })
            job_id = r.json()["job_id"]
            print(f"Started with proxy {proxy}, job {job_id}")
            return job_id
        except Exception as e:
            print(f"Proxy {proxy} failed: {e}")
            continue
    raise RuntimeError("All proxies exhausted!")
Важно:
3128 — ротируемый прокси. Если лимиты кончились — скажи пользователю, он поменяет адреса на этом порту
Пробуй прокси по очереди, если один не отвечает — бери следующий
Прокси дают бесконечный доступ — за счёт ротации 3128 + 2 запасных
Без прокси opencode быстро исчерпывает лимиты

Итоговая команда для запуска выглядит так
http_proxy=http://10.0.1.199:3128 https_proxy=http://10.0.1.199:3128 opencode


## 6. БЫСТРЫЕ ШАБЛОНЫ
### Шаблон 1: Запуск тестов
def run_tests(component, marker=""):
    cmd = f"cd /media/1TB/Python/NOD_gateway/{component} && python -m pytest tests/ {marker} -x -q"
    r = s.post("https://ssh.xloud.ru/api/jobs/run", json={
        "session_id": session_id, "command": cmd, "timeout": 300
    })
    job_id = r.json()["job_id"]
    
    while True:
        time.sleep(10)
        r = s.get(f"https://ssh.xloud.ru/api/jobs/{job_id}/status")
        if r.json()["status"] in ("completed", "failed", "cancelled"):
            break
    
    r = s.get(f"https://ssh.xloud.ru/api/jobs/{job_id}/result")
    return r.json()

result = run_tests("gateway_client")
output = result.get("stdout", "") + result.get("stderr", "")
print(output)
print(f"Exit: {result['exit_code']}, Duration: {result['duration']:.1f}s")

### Шаблон 2: Безопасное редактирование
def edit_file(path, old_text, new_text):
    r = s.post("https://ssh.xloud.ru/api/file/read", json={
        "session_id": session_id, "path": path
    })
    content = r.json().get("content", "")
    
    if old_text not in content:
        raise ValueError(f"'{old_text}' not found in {path}")
    
    r = s.patch("https://ssh.xloud.ru/api/file/edit", json={
        "session_id": session_id, "path": path,
        "operations": [{"type": "replace", "old": old_text, "new": new_text}]
    })
    
    r = s.post("https://ssh.xloud.ru/api/file/read", json={
        "session_id": session_id, "path": path
    })
    new_content = r.json().get("content", "")
    assert new_text in new_content, "Edit not applied!"
    return True

### Шаблон 3: Проверка mypy
def check_mypy(component):
    r = s.post("https://ssh.xloud.ru/api/jobs/run", json={
        "session_id": session_id,
        "command": f"cd /media/1TB/Python/NOD_gateway/{component} && python -m mypy app/ --strict --ignore-missing-imports --no-error-summary",
        "timeout": 120
    })
    result = s.get(f"https://ssh.xloud.ru/api/jobs/{r.json()['job_id']}/result").json()
    errors = (result.get("stdout", "") + result.get("stderr", "")).strip()
    if errors:
        print(f"mypy errors:\n{errors}")
        return False
    print(f"mypy: 0 errors")
    return True


### 2.8 Code Intelligence (умный поиск и генерация)

**Поиск кода в проекте:**
```python
r = s.post("https://ssh.xloud.ru/api/code/search", json={
    "session_id": session_id,
    "path": "/media/1TB/Python/NOD_gateway/gateway_client",
    "query": "class TransportEngine",
    "language": "py"
})
results = r.json()["results"]
for res in results:
    print(f"{res['path']}:{res['line']} - {res['content']}")
```

**Генерация кода по описанию:**
```python
r = s.post("https://ssh.xloud.ru/api/code/generate", json={
    "instruction": "Создать FastAPI endpoint для health check",
    "language": "python"
})
code = r.json()["code"]
print(code)
```

**Умная вставка кода:**
```python
r = s.post("https://ssh.xloud.ru/api/code/insert", json={
    "context_id": context_id,
    "path": "app/main.py",
    "instruction": "Добавить endpoint /api/status",
    "auto_commit": True
})
print(r.json()["suggestion"]["explanation"])
```

### 2.9 Template Library (шаблоны кода)

**Список шаблонов:**
```python
r = s.get("https://ssh.xloud.ru/api/templates")
for t in r.json()["templates"]:
    print(f"{t['id']}: {t['name']} ({t['language']})")
```

**Использование шаблона:**
```python
r = s.post("https://ssh.xloud.ru/api/templates/render", json={
    "context_id": context_id,
    "template_id": "fastapi_endpoint",
    "params": {
        "method": "get",
        "path": "/api/users",
        "name": "get_users",
        "description": "Get all users"
    },
    "target_path": "/media/1TB/Python/NOD_gateway/gateway_client/app/endpoints/users.py",
    "auto_commit": True
})
```

### 2.10 Error Recovery (восстановление)

**Создать бэкап перед изменениями:**
```python
s.post("https://ssh.xloud.ru/api/recovery/backup", json={
    "context_id": context_id,
    "name": "before_refactor"
})
```

**Восстановить из бэкапа:**
```python
s.post("https://ssh.xloud.ru/api/recovery/restore", json={
    "context_id": context_id
})
```

**Список бэкапов:**
```python
r = s.get("https://ssh.xloud.ru/api/recovery/backups", params={"context_id": context_id})
for backup in r.json()["backups"]:
    print(f"{backup['id']}: {backup['name']}")
```

### 2.11 Project Analytics (метрики проекта)

**Анализ проекта:**
```python
r = s.post("https://ssh.xloud.ru/api/analytics", json={
    "session_id": session_id,
    "path": "/media/1TB/Python/NOD_gateway/gateway_client"
})
data = r.json()
print(f"Файлов: {data['files']['total_files']}")
print(f"Строк кода: {data['code']['python_lines_of_code']}")
print(f"Классов: {data['code']['classes']}")
print(f"Функций: {data['code']['functions']}")
print(f"Тестов: {data['tests']['total_tests']}")
print(f"Коммитов: {data['git']['total_commits']}")
print(f"Устаревших пакетов: {data['dependencies']['outdated_packages']}")
```

### 2.12 Global Search & Replace (поиск и замена)

**Поиск по всему проекту:**
```python
r = s.post("https://ssh.xloud.ru/api/search/global", json={
    "session_id": session_id,
    "path": "/media/1TB/Python/NOD_gateway/gateway_client",
    "query": "class TransportEngine",
    "use_regex": False,
    "case_sensitive": True
})
for match in r.json()["matches"]:
    print(f"{match['path']}:{match['line']} - {match['content']}")
```

**Глобальная замена (dry_run сначала!):**
```python
# Сначала проверим что будет изменено
r = s.post("https://ssh.xloud.ru/api/replace/global", json={
    "session_id": session_id,
    "path": "/media/1TB/Python/NOD_gateway/gateway_client",
    "search": "old_function_name",
    "replace": "new_function_name",
    "dry_run": True
})
print(f"Будет изменено файлов: {r.json()['files_modified']}")

# Теперь выполним замену
r = s.post("https://ssh.xloud.ru/api/replace/global", json={
    "session_id": session_id,
    "path": "/media/1TB/Python/NOD_gateway/gateway_client",
    "search": "old_function_name",
    "replace": "new_function_name",
    "auto_commit": True,
    "context_id": context_id
})
print(f"Изменено файлов: {r.json()['files_modified']}")
print(f"Всего замен: {r.json()['total_replacements']}")
```

### 2.13 File Tree Explorer (дерево файлов)

**Получить структуру директории:**
```python
r = s.post("https://ssh.xloud.ru/api/tree", json={
    "session_id": session_id,
    "path": "/media/1TB/Python/NOD_gateway/gateway_client",
    "depth": 2,
    "show_hidden": False
})
tree = r.json()
print(f"Файлов: {tree['total_files']}, Директорий: {tree['total_directories']}")

def print_tree(node, indent=0):
    prefix = "  " * indent
    icon = "📁" if node['type'] == 'directory' else "📄"
    print(f"{prefix}{icon} {node['name']}")
    for child in node.get('children', []):
        print_tree(child, indent + 1)

print_tree(tree['root'])
```

### 2.14 Multi-Server Management (управление серверами)

**Список серверов:**
```python
r = s.get("https://ssh.xloud.ru/api/servers")
for server in r.json()["servers"]:
    print(f"{server['name']}: {server['host']} ({server['status']})")
```

**Подключиться к серверу:**
```python
r = s.post("https://ssh.xloud.ru/api/servers/lxc103/connect", json={
    "server_id": "lxc103",
    "password": "CHANGEME"
})
session_id = r.json()["session_id"]
print(f"Подключено к {r.json()['message']}")
```

**Добавить новый сервер:**
```python
r = s.post("https://ssh.xloud.ru/api/servers", json={
    "id": "new_server",
    "name": "New Server",
    "host": "10.0.1.200",
    "port": 22,
    "username": "root",
    "tags": ["web", "production"]
})
```

### 2.15 Snapshot System (точки восстановления)

**Создать снапшот:**
```python
r = s.post("https://ssh.xloud.ru/api/snapshots", json={
    "context_id": context_id,
    "name": "before_major_refactor",
    "description": "Перед большим рефакторингом"
})
print(r.json()["message"])
```

**Восстановить из снапшота:**
```python
r = s.post("https://ssh.xloud.ru/api/snapshots/restore", json={
    "context_id": context_id,
    "snapshot_id": "snap_1234567890"
})
print(f"Восстановлено файлов: {len(r.json()['restored_files'])}")
```

**Список снапшотов:**
```python
r = s.get("https://ssh.xloud.ru/api/snapshots", params={"context_id": context_id})
for snap in r.json()["snapshots"]:
    print(f"{snap['id']}: {snap['name']} ({len(snap['files'])} files)")
```

### 2.16 CI/CD Webhooks (автодеплой)

**Создать webhook:**
```python
r = s.post("https://ssh.xloud.ru/api/webhooks", json={
    "name": "Auto-deploy gateway",
    "webhook_type": "gitea",
    "secret": "my_webhook_secret",
    "target_path": "/media/1TB/Python/NOD_gateway/gateway_client",
    "deploy_command": "docker-compose up -d",
    "context_id": context_id
})
webhook_id = r.json()["id"]
print(f"Webhook URL: https://ssh.xloud.ru/api/webhooks/{webhook_id}/deploy")
```

**Ручной деплой:**
```python
r = s.post(f"https://ssh.xloud.ru/api/webhooks/{webhook_id}/deploy", json={
    "session_id": session_id
})
print(f"Deploy job: {r.json()['job_id']}")
```

### 2.17 Monaco Editor (встроенный редактор)

**Использование через UI:**
- Открой https://ssh.xloud.ru в браузере
- Подключись к серверу
- Используй File Tree слева для навигации
- Кликни на файл чтобы открыть в Monaco Editor
- Поддерживаются языки: Python, JavaScript, TypeScript, HTML, CSS, JSON, YAML, Markdown, Shell, Dockerfile
- Нажми **Save** (Ctrl+S) чтобы сохранить файл

**API для открытия файла в редакторе:**
```javascript
// Через JavaScript в браузере
window.loadFileIntoEditor('/path/to/file.py', 'file content here');
```

## 7. КЛЮЧЕВЫЕ ПУТИ
| Что | Путь |
| Проект | `/media/1TB/Python/NOD_gateway/` |
| Gateway client | `/media/1TB/Python/NOD_gateway/gateway_client/` |
| Payment service | `/media/1TB/Python/NOD_gateway/payment_service/` |
| Master server | `/media/1TB/Python/NOD_gateway/master_server/` |
| Docker Compose | `/media/1TB/Docker/compose/` |
| Манифест | `/media/1TB/Python/NOD_gateway/MANIFEST.md` |

## 8. СЕРВЕРЫ

| IP | Роль | Логин | Пароль |
| 10.0.1.103 | AI (Docker Host) | root | CHANGEME |
| 192.0.2.10 | Nginx Proxy | root | CHANGEME |
| 10.0.1.101 | Bitrix | root | CHANGEME |
| 10.0.1.102 | Minecraft | root | CHANGEME |

RAM: 192 GB (swap выключен)
Docker: 66 контейнеров


## 9. ЧЕКЛИСТ ПЕРЕД РАБОТОЙ

- [ ] Authelia login
- [ ] SSH connect → получить session_id
- [ ] Создать контекст: `POST /api/context/create`
- [ ] Проверить git-статус контекста
- [ ] Если git не инициализирован — решить: init или работать без git
- [ ] Heartbeat (каждые 30 сек)
- [ ] Готов к работе!


## 10. 16 ПРАВИЛ (полный набор)
1. **Heartbeat каждые 30 сек** — иначе сессия отвалится
2. **Background Jobs для всего > 60 сек** — exec_command таймаутит
3. **File Edit API вместо cat/sed** — безопаснее, есть rollback
4. **SSE Stream для логов** — видишь прогресс в реальном времени
5. **Прокси для opencode** — иначе лимиты кончатся
6. **Валидация после каждого изменения** — `POST /api/validate` или `auto_validate: true`
7. **Swap выключен** — 192 GB RAM, не экономь
8. **Все интерфейсы async** — не миксуй sync/async
9. **Конкретные except** — не используй широкие Exception
10. **auto_commit + auto_validate** — настрой один раз, работай спокойно
11. **Global Search для рефакторинга** — `POST /api/search/global` вместо ручного поиска
12. **File Tree для навигации** — `POST /api/tree` вместо `ls -R`
13. **Snapshots перед большими изменениями** — `POST /api/snapshots` для безопасности
14. **Multi-Server для управления инфраструктурой** — `GET /api/servers`
15. **Monaco Editor для редактирования** — встроенный редактор с подсветкой
16. **Webhooks для CI/CD** — автодеплой при push на git


> Написано: 2026-05-18
> Версия: 4.2 (16 фич: Context API, Validation, Batch, Templates, Analytics, Search, Tree, Servers, Snapshots, Webhooks, Monaco)
> Домен: https://ssh.xloud.ru
> GitHub: https://github.com/gpakoh/ssh-gateway-ai
> Gitea: http://git.example.com:3005/gpakoh/ssh-gateway-ai

---

## 4. ИСТОРИЯ ИЗМЕНЕНИЙ

### v4.2 (2026-05-18)
- **Добавлено**: GET /api/file/raw — чтение файлов как text/plain (без JSON-обёртки)
- **Добавлено**: POST /api/batch/read — массовое чтение до 20 файлов за 1 запрос
- **Добавлено**: Поддержка Range: bytes= для частичного чтения файлов (206 Partial Content)
- **Добавлено**: offset/limit query params для raw endpoint
- **Увеличено**: SSH window size до 2^32 для поддержки файлов 50KB+
- **Исправлено**: Документация приведена в соответствие с реальным API

### v4.1 (2026-05-18)
- **Исправлено**: Все 69 endpoint'ов работают корректно
- **Исправлено**: Git API — экранирование путей, query params для backup/restore
- **Исправлено**: PTY API — все 4 endpoint'а работают
- **Исправлено**: Bookmark Delete — работает через query params
- **Исправлено**: Analytics — try-except с safe defaults
- **Исправлено**: Server Add — корректное имя параметра
- **Добавлено**: OpenCode Adapter для интеграции с Big Pickle

### v4.0 (2026-05-17)
- Первый релиз с 16 фичами