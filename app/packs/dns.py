from __future__ import annotations

from app.command_policy import DestructivePattern, PatternSuggestion, Severity, SuggestionKind
from app.packs import Pack

DNS_PATTERNS: tuple[DestructivePattern, ...] = (
DestructivePattern(
        name="dns-nsupdate-delete",
        regex=r"(?:\bnsupdate\b.*\bdelete\b|\bdelete\b.*\|\s*\bnsupdate\b)",
        reason="nsupdate delete commands remove DNS records",
        severity=Severity.HIGH,
        description="nsupdate delete removes DNS records from the authoritative server via "
        "dynamic DNS updates (RFC 2136). Changes take effect immediately and can break "
        "services relying on those records.",
        suggestions=(
            PatternSuggestion(command="nsupdate -v {file} && nsupdate -v {file}", description="Use -v and verify record state with 'prereq' first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="dig {record} @{server}", description="Verify current record state before deleting", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="dns-nsupdate-local",
        regex=r"\bnsupdate\b.*\s-l\b",
        reason="nsupdate -l applies local updates which can modify DNS records",
        severity=Severity.MEDIUM,
        description="nsupdate -l uses local (loopback) TSIG authentication, allowing DNS "
        "modifications without network credentials. Can accidentally modify production DNS "
        "if run on the wrong server.",
        suggestions=(
            PatternSuggestion(command="nsupdate -k {keyfile} -v {server}", description="Use explicit server and key options for clarity", kind=SuggestionKind.SAFER_ALTERNATIVE),
            PatternSuggestion(command="echo 'show' | nsupdate -l", description="Test changes with 'show' before 'send'", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="dns-dig-zone-transfer",
        regex=r"(?i:\bdig\b.*\b(?:axfr|ixfr)\b)",
        reason="dig AXFR/IXFR zone transfers can exfiltrate full zone data",
        severity=Severity.MEDIUM,
        description="Zone transfers (AXFR/IXFR) download complete DNS zone data, revealing "
        "all hostnames, internal IPs, and infrastructure topology. Aids reconnaissance.",
        suggestions=(
            PatternSuggestion(command="dig {domain} ANY", description="Query specific records instead of full transfer", kind=SuggestionKind.SAFER_ALTERNATIVE),
            PatternSuggestion(command="dig {domain} +short A", description="Use standard queries for specific record types", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="cloudflare-wrangler-dns-delete",
        regex=r"wrangler(?:\s+--?\S+(?:\s+\S+)?)*\s+dns-records\s+delete\b",
        reason="wrangler dns-records delete removes a Cloudflare DNS record",
        severity=Severity.HIGH,
        description="Deleting a DNS record can immediately break connectivity to your "
        "website, API, or mail server. Propagation is fast on Cloudflare.",
        suggestions=(
            PatternSuggestion(command="wrangler dns-records list --zone-id {zone}", description="Review records first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="wrangler dns-records add --zone-id {zone} --name {name} --type {type} --content {content}", description="Re-add the record after verification", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="cloudflare-api-delete-dns-record",
        regex=r"(?i)\bcurl\b(?=.*(?:-X\s*|--request(?:=|\s+))DELETE\b)(?=.*\bapi\.cloudflare\.com\b[^\s]*?/dns_records/[^\s]+).*",
        reason="curl -X DELETE against /dns_records/{id} deletes a Cloudflare DNS record",
        severity=Severity.HIGH,
        description="API deletion of a DNS record takes effect immediately across "
        "Cloudflare's network. No confirmation prompt when using curl directly.",
        suggestions=(
            PatternSuggestion(command="curl -X GET https://api.cloudflare.com/client/v4/zones/{zone}/dns_records", description="Verify the record ID first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="curl -X PUT https://api.cloudflare.com/client/v4/zones/{zone}/dns_records/{id}", description="Update the record instead of deleting", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="cloudflare-api-delete-zone",
        regex=r"(?i)\bcurl\b(?=.*(?:-X\s*|--request(?:=|\s+))DELETE\b)(?=.*\bapi\.cloudflare\.com\b[^\s]*?/zones/[^\s]+).*",
        reason="curl -X DELETE against /zones/{id} deletes a Cloudflare zone",
        severity=Severity.CRITICAL,
        description="Deleting a zone removes ALL DNS records, page rules, firewall rules, "
        "and settings for that domain. Complete outage with no easy recovery.",
        suggestions=(
            PatternSuggestion(command="curl -X GET https://api.cloudflare.com/client/v4/zones", description="List zones and export config first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="curl -X DELETE https://api.cloudflare.com/client/v4/zones/{zone}/dns_records/{id}", description="Delete individual records instead of the whole zone", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="cloudflare-terraform-destroy-record",
        regex=r"terraform\b.*?\s+destroy\s+.*-target=(?:resource\.)?cloudflare_record\.",
        reason="terraform destroy -target=cloudflare_record deletes specific DNS records",
        severity=Severity.HIGH,
        description="Terraform destroy removes DNS records from Cloudflare. The deletion "
        "is immediate and can break services.",
        suggestions=(
            PatternSuggestion(command="terraform plan -destroy -target=cloudflare_record.{name}", description="Preview the deletion first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="terraform state rm cloudflare_record.{name}", description="Stop managing without deleting", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="route53-change-record-sets-delete",
        regex=r"\baws\b.*?\broute53\s+change-resource-record-sets\b.*\bDELETE\b",
        reason="aws route53 change-resource-record-sets with DELETE removes DNS records",
        severity=Severity.HIGH,
        description="DELETE actions in change-resource-record-sets immediately remove DNS "
        "records. Resolvers fail to reach services once caches expire.",
        suggestions=(
            PatternSuggestion(command="aws route53 list-resource-record-sets --hosted-zone-id {zone}", description="Verify record state first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="aws route53 change-resource-record-sets --change-batch '{\"Changes\":[{\"Action\":\"UPSERT\",...}]}'", description="Use UPSERT to modify instead of delete", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="route53-delete-health-check",
        regex=r"\baws\b.*?\broute53\s+delete-health-check\b",
        reason="aws route53 delete-health-check permanently deletes a Route53 health check",
        severity=Severity.HIGH,
        description="Deleting a health check can disrupt DNS failover. Route53 may route "
        "traffic to unhealthy endpoints or stop failover entirely.",
        suggestions=(
            PatternSuggestion(command="aws route53 get-health-check --health-check-id {id}", description="Review health check configuration first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="aws route53 list-resource-record-sets --hosted-zone-id {zone} --query 'ResourceRecordSets[?HealthCheckId==`{id}`]'", description="Check which records use this health check", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="route53-delete-query-logging-config",
        regex=r"\baws\b.*?\broute53\s+delete-query-logging-config\b",
        reason="aws route53 delete-query-logging-config removes query logging",
        severity=Severity.MEDIUM,
        description="Deleting query logging stops DNS query visibility for that hosted "
        "zone. Impacts debugging, security monitoring, and compliance auditing.",
        suggestions=(
            PatternSuggestion(command="aws route53 get-query-logging-config --id {id}", description="Review logging config before deletion", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="aws cloudwatch get-log-group --log-group-name {group}", description="Ensure CloudWatch log retention is adequate", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="route53-delete-traffic-policy",
        regex=r"\baws\b.*?\broute53\s+delete-traffic-policy\b",
        reason="aws route53 delete-traffic-policy permanently deletes a traffic policy",
        severity=Severity.HIGH,
        description="Deleting a traffic policy removes routing logic. Policy instances "
        "will fail to update or may stop working.",
        suggestions=(
            PatternSuggestion(command="aws route53 list-traffic-policy-instances", description="Check policy usage first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="aws route53 create-traffic-policy --name {name} --document {doc}", description="Create a new policy version instead of deleting", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
)


def build_dns_pack() -> Pack:
    return Pack(
        id="dns",
        name="DNS",
        destructive_patterns=DNS_PATTERNS,
        keywords=("nsupdate", "dig", "wrangler", "cloudflare", "route53", "terraform"),
    )
