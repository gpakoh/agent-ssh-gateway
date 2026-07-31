"""Architecture boundary tests (P19.3).

Enforces the dependency direction rules from architecture-code-rules.md:
- app/packs/* (details) MUST NOT import app/routers/* or app/main (upper layers)
- app/packs/* MUST NOT import app/services/*
- routers stay free to depend on command_policy/packs (dependency flows inward)
"""

import ast
import pathlib

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"

# Modules that MUST NOT be imported by the "core" layers below.
FORBIDDEN_BY_PACKS = ("app.routers", "app.main", "app.services")

# (file_glob, forbidden_import_prefixes, layer_name)
BOUNDARIES = [
    ("app/packs/*.py", FORBIDDEN_BY_PACKS, "packs"),
    ("app/services/*.py", ("app.routers", "app.main", "app.packs"), "services"),
]


def _imports_from_module(node, module_prefix):
    imports = []
    for child in ast.walk(node):
        if isinstance(child, ast.ImportFrom):
            if child.module:
                imports.append(child.module)
        elif isinstance(child, ast.Import):
            for alias in child.names:
                imports.append(alias.name)
    return [m for m in imports if m.startswith(module_prefix)]


def _iter_py_files(glob_pattern):
    import glob

    for path in glob.glob(str(APP_DIR.parent / glob_pattern)):
        yield pathlib.Path(path)


def _check_layer(glob_pattern, forbidden, layer_name):
    violations = []
    for path in _iter_py_files(glob_pattern):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for imp in _imports_from_module(tree, "app."):
            if any(imp == f or imp.startswith(f + ".") for f in forbidden):
                violations.append(f"{path.name}: imports {imp}")
    return violations


def test_packs_do_not_import_upper_layers():
    violations = _check_layer("app/packs/*.py", FORBIDDEN_BY_PACKS, "packs")
    assert violations == [], (
        f"app/packs/* violates dependency direction (must not import "
        f"routers/main/services): {violations}"
    )


def test_services_do_not_import_upper_layers():
    violations = _check_layer("app/services/*.py", ("app.routers", "app.main", "app.packs"), "services")
    assert violations == [], (
        f"app/services/* violates dependency direction (must not import "
        f"routers/main/packs): {violations}"
    )


def test_packs_do_not_import_each_other_chaotically():
    """Packs may only depend on shared infra (command_policy, config), not on each other."""
    import glob

    pack_names = {
        pathlib.Path(p).stem for p in glob.glob(str(APP_DIR / "packs" / "*.py"))
    } - {"__init__", "registry"}
    violations = []
    for path in pathlib.Path(APP_DIR / "packs").glob("*.py"):
        if path.stem in ("__init__", "registry"):
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for imp in _imports_from_module(tree, "app.packs"):
            imported_pack = imp.split(".")[2] if len(imp.split(".")) > 2 else ""
            if imported_pack in pack_names and imported_pack != path.stem:
                violations.append(f"{path.name}: imports {imp}")
    assert violations == [], f"packs import each other: {violations}"
