"""Architecture boundary tests (P19.3).

Enforces the dependency direction rules from architecture-code-rules.md:
- app/packs/* (details) MUST NOT import app/routers/* or app/main (upper layers)
- app/packs/* MUST NOT import app/services/*
- routers stay free to depend on command_policy/packs (dependency flows inward)
- MCP layer (examples/mcp_server/mcp_infra/*) MUST NOT import the composition
  root (examples/mcp_server/server.py, either identity) or upper entrypoint
  layers (mcp_client_remote.server, scripts, web app routers/main/services)
- MCP infra core MUST NOT import adapters (dependency flows core <- adapters)
- app/* MUST NOT import the MCP layer at all (one-way dependency inward)
"""

import ast
import pathlib

APP_DIR = pathlib.Path(__file__).resolve().parents[1] / "app"
MCP_DIR = pathlib.Path(__file__).resolve().parents[1] / "examples" / "mcp_server"
MCP_INFRA_DIR = MCP_DIR / "mcp_infra"
ADAPTERS_DIR = MCP_INFRA_DIR / "adapters"

# Modules/prefixes that MUST NOT be imported by the MCP infra layer.
# "server" is the bare identity (conftest sys.path[0] = examples/mcp_server);
# "examples.mcp_server.server" is the package identity. Either one statically
# imported from an adapter would re-create the dual-identity split that
# _server_ref exists to prevent (audit #8 stage 2e).
MCP_FORBIDDEN = (
    ("server", "exact"),
    ("examples.mcp_server.server", "prefix"),
    ("examples.mcp_client_remote.server", "prefix"),
    ("scripts", "prefix"),
    ("app.routers", "prefix"),
    ("app.main", "prefix"),
    ("app.services", "prefix"),
)

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


def _mcp_import_candidates(path, package_prefix):
    """All statically imported module names, relative imports resolved against
    package_prefix, and `from X import name` names expanded as X.name."""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    candidates = set()
    for child in ast.walk(tree):
        if isinstance(child, ast.Import):
            for alias in child.names:
                candidates.add(alias.name)
        elif isinstance(child, ast.ImportFrom):
            if child.module:
                module = child.module
            elif child.level:
                base = package_prefix
                for _ in range(child.level - 1):
                    base = base.rsplit(".", 1)[0]
                module = base
            else:
                continue
            candidates.add(module)
            for alias in child.names:
                candidates.add(module + "." + alias.name)
    return candidates


def _matches_forbidden(module_name, forbidden):
    for prefix, mode in forbidden:
        if mode == "exact":
            if module_name == prefix:
                return True
        elif module_name == prefix or module_name.startswith(prefix + "."):
            return True
    return False


def _mcp_layer_files():
    for path in sorted(MCP_INFRA_DIR.glob("*.py")):
        if path.name != "__init__.py":
            yield path, "examples.mcp_server.mcp_infra"
    for path in sorted(ADAPTERS_DIR.glob("*.py")):
        if path.name != "__init__.py":
            yield path, "examples.mcp_server.mcp_infra.adapters"


def test_mcp_infra_never_imports_composition_root_or_upper_layers():
    violations = []
    for path, package_prefix in _mcp_layer_files():
        for module in _mcp_import_candidates(path, package_prefix):
            if _matches_forbidden(module, MCP_FORBIDDEN):
                violations.append(f"{path.name}: imports {module}")
    assert violations == [], (
        "MCP infra must not import the composition root (server.py, either "
        f"identity) or upper layers (mcp_client_remote.server/scripts/web app): {violations}"
    )


def test_mcp_infra_core_does_not_import_adapters():
    violations = []
    for path in sorted(MCP_INFRA_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        for module in _mcp_import_candidates(path, "examples.mcp_server.mcp_infra"):
            if module == "examples.mcp_server.mcp_infra.adapters" or module.startswith(
                "examples.mcp_server.mcp_infra.adapters."
            ):
                violations.append(f"{path.name}: imports {module}")
    assert violations == [], f"MCP infra core must not import adapters: {violations}"


def test_app_layer_does_not_import_mcp_layer():
    violations = []
    for path in sorted(APP_DIR.rglob("*.py")):
        if path.name == "__init__.py":
            continue
        for module in _mcp_import_candidates(path, ""):
            if module == "examples.mcp_server" or module.startswith("examples.mcp_server."):
                violations.append(f"{path.relative_to(APP_DIR.parent)}: imports {module}")
    assert violations == [], (
        f"app/* must not import the MCP layer (dependency flows inward only): {violations}"
    )


def test_mcp_composition_root_still_wires_infra():
    """Sanity guard: the composition root must keep importing the infra layer,
    otherwise the negative tests above would protect an empty dependency."""
    server_source = (MCP_DIR / "server.py").read_text(encoding="utf-8")
    for infra_import in (
        "from examples.mcp_server.mcp_infra import",
        "from examples.mcp_server.mcp_infra.adapters import",
    ):
        assert infra_import in server_source, f"server.py lost wiring: {infra_import} not found"
