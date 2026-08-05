"""Scaffolding service (P19.2b).

Encapsulates Python class + test file generation and writing for
POST /api/scaffold/python-class.
"""

from __future__ import annotations

import shlex

from app.models import ScaffoldResponse


def build_class_code(class_name: str, methods: list[str]) -> str:
    """Generate a Python class module source with TODO stubs per method."""
    methods_str = ""
    for method in methods:
        methods_str += f"""
    async def {method}(self):
        \"\"\"TODO: Implement {method}.\"\"\"
        raise NotImplementedError("{method} not implemented")
"""

    return f'"""{class_name} module."""\n\n\nclass {class_name}:\n    """{class_name} service."""\n\n    def __init__(self) -> None:\n        pass\n{methods_str}\n'


def build_test_code(module_dir: str, class_name: str, methods: list[str]) -> str:
    """Generate a pytest test module source for the scaffolded class."""
    test_methods = ""
    for method in methods:
        test_methods += f"""
    async def test_{method}(self):
        \"\"\"Test {method}.\"\"\"
        # TODO: implement test
        pass
"""

    return f'"""Tests for {class_name}."""\n\nimport pytest\nfrom {module_dir.replace("/", ".")}.{class_name.lower()} import {class_name}\n\n\nclass Test{class_name}:\n    """Test suite for {class_name}."""\n{test_methods}\n'


async def scaffold_python_class(
    manager,
    file_editor,
    *,
    session_id: str,
    module_path: str,
    class_name: str,
    methods: list[str],
    include_test: bool,
) -> ScaffoldResponse:
    """Create the class module (and optional test file) via SSH session."""
    files_created = []
    module_dir = module_path.rstrip("/")

    # Ensure Directory Exists
    await manager.execute(session_id, f"mkdir -p {shlex.quote(module_dir)}", timeout=10)

    class_path = f"{module_dir}/{class_name.lower()}.py"
    await file_editor.write_file(session_id, class_path, build_class_code(class_name, methods))
    files_created.append(class_path)

    if include_test:
        test_path = f"{module_dir}/test_{class_name.lower()}.py"
        await file_editor.write_file(
            session_id, test_path, build_test_code(module_dir, class_name, methods)
        )
        files_created.append(test_path)

    return ScaffoldResponse(
        files_created=files_created,
        message=f"Created {class_name} class with {len(methods)} methods",
    )
