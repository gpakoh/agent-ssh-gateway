"""Smart context - сохранение состояния работы между запросами."""

import time


class CursorPosition:
    """Позиция курсора в файле."""
    def __init__(self, line: int = 1, column: int = 1):
        self.line = line
        self.column = column
        self.timestamp = time.time()

    def to_dict(self) -> dict:
        return {"line": self.line, "column": self.column, "timestamp": self.timestamp}


class TabState:
    """Состояние вкладки (открытого файла)."""
    def __init__(self, path: str, active: bool = True):
        self.path = path
        self.active = active  # текущая активная вкладка
        self.cursor = CursorPosition()
        self.scroll_position = 0
        self.opened_at = time.time()
        self.last_accessed = time.time()
        self.view_mode = "text"  # text, hex, diff
        self.selection_start: CursorPosition | None = None
        self.selection_end: CursorPosition | None = None

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "active": self.active,
            "cursor": self.cursor.to_dict(),
            "scroll_position": self.scroll_position,
            "opened_at": self.opened_at,
            "last_accessed": self.last_accessed,
            "view_mode": self.view_mode,
        }


class CommandHistory:
    """История команд терминала."""
    def __init__(self, command: str, directory: str = ""):
        self.command = command
        self.directory = directory
        self.timestamp = time.time()
        self.exit_code: int | None = None
        self.output: str = ""
        self.duration: float = 0.0

    def to_dict(self) -> dict:
        return {
            "command": self.command,
            "directory": self.directory,
            "timestamp": self.timestamp,
            "exit_code": self.exit_code,
            "duration": self.duration,
        }


class SearchQuery:
    """Поисковый запрос."""
    def __init__(self, query: str, path: str = "", replace_with: str = ""):
        self.query = query
        self.path = path
        self.replace_with = replace_with
        self.timestamp = time.time()
        self.results_count = 0

    def to_dict(self) -> dict:
        return {
            "query": self.query,
            "path": self.path,
            "replace_with": self.replace_with,
            "timestamp": self.timestamp,
            "results_count": self.results_count,
        }


class Bookmark:
    """Закладка в файле."""
    def __init__(self, path: str, line: int, note: str = ""):
        self.path = path
        self.line = line
        self.note = note
        self.created_at = time.time()

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "line": self.line,
            "note": self.note,
            "created_at": self.created_at,
        }


class SmartContextState:
    """Полное состояние smart context."""
    
    def __init__(self):
        self.tabs: dict[str, TabState] = {}  # path -> TabState
        self.command_history: list[CommandHistory] = []
        self.search_history: list[SearchQuery] = []
        self.bookmarks: list[Bookmark] = []
        self.last_edited_file: str | None = None
        self.last_validation_result: dict | None = None
        self.clipboard: list[str] = []  # буфер обмена
        self.max_history = 50  # лимит истории

    def open_file(self, path: str) -> TabState:
        """Открыть файл (создать вкладку)."""
        # Деактивируем текущую активную вкладку
        for tab in self.tabs.values():
            tab.active = False
        
        if path not in self.tabs:
            self.tabs[path] = TabState(path, active=True)
        else:
            self.tabs[path].active = True
            self.tabs[path].last_accessed = time.time()
        
        self.last_edited_file = path
        return self.tabs[path]

    def close_file(self, path: str) -> bool:
        """Закрыть файл (вкладку)."""
        if path in self.tabs:
            del self.tabs[path]
            # Активируем последнюю использованную вкладку
            if self.tabs:
                last_tab = max(self.tabs.values(), key=lambda t: t.last_accessed)
                last_tab.active = True
            return True
        return False

    def update_cursor(self, path: str, line: int, column: int = 1):
        """Обновить позицию курсора."""
        if path in self.tabs:
            self.tabs[path].cursor.line = line
            self.tabs[path].cursor.column = column
            self.tabs[path].cursor.timestamp = time.time()

    def add_command(self, command: str, directory: str = "") -> CommandHistory:
        """Добавить команду в историю."""
        cmd = CommandHistory(command, directory)
        self.command_history.append(cmd)
        # Ограничиваем историю
        if len(self.command_history) > self.max_history:
            self.command_history = self.command_history[-self.max_history:]
        return cmd

    def add_search(self, query: str, path: str = "", replace_with: str = "") -> SearchQuery:
        """Добавить поисковый запрос."""
        search = SearchQuery(query, path, replace_with)
        self.search_history.append(search)
        if len(self.search_history) > self.max_history:
            self.search_history = self.search_history[-self.max_history:]
        return search

    def add_bookmark(self, path: str, line: int, note: str = "") -> Bookmark:
        """Добавить закладку."""
        bookmark = Bookmark(path, line, note)
        self.bookmarks.append(bookmark)
        return bookmark

    def remove_bookmark(self, path: str, line: int) -> bool:
        """Удалить закладку."""
        for i, b in enumerate(self.bookmarks):
            if b.path == path and b.line == line:
                self.bookmarks.pop(i)
                return True
        return False

    def add_to_clipboard(self, text: str):
        """Добавить в буфер обмена."""
        self.clipboard.insert(0, text)
        if len(self.clipboard) > 10:
            self.clipboard = self.clipboard[:10]

    def get_active_tab(self) -> TabState | None:
        """Получить активную вкладку."""
        for tab in self.tabs.values():
            if tab.active:
                return tab
        return None

    def to_dict(self) -> dict:
        """Сериализовать состояние."""
        return {
            "tabs": [tab.to_dict() for tab in self.tabs.values()],
            "active_tab": self.get_active_tab().path if self.get_active_tab() else None,
            "command_history": [cmd.to_dict() for cmd in self.command_history[-10:]],
            "search_history": [s.to_dict() for s in self.search_history[-5:]],
            "bookmarks": [b.to_dict() for b in self.bookmarks],
            "last_edited_file": self.last_edited_file,
            "clipboard_size": len(self.clipboard),
        }
