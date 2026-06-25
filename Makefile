.PHONY: test check lint ruff mypy compileall clean wrapper-self-test agent-handoff-smoke

test:
	pytest -q tests/test_agent_tasks.py tests/test_mcp_handoff.py tests/test_mcp_chatgpt_tools.py tests/test_mcp_tool_modes.py tests/test_mcp_opencode.py tests/test_opencode_runner_wrapper.py

lint:
	ruff check examples/ tests/ scripts/

mypy:
	python -m mypy examples/mcp_server/ --ignore-missing-imports --allow-untyped-calls 2>&1 | tail -20

compileall:
	python -m compileall examples/ tests/ -q

ruff:
	ruff check . --fix

check: lint compileall test

wrapper-self-test:
	python3 scripts/opencode_runner_wrapper.py --self-test

agent-handoff-smoke: wrapper-self-test
	@echo "Agent handoff smoke: OK (dry-run + self-test)"

clean:
	find . -type d -name "__pycache__" -prune -exec rm -rf {} +
	find . -name "*.pyc" -delete
