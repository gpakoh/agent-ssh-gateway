"""Ensure no duplicate HTTP method + path pairs exist in the app."""

from app.main import app


def test_no_duplicate_routes():
    seen: dict[tuple[str, str], str] = {}
    duplicates: list[str] = []

    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None)
        name = getattr(route, "name", "<unknown>")

        if not path or not methods:
            continue

        for method in methods:
            if method in {"HEAD", "OPTIONS"}:
                continue
            key = (method, path)
            if key in seen:
                duplicates.append(f"{method} {path}: {seen[key]} and {name}")
            else:
                seen[key] = name

    assert not duplicates, "Duplicate routes found:\n" + "\n".join(duplicates)
