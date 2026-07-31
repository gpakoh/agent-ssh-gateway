from __future__ import annotations

from app.command_policy import DestructivePattern, PatternSuggestion, Severity, SuggestionKind
from app.packs import Pack

SECRETS_PATTERNS: tuple[DestructivePattern, ...] = (
DestructivePattern(
        name="vault-secrets-disable",
        regex=r"vault(?:\s+--?\S+(?:\s+\S+)?)*\s+secrets\s+disable\b",
        reason="vault secrets disable deactivates a secrets engine",
        severity=Severity.CRITICAL,
        description="vault secrets disable shuts down a secrets engine. All secrets stored "
        "in it are lost immediately; applications lose access to credentials.",
        suggestions=(
            PatternSuggestion(command="vault secrets list", description="Review enabled secrets engines first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="vault kv list {path}", description="Check what secrets exist in the engine", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="vault-kv-destroy",
        regex=r"vault(?:\s+--?\S+(?:\s+\S+)?)*\s+kv\s+destroy\b",
        reason="vault kv destroy permanently destroys secret versions",
        severity=Severity.CRITICAL,
        description="vault kv destroy permanently deletes secret versions — they cannot be "
        "recovered, even with the delete version path. No undelete possible.",
        suggestions=(
            PatternSuggestion(command="vault kv get -version={version} {path}", description="Read the version before destroying it", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="vault kv delete {path}", description="Use soft delete — versions stay recoverable via undelete", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="vault-kv-metadata-delete",
        regex=r"vault(?:\s+--?\S+(?:\s+\S+)?)*\s+kv\s+metadata\s+delete\b",
        reason="vault kv metadata delete removes all versions and metadata",
        severity=Severity.CRITICAL,
        description="vault kv metadata delete permanently removes the secret AND all its "
        "versions plus metadata. The path is gone entirely.",
        suggestions=(
            PatternSuggestion(command="vault kv metadata get {path}", description="Review version history and metadata first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="vault kv get -format=json {path} | jq .", description="Export the secret value before deletion", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="vault-kv-delete",
        regex=r"vault(?:\s+--?\S+(?:\s+\S+)?)*\s+kv\s+delete\b",
        reason="vault kv delete removes the latest secret version",
        severity=Severity.HIGH,
        description="vault kv delete removes the latest version of a secret. Older "
        "versions may remain, but applications reading the secret lose access.",
        suggestions=(
            PatternSuggestion(command="vault kv get {path}", description="Check current value before deleting", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="vault kv put {path} {key}={value}", description="Rotate the secret with a new value instead", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="vault-delete",
        regex=r"vault(?:\s+--?\S+(?:\s+\S+)?)*\s+delete\b",
        reason="vault delete removes a secret from the active engine",
        severity=Severity.HIGH,
        description="vault delete removes a secret via the generic delete endpoint. "
        "Behavior depends on the engine; for KV v1 the value is gone for good.",
        suggestions=(
            PatternSuggestion(command="vault read {path}", description="Read the secret before deleting", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="vault write {path} {key}={value}", description="Write a new value instead of deleting", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="vault-policy-delete",
        regex=r"vault(?:\s+--?\S+(?:\s+\S+)?)*\s+policy\s+delete\b",
        reason="vault policy delete removes an access policy",
        severity=Severity.HIGH,
        description="vault policy delete removes an access control policy. Tokens and "
        "roles relying on it lose permissions — can cause lockout.",
        suggestions=(
            PatternSuggestion(command="vault policy read {name}", description="Read the policy before deleting", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="vault policy write {name} {file}", description="Write a revised policy instead of deleting", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="vault-auth-disable",
        regex=r"vault(?:\s+--?\S+(?:\s+\S+)?)*\s+auth\s+disable\b",
        reason="vault auth disable deactivates an auth method",
        severity=Severity.CRITICAL,
        description="vault auth disable turns off an authentication method. Users and "
        "machines authenticating through it can no longer log in.",
        suggestions=(
            PatternSuggestion(command="vault auth list", description="Review enabled auth methods first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="vault auth tune -listing-visibility=unauth {path}", description="Adjust visibility instead of disabling the method", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="vault-token-revoke",
        regex=r"vault(?:\s+--?\S+(?:\s+\S+)?)*\s+token\s+revoke\b",
        reason="vault token revoke invalidates access tokens",
        severity=Severity.HIGH,
        description="vault token revoke invalidates tokens immediately. Revoking the "
        "wrong token can break applications mid-operation.",
        suggestions=(
            PatternSuggestion(command="vault token lookup {token}", description="Look up the token before revoking", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="vault token revoke -accessor {accessor}", description="Revoke by accessor for precise targeting", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="vault-lease-revoke",
        regex=r"vault(?:\s+--?\S+(?:\s+\S+)?)*\s+lease\s+revoke\b",
        reason="vault lease revoke invalidates dynamic credentials",
        severity=Severity.HIGH,
        description="vault lease revoke invalidates dynamic secrets (DB creds, AWS creds, "
        "certificates). Dependent services lose credentials immediately.",
        suggestions=(
            PatternSuggestion(command="vault lease lookup {lease_id}", description="Look up the lease before revoking", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="vault lease revoke-prefix {path}", description="Revoke by prefix to target a specific app's leases", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="aws-secretsmanager-delete-resource-policy",
        regex=r"aws(?:\s+--?\S+(?:\s+\S+)?)*\s+secretsmanager\s+delete-resource-policy\b",
        reason="aws secretsmanager delete-resource-policy removes access policy",
        severity=Severity.HIGH,
        description="Removes the resource policy from a secret. Applications and "
        "cross-account consumers lose access to the secret.",
        suggestions=(
            PatternSuggestion(command="aws secretsmanager describe-secret --secret-id {id}", description="Check who uses this secret first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="aws secretsmanager get-resource-policy --secret-id {id}", description="Save the policy before removing it", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="aws-secretsmanager-remove-regions",
        regex=r"aws(?:\s+--?\S+(?:\s+\S+)?)*\s+secretsmanager\s+remove-regions-from-replication\b",
        reason="aws secretsmanager remove-regions-from-replication stops replication",
        severity=Severity.MEDIUM,
        description="Stops secret replication to specified regions. If the primary region "
        "fails, replicated copies are no longer available.",
        suggestions=(
            PatternSuggestion(command="aws secretsmanager describe-secret --secret-id {id}", description="Check replication status first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="aws secretsmanager replicate-secret-to-regions", description="Re-add replication if needed", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="aws-ssm-delete-parameter",
        regex=r"aws(?:\s+--?\S+(?:\s+\S+)?)*\s+ssm\s+delete-parameter\b",
        reason="aws ssm delete-parameter removes a parameter from Parameter Store",
        severity=Severity.HIGH,
        description="Deletes a parameter from SSM Parameter Store. Applications reading "
        "this parameter lose their configuration or secret.",
        suggestions=(
            PatternSuggestion(command="aws ssm get-parameter --name {name}", description="Read the parameter before deleting", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="aws ssm describe-parameters --parameter-filters", description="Check which services reference it", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="aws-ssm-delete-parameters",
        regex=r"aws(?:\s+--?\S+(?:\s+\S+)?)*\s+ssm\s+delete-parameters\b",
        reason="aws ssm delete-parameters removes multiple parameters",
        severity=Severity.HIGH,
        description="Batch-deletes parameters from Parameter Store. Multiple applications "
        "can break at once if any parameter is in use.",
        suggestions=(
            PatternSuggestion(command="aws ssm get-parameters --names {names}", description="Read all parameters before deleting", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="aws ssm get-parameters-by-path --path {path} --recursive", description="Inventory all parameters in the path first", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="doppler-secrets-delete",
        regex=r"doppler(?:\s+--?\S+(?:\s+\S+)?)*\s+secrets\s+delete\b",
        reason="doppler secrets delete removes secrets from a config",
        severity=Severity.HIGH,
        description="Deletes secrets from a Doppler config. Deployed environments lose "
        "those values on next sync.",
        suggestions=(
            PatternSuggestion(command="doppler secrets get {name}", description="Read the secret before deleting", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="doppler secrets set {name}={value}", description="Rotate the secret instead of deleting", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="doppler-projects-delete",
        regex=r"doppler(?:\s+--?\S+(?:\s+\S+)?)*\s+projects\s+delete\b",
        reason="doppler projects delete removes an entire project",
        severity=Severity.CRITICAL,
        description="Deletes a whole Doppler project with all configs and secrets. "
        "Environment deployments break everywhere.",
        suggestions=(
            PatternSuggestion(command="doppler projects list", description="Review projects before deleting", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="doppler secrets download {project}/{config} > backup.json", description="Back up all secrets first", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="doppler-environments-delete",
        regex=r"doppler(?:\s+--?\S+(?:\s+\S+)?)*\s+environments\s+delete\b",
        reason="doppler environments delete removes an environment",
        severity=Severity.HIGH,
        description="Deletes a Doppler environment. Deployments targeting that "
        "environment stop resolving secrets.",
        suggestions=(
            PatternSuggestion(command="doppler environments list", description="Review environments first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="doppler secrets download {project}/{env} > backup.json", description="Back up before deleting", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="doppler-configs-delete",
        regex=r"doppler(?:\s+--?\S+(?:\s+\S+)?)*\s+configs\s+delete\b",
        reason="doppler configs delete removes a config",
        severity=Severity.HIGH,
        description="Deletes a Doppler config (e.g. dev/staging/prod). Environments "
        "referencing it lose their secret source.",
        suggestions=(
            PatternSuggestion(command="doppler configs list", description="Review configs first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="doppler secrets download {project}/{config} > backup.json", description="Back up secrets before deleting", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="op-item-delete",
        regex=r"op(?:\s+--?\S+(?:\s+\S+)?)*\s+item\s+delete\b",
        reason="op item delete permanently deletes a 1Password item",
        severity=Severity.HIGH,
        description="Permanently deletes a 1Password item (login, password, etc.). "
        "Deletion bypasses the trash if --permanent is used.",
        suggestions=(
            PatternSuggestion(command="op item get {item}", description="Read the item before deleting", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="op item create --category={category} ...", description="Recreate the item after verifying", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="op-document-delete",
        regex=r"op(?:\s+--?\S+(?:\s+\S+)?)*\s+document\s+delete\b",
        reason="op document delete removes a 1Password document",
        severity=Severity.HIGH,
        description="Permanently deletes a 1Password document item.",
        suggestions=(
            PatternSuggestion(command="op document get {document}", description="Download the document before deleting", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="op item create --category=DOCUMENT ...", description="Recreate after verifying contents", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="op-vault-delete",
        regex=r"op(?:\s+--?\S+(?:\s+\S+)?)*\s+vault\s+delete\b",
        reason="op vault delete removes an entire 1Password vault",
        severity=Severity.CRITICAL,
        description="Deletes an entire 1Password vault with all items. Export first — "
        "this is irreversible.",
        suggestions=(
            PatternSuggestion(command="op vault get {vault}", description="Review the vault before deleting", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="op vault export {vault} > vault-backup.1pux", description="Export the vault before deletion", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="op-user-delete",
        regex=r"op(?:\s+--?\S+(?:\s+\S+)?)*\s+user\s+delete\b",
        reason="op user delete removes a 1Password user",
        severity=Severity.HIGH,
        description="Removes a user from the 1Password account. They lose access to all "
        "shared vaults immediately.",
        suggestions=(
            PatternSuggestion(command="op user get {user}", description="Review user details first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="op user suspend {user}", description="Suspend instead — reversible", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="op-group-delete",
        regex=r"op(?:\s+--?\S+(?:\s+\S+)?)*\s+group\s+delete\b",
        reason="op group delete removes a 1Password group",
        severity=Severity.HIGH,
        description="Removes a 1Password group. Members lose group-level vault access.",
        suggestions=(
            PatternSuggestion(command="op group get {group}", description="Review group members first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="op user list --group={group}", description="Check which users are affected", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="op-connect-token-delete",
        regex=r"op(?:\s+--?\S+(?:\s+\S+)?)*\s+connect\s+token\s+delete\b",
        reason="op connect token delete revokes a Connect service token",
        severity=Severity.HIGH,
        description="Deletes a 1Password Connect token. Applications using that token "
        "lose access to secrets immediately.",
        suggestions=(
            PatternSuggestion(command="op connect token get {token}", description="Review token details first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="op connect token create ...", description="Create a replacement token after verifying", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
)


def build_secrets_pack() -> Pack:
    return Pack(
        id="secrets",
        name="Secrets",
        destructive_patterns=SECRETS_PATTERNS,
        keywords=("vault", "secretsmanager", "ssm", "doppler", "1password", "op "),
    )
