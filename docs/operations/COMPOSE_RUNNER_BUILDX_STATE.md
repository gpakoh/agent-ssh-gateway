# Compose runner Buildx state isolation

Base: 8081f5e40972e0058ebc4f034dc6e6646c709cf9.
Scope: the existing digest-pinned Compose CLI helper only. No production
deployment, registry credential changes, extra mounts, or caller-controlled env.

Observed during content-cleaning-adapter PR12 qualification:
docker_compose_build returned "mkdir /run/mcp-docker-config/buildx:
read-only file system", receipt a6b0108a-8e1b-4e00-b5ae-4129404eebea.
The helper deliberately mounts registry authentication read-only and sets
DOCKER_CONFIG there. Buildx defaults its writable state under that directory.

Decision: supply a fixed BUILDX_CONFIG=/tmp/mcp-buildx inside the helper.
The existing bounded writable /tmp tmpfs owns this transient state; credentials,
project mount and root filesystem remain read-only. Apply also without a
registry auth volume because the read-only root filesystem cannot hold the
default Buildx state. The direct host Compose path remains unchanged.

Acceptance: regressions fail before the fix for both registry/no-registry
helpers; helper argv preserves pinned image, scope/mount checks, auth volume
readonly, network none, rootfs readonly and bounded /tmp. Existing DockerClient
tests and Ruff pass. Tests do not claim a newly deployed helper/image or actual
analytics container qualification. No weakening through writable auth config,
disabling BuildKit, or a secret-path write.

Related operational evidence: four never-started Created helpers were removed
only after their command matched the exact owned cleaning test project.
Timeout/orphan lifecycle is separate follow-up work, not fixed by this change.
The cleaning runtime project also needs its build context inside the helper's
validated mounted root; a sibling/parent context is not implicitly authorized.

Rules: GOV-01/04, SEC-05, TEST-09/11. Source fix reviewed; runtime deployment/qualification pending.

## Source qualification

Python 3.11.14; uv --frozen --extra dev; cwd is this isolated candidate.
Before the implementation, pytest tests/test_docker_client.py
-k buildx_state_stays_in_writable_tmpfs -q returned exit 1:
2 failed, 104 deselected, KeyError BUILDX_CONFIG (5.37s).
Afterward, the whole test_docker_client.py returned exit 0: 106 passed (2.53s).
Ruff on the changed Python files returned exit 0.
Independent read-only review found no blocking findings.

These are regression/argv contract checks. They do not establish that the
running MCP image already uses this change. Credential mounts remain protected;
no secret-path write or global daemon configuration change was performed.

## CI dependency blocker and bounded repair — 2026-10-19

Exact-head PR run 18153 for 32966f19484b483f8cda6f519e1e3b28a0375050:
all 12 unit shards, both coverage jobs, integration/smoke, PostgreSQL and
Selenium passed. Checks 3.11/3.12 failed at pip-audit (jobs 88131/88132):
aiohttp 3.14.3, GHSA-2g87-pp6c-29x5, CVE-2026-111063 and CVE-2026-111064;
the audit lists 3.14.4 as fixed. This does not demonstrate exploitation.

Repair scope: require aiohttp>=3.14.4 in pyproject.toml, resolve precisely
aiohttp==3.14.4 in uv.lock using the existing package registry, and reject
unrelated package drift. The published PyPI 3.14.4 release was checked.
No suppression, ignored advisory or security-gate change is permitted.
Rules: GOV-01/04, dependency/security risk under section 1.4 and CI-02.

Acceptance: locked install and uv lock --check; strict OSV pip-audit exits 0;
existing HTTP-client, event-hook, notifier and Docker tests pass, plus Ruff
and mypy. Required PR CI on the new exact head must pass before merge.
The image and real cleaning Docker qualification remain separate gates.
If resolution or compatibility fails, keep the candidate isolated and
report the failure; production packages and running processes are untouched.

Validation in this candidate after locked installation: 153 selected HTTP-client/
notifier/event-hook/Docker tests passed (receipt 7a36dfac-dd6f-443f-ab04-aa074aba05d1);
strict OSV pip-audit exited 0, "No known vulnerabilities found"
(receipt a9f038c2-1438-45e2-8931-64a22e7adc48); mypy passed on 221 source files
(receipt c13bf332-ecc2-4375-8adf-4284e14bebd6); uv lock --check passed.
The only package-version changes are aiohttp 3.14.3 -> 3.14.4 and its required
yarl 1.24.5 -> 1.25.1. Explicitly retaining yarl 1.24.5 was rejected by the
resolver because aiohttp 3.14.4 requires yarl>=1.25.1,<2.0. No unrelated upgrade.
