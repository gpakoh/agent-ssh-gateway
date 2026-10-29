# Gitea-only project registry: deploy preflight and guarded reconciliation

## Why a green deploy is not enough

The CI checkout supplies the reviewed `projects.yaml`, but the production Compose
services bind `${PROJECTS_YAML_PATH}` from the **host** over
`/app/projects.yaml`. Replacing application images does not update this
operator-owned file. The services can therefore report the correct build SHA
and pass health checks while loading an older project catalog.

For Issue #703, the reviewed source mapping is `nod-shared ->
gpakoh/nod-shared` with `type: coordination-only` and **no local root**.
The old host registry reserved that ID as a missing nested
`NOD_gateway/nod-shared` workspace. Do not substitute the vendored
`NOD_gateway/gateway_client/nod_shared`.

## Read-only predeploy check

`scripts/deploy-from-registry.sh` runs
`scripts/check_gitea_only_registry_mount.py` from the exact digest-tagged
MCP image before service recreation. The fresh Git `projects.yaml` is sent
over stdin; the host `PROJECTS_YAML_PATH` is bound read-only. The helper runs
without network and prints **only project IDs**, never YAML contents.

The helper checks source-controlled Gitea-only project IDs, exact
`gitea_repo` mappings, and the absence of `root`/`parent`. It rejects
unexpected Gitea-only entries or malformed/ambiguous YAML. It intentionally
does not reject independent operator changes to unrelated project entries.

`REGISTRY_CONFIG_DRIFT` is a **hard predeploy refusal**, not permission to
rewrite the host file or bypass the guard. Running services stay unchanged.

## Operator-controlled reconciliation

1. Verify the **canonical** repository, source commit, host bind target, and
   required owner/repo permissions. Read `PROJECTS_YAML_PATH` only as a path,
   never expose other `docker/.env` values.
2. Inspect the currently bound YAML and compare the exact affected stanza
   with the reviewed Git version. Use a read-only
   `workspace_preview_edit` to record `before_hash`, `after_hash`, and
   the complete diff. Stop if any unrelated line would change.
3. Obtain explicit runtime-config/deploy approval. Apply the **single
   reviewed change** with a sanctioned, audit-journaled, expected-SHA
   integration. Stop on SHA mismatch, pending journal, denied permission,
   unknown outcome, or mount/inode constraint; reconcile actual state
   before retry. Never force-write, reset a diverged checkout, or bulk-copy
   `projects.yaml`.
4. Recreate/reload affected services through the approved deployment path:
   the MCP registry is cached in-process, and a bind mount may retain the
   previous inode after an atomic host file replacement. A file hash change
   alone is **not** runtime acceptance.
5. Confirm the running image provenance and postmerge CI, then use read-only
   `project_list`, `info`, `coord_board` and `coord_inbox` for
   `nod-shared`. Confirm there is no workspace root. Do not create an
   artificial Issue in the target repository as a smoke test.

Close #703 only after the full runtime contract has passed.
