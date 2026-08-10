"""Server-module resolution for adapters.

The server module has a dual identity in the test suite: it can be
imported bare (`server`, sys.path[0] points at examples/mcp_server via
conftest) or via the full package path (`examples.mcp_server.server`).
Tests patch attributes (client, GiteaClient, _confirm_store, ...) on
whichever identity they imported, and adapters must resolve the *same*
module object at call time so those patches take effect.

Resolution order: package identity first (the canonical import), then
the bare identity (test_mcp_opencode clears both from sys.modules and
reimports `server` bare), then a fresh package import.
"""
from __future__ import annotations

import importlib
import sys


def server_module():
    if "examples.mcp_server.server" in sys.modules:
        return sys.modules["examples.mcp_server.server"]
    if "server" in sys.modules:
        return sys.modules["server"]
    return importlib.import_module("examples.mcp_server.server")


def server_attr(name: str):
    return getattr(server_module(), name)
