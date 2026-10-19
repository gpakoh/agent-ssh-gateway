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
