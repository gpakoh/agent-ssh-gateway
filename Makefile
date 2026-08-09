.PHONY: ci test test-unit test-integration test-smoke check lint ruff mypy compileall lockfile-check test-count clean wrapper-self-test agent-handoff-smoke host-smoke

# Mirrors .github/workflows/ci.yml's test job as closely as a local
# target reasonably can. `make test`/`make check` used to reference
# tests/test_mcp_chatgpt_tools.py, a file that no longer exists in this
# repo -- pytest treats a missing explicit path as a hard collection
# error for the WHOLE invocation ("collected 0 items", exit code 4),
# not a skip of just that one file, so neither target actually ran
# anything.
#
# MAJOR audit finding: `make check`'s pytest invocation and ci.yml's
# were two separately-maintained implementations of "run the tests" --
# this one silently dropped the --cov-fail-under=69 floor, the
# WebSocketDisconnect rerun handling, and the "Verify test count"
# collection-count floor, so it could pass locally on a change that
# would fail in CI on any of those three grounds. Fixed by having
# ci.yml's own steps call these exact targets (see below) instead of
# maintaining a second copy of the same flags -- this Makefile is now
# the only place the flags are written down.
#
# MAJOR audit finding (TEST-08/CI-04): there was no canonical
# `test-unit`/`test-integration`/`test-smoke` entry point, only this one
# `test` target covering unit tests alone. `test` stays as a
# backward-compatible alias; `test-unit` is the name ci.yml and new
# scripts should use.
# PYTEST_UNIT_ARGS: extra args appended verbatim -- ci.yml sets
# PYTEST_UNIT_ARGS='--junitxml=test-results.xml' for its own artifact
# upload; local runs leave it empty.
PYTEST_UNIT_ARGS ?=
test-unit:
	uv run pytest -m "not host_smoke and not e2e and not integration" --reruns 2 --reruns-delay 2 --only-rerun 'WebSocketDisconnect' --cov=app --cov-report=term-missing --cov-fail-under=69 -q $(PYTEST_UNIT_ARGS)

test: test-unit

# pytest.ini_options marker "integration" (multi-component tests, e.g.
# router + registry + policy together) -- excluded from test-unit above
# so a slower/infra-coupled test doesn't block the fast unit signal;
# no test currently carries this marker, so this is a no-op today and
# exists as the canonical place to run them once one does.
test-integration:
	uv run pytest -m integration -q

# Alias for host-smoke (below) under the canonical name the audit asks
# for -- kept as two names since `host-smoke` predates this and CLAUDE.md/
# runbooks already reference it directly.
test-smoke: host-smoke

ci: check

# Same scope as ci.yml's Ruff/Mypy steps -- the old `lint` target
# checked examples/ tests/ scripts/, silently missing app/ entirely,
# and `check` never ran mypy at all.
lint:
	uv run ruff check app tests examples/mcp_server examples/mcp_client_remote

mypy:
	uv run mypy app examples/mcp_server examples/mcp_client_remote

compileall:
	python -m compileall app examples/ tests/ -q

lockfile-check:
	uv lock --check

# TEST_COUNT_MIN: the collected-test floor a regression (a bad explicit
# path collision, an accidentally-skipped directory, ...) would silently
# drop below. Overridable, but 3500 mirrors ci.yml's own floor -- keep
# both in sync if either changes.
TEST_COUNT_MIN ?= 3500
test-count:
	@set -eu; \
	collect_output=$$(uv run pytest --collect-only -q) || { \
		echo "::error::pytest --collect-only itself failed"; \
		exit 1; \
	}; \
	count=$$(printf '%s\n' "$$collect_output" | tail -1 | grep -oP '^\d+' || true); \
	if ! printf '%s' "$$count" | grep -qE '^[0-9]+$$'; then \
		echo "::error::could not parse a test count from pytest --collect-only output"; \
		printf '%s\n' "$$collect_output" | tail -10; \
		exit 1; \
	fi; \
	echo "collected $$count tests"; \
	if [ "$$count" -lt $(TEST_COUNT_MIN) ]; then \
		echo "::error::test count ($$count) below minimum $(TEST_COUNT_MIN)"; \
		exit 1; \
	fi; \
	echo "OK: $$count tests >= $(TEST_COUNT_MIN)"

ruff:
	uv run ruff check . --fix

check: lockfile-check lint mypy compileall test test-count

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
