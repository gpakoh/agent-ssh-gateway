# Lightweight documentation CI

The CI workflow records a result on every PR and main/master push. Its change
scope job uses Python's standard library and checks documentation whitespace;
it does not install the application or start browsers/builds.

Only changes entirely within the allowlist in `scripts/ci_change_scope.py`
skip the Python matrix and its dependent E2E, image build, deployment and host
smoke jobs. The allowlist includes the root README, TODO, changelog, security
and operator guides, plus documentation text and images under `docs/`.

Code, tests, dependency files, scripts, workflows, configuration, agent
instructions, executable documents and symlinks require full CI. Mixed changes
also require full CI. Adding a new allowlisted path is a code change and itself
runs full CI. JSON/YAML contracts and runnable examples under docs are not
allowlisted.

PR comparison uses the merge base of the event's exact base/head revisions.
Push comparison covers the whole before/after range, including multi-commit
pushes. Missing history, new branches and unknown events conservatively require
full CI. A checkout/event head mismatch fails the scope job.

There are no workflow-level path filters or unconditional docs-success
workflows. Existing deployment serialization and exact-SHA safeguards remain.
Current Gitea master has no required status contexts; if required checks are
configured later, include the scope/workflow result and verify docs-only merge
behavior on Gitea before enforcing per-job checks.
