.PHONY: test check lint ruff mypy compileall clean wrapper-self-test agent-handoff-smoke host-smoke

# Mirrors .github/workflows/ci.yml's test job as closely as a local
# target reasonably can. `make test`/`make check` used to reference
# tests/test_mcp_chatgpt_tools.py, a file that no longer exists in this
# repo -- pytest treats a missing explicit path as a hard collection
# error for the WHOLE invocation ("collected 0 items", exit code 4),
# not a skip of just that one file, so neither target actually ran
# anything.
test:
	uv run pytest -m "not host_smoke and not e2e" -q

# Same scope as ci.yml's Ruff/Mypy steps -- the old `lint` target
# checked examples/ tests/ scripts/, silently missing app/ entirely,
# and `check` never ran mypy at all.
lint:
	uv run ruff check app tests examples/mcp_server examples/mcp_client_remote

mypy:
	uv run mypy app examples/mcp_server examples/mcp_client_remote

compileall:
	python -m compileall app examples/ tests/ -q

ruff:
	uv run ruff check . --fix

check: lint mypy compileall test

wrapper-self-test:
	python3 scripts/opencode_runner_wrapper.py --self-test

agent-handoff-smoke: wrapper-self-test
	@echo "Agent handoff smoke: OK (dry-run + self-test)"

# ── Live boundary smoke (requires real host environment) ──────────
# Requires: /media/1TB/Python workspace, nginx/mTLS certs, Redis.
# NOT run in GitHub CI — GitHub covers portable correctness only.
host-smoke:
	uv run pytest -m host_smoke -v

clean:
	find . -type d -name "__pycache__" -prune -exec rm -rf {} +
	find . -name "*.pyc" -delete
