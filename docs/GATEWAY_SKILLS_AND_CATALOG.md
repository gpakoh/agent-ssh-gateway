# Discover tools and load skills

Start with `gateway_catalog()` instead of requesting every description/schema.
It returns small group summaries and skill metadata, without skill bodies or
the flat tool list. The catalog is built from the actual live tool manager.

Examples:

```text
gateway_catalog(group="repositories")
gateway_catalog(group="sites")
gateway_catalog(query="video")
gateway_catalog(tool_name="git_commit", include_schema=true)
gateway_skill(name="site-design", expected_pack_sha256="<advertised pack hash>")
gateway_skill(name="site-design", resource="references/review.md")
```

Group IDs are returned with titles, explanations and ready-to-use next calls.
Lists have explicit counts/pagination. Full schemas require one exact name.
Dependencies marked unavailable remain clearly distinct from registered tools.
The catalog is read-only metadata, not a dispatcher or permission grant.
Existing scopes, modes and mutation guards still apply to every actual call.
Role selection describes instructions; it does not select an authenticated role.

## One release-owned source

`skills/` is the canonical server pack, shipped with the MCP image. The initial
four definitions were imported from the personal ChatGPT skills created on
2026-10-28 and adapted to Gateway roles. Personal ChatGPT installations are
separate snapshots, not the server's live source. Future server edits go through
this repository's reviewed delivery; no request fetches mutable remote Markdown.

`app/skill_pack.py` owns metadata and the exact resource allowlist. Its digest
binds metadata and every allowed instruction/reference. Loading can fence that
digest across a release change. Unknown paths/URLs, symlinks, oversized files and
invalid frontmatter fail closed. The MCP image checks the pack at build time.

## Controller and worker

The controller can discover all four skills through MCP. `gateway-finish-task`
is controller-only: the standalone worker cannot commit, push, merge, deploy or
close an Issue. The other three skills contain an explicit worker handoff rule.

For each newly generated worker launch, the server transports only worker
resources into that task's persistent state, outside the source/workspace and
outside shared HOME. A compressed literal keeps the launch bounded. A receipt
`skills-receipt.json` records the release digest, names and file hashes.

The installer extends the current inline OpenCode `skills` paths, preserving
other settings, existing paths/URLs and permissions. It does not enable a denied
skill tool or replace provider configuration. OpenCode discovers metadata and
loads selected instructions through its native skill mechanism. Installation
evidence (`files_available`) is distinct from a verified native load; the
receipt never pretends the model consumed an instruction.

Existing task IDs/contracts are not replayed for a new skill pack. A changed
generated command participates in the existing execution fingerprint contract.
Previously launched jobs keep their original script. Source repositories, global
OpenCode configuration and authoritative working copies are not rewritten.

## Client boundary and acceptance

MCP clients may still receive/render a flat `tools/list`; this feature does not
claim to control ChatGPT's external schema injection or dynamically register
tools after a category click. No generic call-tool escape hatch is introduced.
After a release, clients must discover the two new tools through their normal
catalog refresh. `tools_manifest.agent_guidance` points to the new entrypoint.

Acceptance includes compact discovery, exact skill/reference loading, real MCP
schema/envelope tests, role separation, preserved permissions/configuration,
separate task paths, tamper checks, packaging, and actual native OpenCode
discovery before claiming deployed worker usability. A live model's task
success is a separate property from native skill discovery.

## Reproduce native discovery qualification

Run in an environment with the project Python dependencies and the approved
OpenCode executable:

```sh
python scripts/qualify_gateway_skills.py --opencode-bin /path/to/opencode
```

This uses a temporary task/workspace and separate XDG directories, without
inheriting provider credentials. It calls only version and native debug skill
discovery, verifies exact worker source locations/content, emits hashes and
cleans temporary data. It does not call a model or change production. The
actual executor/release integration still needs its own deployment acceptance.
