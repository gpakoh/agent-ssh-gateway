# Managed SSH targets: LXC103 and LXC104 (opt-in)

## Scope and safety

The Gateway already has a managed executor SSH session (the container
`agent-sshd`). **That session is not equivalent to an interactive shell
on the LXC103 host**. This feature adds two explicit operator-owned target
aliases, `lxc103` and `lxc104`, to obtain distinct connections to the
*actual hosts*. Neither alias is implicitly configured.

`list_ssh_targets` lists configured aliases and addresses, never paths or
private keys. `execute_target_argv(target, argv, stdin, timeout_s)` uses an
exact registered alias; callers cannot supply host/port/username/key path.
A separate ephemeral Gateway SSH session is created, the command is executed
through the **existing** `/api/ssh/execute-argv` command-policy, audit and
session-ownership checks, and the new session is disconnected in a `finally`
block. The ephemeral Gateway session has a 60-second idle ceiling and may
not be pooled/reused across targets. There is no `ssh -o StrictHostKeyChecking=no`,
no shelling out to `ssh`, no direct `pct exec`, and no blind `ssh-keyscan`
trust on the user's behalf.

The target execution MCP tool is available only in explicit admin-capable
tool modes and requires **both** `mcp:execute` and `mcp:admin`. It is absent
from the ordinary `mcp_client` mode and its safe subset. Target aliases
cannot escape the Gateway server's `ALLOWED_TARGET_CIDRS` /
`DENIED_TARGET_CIDRS` or its strict SSH host-key verification. Do not
expand target CIDRs to `0.0.0.0/0` just to reach LXC104.

## Operator provisioning (no production changes by this PR)

1. Confirm each host's **actual** private IP and SSH port using the
   Proxmox/LXC configuration or your existing admin connection. The two
   `192.0.2.x` addresses in
   [ssh-targets.example.json](ssh-targets.example.json) are **documentation
   placeholders**, not discovered addresses.
2. Create or choose one dedicated automation SSH identity for each host.
   Prefer unprivileged `gatewayops` and `mailops` with narrowly scoped
   sudoers commands to review/reload Nginx/PHP; do not permit unrestricted
   root commands. Install its *public* key in the target's
   `~/.ssh/authorized_keys`; do not send private keys to chat, Git or logs.
3. Keep the authoritative SSH private keys in a protected operator location
   (in the host administrator's protected SSH key directory). The **mcp-oauth** container runs as
   **UID/GID 1000**, so an untouched root-owned 0600 key mounted read-only
   will NOT be readable by it. Provision a separate narrowly mounted
   0600 copy owned by uid 1000 in an operator-only directory, or an
   equivalent narrowly scoped root-managed credential mount. Never mount
   the host administrator's entire home or SSH key directory into the MCP container.
   The feature refuses group/world-readable key files and symlinks.
4. Verify the host **public-key fingerprint out of band** (from the Proxmox
   console or SSH host-key files). Register the corresponding host key in
   the existing Gateway server's `KNOWN_HOSTS_STORE=file` trust store.
   Unknown and changed host keys must be rejected. This is separate from
   authenticating with the *client* private key.
5. Create the private JSON config, using the example above as the schema.
   For each entry use the real `host` literal IPv4/IPv6, `port`,
   `username`, and the **basename** `key_file`. Only names from the
   config work. Configuration must be an ordinary non-writable-by-others
   file (e.g. root-owned 0644 for this non-secret mapping, or uid 1000-owned
   0600), readable by the MCP container but not writable by other users.
   Keys must be regular uid 1000-readable 0600 files; no symlinks,
   arbitrary user-supplied paths or inline keys.
6. Review the opt-in
   [Compose override](../../docker/compose.ssh-targets.example.yml).
   It supplies a **read-only** per-file key mount, not a directory mount.
   The required `MCP_SSH_TARGETS_CONFIG_SOURCE`,
   `MCP_SSH_LXC103_KEY_SOURCE`, and `MCP_SSH_LXC104_KEY_SOURCE`
   environment values name files on the LXC103 *Docker host*. Never set
   them from a request or print their contents. The main Compose file
   remains unchanged until an operator deliberately includes the override.
   To **persist** this across CI deployments, put the following entries in
   the operator-owned `docker/.env` on the deployment host (never in Git):

   ```dotenv
   SSH_TARGETS_OVERLAY_ENABLED=true
   MCP_SSH_TARGETS_CONFIG_SOURCE=/srv/gateway-ssh-targets/targets.json
   MCP_SSH_LXC103_KEY_SOURCE=/srv/gateway-ssh-targets/keys/lxc103
   MCP_SSH_LXC104_KEY_SOURCE=/srv/gateway-ssh-targets/keys/lxc104
   ```

   The reviewed `scripts/deploy-from-registry.sh` applies this fixed
   read-only overlay during normal rollout **and rollback**. The CI deploy
   job is itself a container: it must **not** mount operator SSH keys into
   its job workspace, nor pretend `/srv/gateway-ssh-targets` exists there.
   When enabled, a disposable verifier runs on the host Docker daemon
   from the already-installed immutable MCP image, with no network,
   no Linux capabilities, no new privileges and read-only access only to
   the operator target directory. It checks for missing/symlinked files,
   group/world-writable config, exact uid 1000/mode 0600 private key files
   and unreadable inputs without printing their bytes. The CI job only
   receives a typed GO/NO-GO; it does not read the private key contents.
   Compose itself must also validate successfully before any image pull.
   `SSH_TARGETS_OVERLAY_ENABLED=false` (the default) keeps the original
   Compose model and requires no operator key directory. A one-off Compose
   override would be lost on the next CI deployment. If the optional
   Media API overlay is enabled, both fixed overlays compose together.
7. Start only the affected MCP adapter via the existing guarded,
   immutable-image deployment path. Verify the exact build SHA, then
   `list_ssh_targets` and harmless `hostname`/`id -un`/health
   commands on **each distinct target**. Confirm audit events report
   two separate sessions and failed target commands do not poison the
   normal executor SID.

Examples after the opt-in file, verified host keys and target CIDRs are
installed:

```text
list_ssh_targets()
execute_target_argv(target="lxc103", argv=["hostname"])
execute_target_argv(target="lxc104", argv=["hostname"])
execute_target_argv(target="lxc104", argv=["systemctl", "is-active", "php8.2-fpm"])
```

The last command is illustrative; actual commands still require the current
Gateway **command policy** and the target account's OS permissions. No
password is required from the ChatGPT client. No session credential or
host-key trust can be supplied in a chat prompt.

## Acceptance and rollback

- Invalid alias, invalid IP/port/user, insecure key file, missing trust,
  unreachable target or denied command **fails closed**, leaving current
  production SOGo/SMTP services untouched.
- Run read-only checks on LXC103 and LXC104 before *any* Mailcow deployment.
- Validate the application on staging using a dedicated Mailcow account,
  exact SOGo controller lifecycle, session-bound PHP authorization and
  private verified HTTPS link LXC104 → LXC103. See Mail Composer Issues
  #57/#58. SSH access **alone is not a production GO** for Mosaic.
- Rollback: remove the opt-in Compose override and redeploy only the pinned
  previous MCP image/config via the established gateway rollback contract;
  do not rotate or delete existing host SSH keys. The original
  `execute_argv(session_id, argv)` remains unchanged.

This PR provides code and a configuration *example*. It does **not**
provision SSH accounts, publish keys, modify firewall/Nginx/Proxmox, or
enable either target in production.
