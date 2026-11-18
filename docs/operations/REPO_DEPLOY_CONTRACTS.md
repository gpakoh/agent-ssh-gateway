# Repository-owned deploy contracts: execution and recovery

This document describes the existing `docker_deploy_contract` MCP execution
boundary. It is **not** permission to execute arbitrary repository scripts.

## Authority before execution

The Gateway binds each enabled contract to a reviewed, server-owned allowlist.
The `gpt-browser-bridge` entry is tied to its registered source project,
immutable candidate HEAD and base SHA, clean Git tree, exact script blob SHA
and script-file SHA-256, trusted digest-pinned image repository, operator
infra-root, approved state path, Compose identity and profile volume. It also
verifies the helper image identity and volume metadata before returning a
high-risk `mcp:docker:admin` confirmation action. The confirmation rechecks
the input contract before actual execution.

Other specialized deploy adapters (JS Chat Engine, Telegram Storage,
Site Audit, XLOUD and Content Harvester) have their own release authorities
and must not be made interchangeable by a caller-supplied script path or a
dynamic registry entry.

## Output and completion evidence

Deploy-script stdout is untrusted: even a reviewed script can print credentials
or a database connection string. The helper emits a static redaction marker
rather than raw stdout. Its permitted state receipt fields must be present in
their exact bounded formats before serialization. The MCP adapter accepts only
the version-1 helper receipt and a matching integer process exit code.

For the reviewed `gpt-browser-bridge` contract, a successful receipt must
agree with Docker's inspected immutable image reference and image ID,
container ID, generation label, healthy runtime state, and Compose
`project=infra-quart` / `service=gpt-browser-bridge`. Two inspections must
remain stable over the configured window. A claimed successful script exit
**alone** is not a qualified deployment.

## Non-atomic failures: do not automatically retry

The deploy script can mutate the Docker daemon before a helper transport
timeout, process death, unreadable/malformed receipt, mismatched receipt
protocol or explicit script timeout. Such outcomes use
`DEPLOY_CONTRACT_OUTCOME_AMBIGUOUS` with `retryable=false`.
A failed script can also have caused partial changes; a typed
`DEPLOY_CONTRACT_FAILED` is **not** rollback evidence.

For either case, the next action is *read-only reconciliation* against the
exact target container, Compose labels, image digest, state file, generation
and recovery/LKG contract. Do not dispatch a second deploy until an
authorized operator has established a new consistent release/recovery plan.
Do not assume a successful `docker_inspect` on its own verifies the state
file or the rollback path.

This scope hardens the already registered deploy runner; it does not close
P1-04 / Issue #878's broader goal of reviewed, generalized deploy-contract
registration and independent recovery qualification. Secret propagation and
one-shot Compose execution remain separate P1 tasks (#880 and #879).
