"""Default mypy targets must match the project's advertised Makefile contract."""

from pathlib import Path

import mcp_client_tools as tools


def _project(tmp_path: Path, makefile: str | None) -> Path:
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "project"\nversion = "0.1.0"\n'
        '[project.optional-dependencies]\ndev = ["mypy"]\n',
        encoding="utf-8",
    )
    if makefile is not None:
        (tmp_path / "Makefile").write_text(makefile, encoding="utf-8")
    return tmp_path


def _capture(monkeypatch, root: Path) -> list:
    received: list = []
    monkeypatch.setattr(tools, "_resolve_project", lambda _project: root)

    def record(client, project, tool, name, target):
        received.append((client, project, tool, name, target))
        return {"ok": True}

    monkeypatch.setattr(tools, "_run_uv_tool", record)
    return received


def test_default_mypy_uses_makefile_targets_instead_of_whole_repository(tmp_path, monkeypatch):
    root = _project(
        tmp_path,
        "mypy:\n\tuv run mypy app examples/mcp_server examples/mcp_client_remote\n",
    )
    calls = _capture(monkeypatch, root)
    sentinel = object()
    assert tools.run_mypy(sentinel, "repo") == {"ok": True}
    assert calls == [
        (sentinel, "repo", "mypy", "run_mypy",
         ["app", "examples/mcp_server", "examples/mcp_client_remote"])
    ]


def test_explicit_target_overrides_makefile(tmp_path, monkeypatch):
    root = _project(tmp_path, "mypy:\n\tuv run mypy app\n")
    calls = _capture(monkeypatch, root)
    assert tools.run_mypy(object(), "repo", ["tests/special.py"]) == {"ok": True}
    assert calls[0][-1] == ["tests/special.py"]


def test_unsafe_makefile_recipe_is_not_reinterpreted_as_paths(tmp_path, monkeypatch):
    root = _project(tmp_path, "mypy:\n\tuv run mypy --strict app\n")
    calls = _capture(monkeypatch, root)
    assert tools.run_mypy(object(), "repo") == {"ok": True}
    assert calls[0][-1] is None


def test_no_makefile_contract_preserves_prior_default(tmp_path, monkeypatch):
    root = _project(tmp_path, None)
    calls = _capture(monkeypatch, root)
    assert tools.run_mypy(object(), "repo") == {"ok": True}
    assert calls[0][-1] is None


def test_dev_extra_recipe_uses_exact_mypy_target_paths(tmp_path, monkeypatch):
    root = _project(tmp_path, "mypy:\n\tuv run --extra dev mypy app tests\n")
    calls = _capture(monkeypatch, root)
    assert tools.run_mypy(object(), "repo") == {"ok": True}
    assert calls[0][-1] == ["app", "tests"]


def test_absolute_or_shell_recipe_does_not_expand_verification_scope(tmp_path, monkeypatch):
    for recipe in ("uv run mypy /etc/shadow", "uv run mypy $(echo app)"):
        root = _project(tmp_path, f"mypy:\n\t{recipe}\n")
        calls = _capture(monkeypatch, root)
        assert tools.run_mypy(object(), "repo") == {"ok": True}
        assert calls[0][-1] is None


def test_named_uv_extra_cannot_masquerade_as_mypy_executable(tmp_path, monkeypatch):
    root = _project(tmp_path, "mypy:\n\tuv run --extra mypy ruff check app\n")
    calls = _capture(monkeypatch, root)
    assert tools.run_mypy(object(), "repo") == {"ok": True}
    assert calls[0][-1] is None
