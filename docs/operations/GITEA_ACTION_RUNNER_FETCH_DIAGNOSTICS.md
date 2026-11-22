# Gitea Actions: runner action-source fetch diagnostics

Applies to read-only `gitea_get_action_job_logs` (Gateway #877).

## Recognized diagnostic

The optional `runner_failure_diagnostic` field is included only when a
**complete**, bounded, redacted log explicitly shows all of:

- an initial Gitea runner task header;
- a timestamped `git clone` for `actions/upload-artifact@v3` or
  `actions/download-artifact@v3`;
- a matching timestamped Git `info/refs?service=git-upload-pack`
  connection reset or network timeout;
- the terminal runner log marker `Job failed`.

The result has `failure_class=external_action_source_fetch`, a static
`network_error_class`, action name, `cleanup_timeout_observed` and
`evidence_scope=bounded_redacted_log_signature`. It never returns a
network endpoint, raw stderr, credential or container identifier.

If `truncated` or `line_truncated` is true, the log is malformed, or
signatures are missing, the tool **omits** the diagnostic: silence is not
proof that the failure was caused by application tests or by the network.

## Safe operational response

This is **classification, not CI approval**: `rerun_authorized=false`
is always set. Read the official run, job, exact HEAD, run attempt and
terminal status independently. Only after proving one failed or cancelled
job in a terminal non-success run should an authorized operator consider
`gitea_rerun_action_job` with its exact SHA/head guards. Do not bypass
required CI checks, rerun an entire workflow unnecessarily, or automatically
prune/restart shared Docker workers after a cleanup-timeout signature.

Gateway's current Gitea runner pool uses artifact action v3 because
v4's GHES detection fails in this environment; blindly swapping v3 to v4
is **not** a network remediation. Repeated source fetch failures require
a separate runner-owned examination of GitHub egress or a reviewed internal
action-source mirror and its trusted provenance. The classifier does not
solve egress or certify runner cleanup.