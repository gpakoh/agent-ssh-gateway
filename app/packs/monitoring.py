from __future__ import annotations

from app.command_policy import DestructivePattern, PatternSuggestion, Severity, SuggestionKind
from app.packs import Pack

MONITORING_PATTERNS: tuple[DestructivePattern, ...] = (
DestructivePattern(
        name="promtool-tsdb-delete",
        regex=r"promtool\b.*?\btsdb\s+delete\b",
        reason="promtool tsdb delete removes time series from local TSDB blocks",
        severity=Severity.HIGH,
        description="promtool tsdb delete permanently removes time series matching the "
        "selector from the local TSDB. Data is gone even after compaction.",
        suggestions=(
            PatternSuggestion(command="promtool tsdb list | grep {series}", description="Confirm the series exists first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="curl 'http://localhost:9090/api/v1/series?match[]={selector}'", description="Query the series before deletion", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="promtool-tsdb-remove-limits",
        regex=r"promtool\b.*?\btsdb\s+remove-limits\b",
        reason="promtool tsdb remove-limits deletes retention limits",
        severity=Severity.MEDIUM,
        description="promtool tsdb remove-limits removes retention/compaction limits "
        "from blocks. Enables unbounded disk growth.",
        suggestions=(
            PatternSuggestion(command="promtool tsdb info {path}", description="Review block metadata first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="df -h {path}", description="Check disk usage before changing limits", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="prometheus-api-delete-series",
        regex=r"(?i)\bcurl\b(?=.*(?:-X\s*|--request(?:=|\s+))POST\b)(?=.*/api/v1/admin/tsdb/delete_series\b).*",
        reason="POST /api/v1/admin/tsdb/delete_series deletes series from Prometheus",
        severity=Severity.CRITICAL,
        description="The admin API delete_series endpoint permanently removes time "
        "series from Prometheus storage. Requires --web.enable-admin-api and is "
        "irreversible.",
        suggestions=(
            PatternSuggestion(command="curl 'http://localhost:9090/api/v1/series?match[]={selector}'", description="Query the series before deleting", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="curl -X POST 'http://localhost:9090/api/v1/admin/tsdb/clean_tombstones'", description="Clean tombstones only after verifying", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="prometheus-rules-file-delete",
        regex=r"\brm\b(?:\s+--?\S+(?:\s+\S+)?)*\s+(?:(?:-f|--force)\s+)?['\"]?(?:/etc/prometheus/(?:rules\.d|rules)/\S+|/etc/prometheus/(?:prometheus|rules)\.(?:ya?ml))['\"]?(?:\s|$)",
        reason="rm on /etc/prometheus rules files removes alerting rules",
        severity=Severity.HIGH,
        description="Deleting Prometheus rule files removes all alerting and recording "
        "rules. Alerts stop firing silently — no notification of the loss.",
        suggestions=(
            PatternSuggestion(command="ls -la /etc/prometheus/rules/", description="Review rules directory first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="cp {file} {file}.bak && promtool check rules {file}", description="Back up and validate rules before changes", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="grafana-cli-plugins-uninstall",
        regex=r"\bgrafana-cli\b(?:\s+--?\S+(?:\s+\S+)?)*\s+plugins\s+uninstall\b",
        reason="grafana-cli plugins uninstall removes a Grafana plugin",
        severity=Severity.MEDIUM,
        description="Removes a Grafana plugin. Dashboards or panels using it break and "
        "render as errors.",
        suggestions=(
            PatternSuggestion(command="grafana-cli plugins ls", description="List installed plugins first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="grafana-cli plugins install {plugin}", description="Reinstall the plugin after verifying", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="grafana-api-delete-dashboard",
        regex=r"(?i)\bcurl\b(?=.*(?:-X\s*|--request(?:=|\s+))DELETE\b)(?=.*/api/dashboards/).*",
        reason="curl DELETE /api/dashboards/{uid} removes a Grafana dashboard",
        severity=Severity.HIGH,
        description="Deletes a Grafana dashboard permanently. All panels and alert rules "
        "on it are lost.",
        suggestions=(
            PatternSuggestion(command="curl 'http://localhost:3000/api/search?type=dash-db'", description="List dashboards to confirm the uid", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="curl 'http://localhost:3000/api/dashboards/uid/{uid}' > dashboard.json", description="Export the dashboard before deleting", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="grafana-api-delete-datasource",
        regex=r"(?i)\bcurl\b(?=.*(?:-X\s*|--request(?:=|\s+))DELETE\b)(?=.*/api/datasources/).*",
        reason="curl DELETE /api/datasources/{id} removes a Grafana datasource",
        severity=Severity.HIGH,
        description="Deletes a Grafana datasource. Every dashboard using it stops "
        "querying data.",
        suggestions=(
            PatternSuggestion(command="curl 'http://localhost:3000/api/datasources'", description="List datasources and confirm the id", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="curl -X PUT 'http://localhost:3000/api/datasources/{id}' -d @datasource.json", description="Re-add the datasource config after verifying", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="grafana-cli-admin-reset-admin-password",
        regex=r"\bgrafana-cli\b.*\badmin\s+reset-admin-password\b",
        reason="grafana-cli admin reset-admin-password overwrites the admin password",
        severity=Severity.HIGH,
        description="Resets the Grafana admin password. Existing sessions and API tokens "
        "may stop working; audit trail is disrupted.",
        suggestions=(
            PatternSuggestion(command="grafana-cli admin list-users", description="Verify users before resetting", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="grafana-cli admin reset-admin-password {new} --force", description="Set a known strong password deliberately", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="influx-delete",
        regex=r"\binflux\b\s+delete\b(?!.*--dry-run(?:=true)?(?:\s|$))",
        reason="influx delete removes data from a bucket",
        severity=Severity.CRITICAL,
        description="influx delete permanently removes data matching the predicate from "
        "a bucket. Irreversible — no trash or recovery.",
        suggestions=(
            PatternSuggestion(command="influx query 'from(bucket:\"{bucket}\") |> range(start: {start})'", description="Preview the data before deleting", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="influx backup --bucket {bucket} {dir}", description="Back up the bucket before deletion", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="influx-bucket-delete",
        regex=r"\binflux\b.*?\bbucket\s+delete\b",
        reason="influx bucket delete removes an entire bucket",
        severity=Severity.CRITICAL,
        description="Deletes an entire InfluxDB bucket with ALL its data. Any writer "
        "still sending data recreates it empty.",
        suggestions=(
            PatternSuggestion(command="influx bucket list", description="List buckets and confirm the id", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="influx backup --bucket {bucket} {dir}", description="Back up the bucket before deleting", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="influx-org-delete",
        regex=r"\binflux\b.*?\borg\s+delete\b",
        reason="influx org delete removes an organization",
        severity=Severity.CRITICAL,
        description="Deletes an InfluxDB organization with all its buckets, tasks, and "
        "data. Permanent.",
        suggestions=(
            PatternSuggestion(command="influx org list", description="List organizations first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="influx backup --org {org} {dir}", description="Back up the org before deleting", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="influx-task-delete",
        regex=r"\binflux\b.*?\btask\s+delete\b",
        reason="influx task delete removes a scheduled task",
        severity=Severity.MEDIUM,
        description="Deletes an InfluxDB task. Downsampling, alerts, and data "
        "processing the task performed stop silently.",
        suggestions=(
            PatternSuggestion(command="influx task list", description="List tasks and confirm the id", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="influx task create --org {org} --flux {flux}", description="Recreate the task after verifying", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="influxdb-drop-database",
        regex=r"\binflux\b.*?(?:-database|--database)\b[^\s]*\s+\S+\s+.*\bDROP\s+DATABASE\b",
        reason="influx DROP DATABASE removes an InfluxDB 1.x database",
        severity=Severity.CRITICAL,
        description="influx DROP DATABASE permanently removes an InfluxDB 1.x database "
        "and all its series and points.",
        suggestions=(
            PatternSuggestion(command="influx -database {db} -execute 'SHOW MEASUREMENTS'", description="Review measurements before dropping", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="influxd backup -portable {dir}", description="Back up the database first", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="influxdb-drop-series",
        regex=r"\binflux\b.*?(?:-database|--database)\b[^\s]*\s+\S+\s+.*\bDROP\s+SERIES\b",
        reason="influx DROP SERIES removes series from a database",
        severity=Severity.HIGH,
        description="influx DROP SERIES permanently deletes the specified series and "
        "their measurements from an InfluxDB 1.x database.",
        suggestions=(
            PatternSuggestion(command="influx -database {db} -execute 'SHOW SERIES'", description="Confirm the series before dropping", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="influxd backup -portable {dir}", description="Back up the database first", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="whisper-delete",
        regex=r"\bwhisper-delete(?:\.py)?\b",
        reason="whisper-delete removes whisper data files",
        severity=Severity.HIGH,
        description="whisper-delete.py permanently deletes whisper data files "
        "(Graphite metrics). Historical metrics are lost.",
        suggestions=(
            PatternSuggestion(command="ls -la {path}", description="Verify the whisper file path first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="cp {file} {file}.bak", description="Back up the whisper file before deleting", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="whisper-clean",
        regex=r"\bwhisper-clean(?:\.py)?\b",
        reason="whisper-clean removes stale whisper files",
        severity=Severity.MEDIUM,
        description="whisper-clean.py removes whisper files that are stale (older than "
        "the threshold). Can delete metrics still needed for graphing.",
        suggestions=(
            PatternSuggestion(command="whisper-info.py {file}", description="Check the whisper file age first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="whisper-fetch.py {file} {start} {end}", description="Fetch data before cleaning", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="kubectl-delete-monitoring-resources",
        regex=r"\bkubectl\b(?:\s+--?\S+(?:\s+\S+)?)*\s+delete\s+(?:prometheusrules?|servicemonitors?|podmonitors?)(?:\.monitoring\.coreos\.com)?\b",
        reason="kubectl delete removes Prometheus operator monitoring resources",
        severity=Severity.HIGH,
        description="Deleting PrometheusRule/ServiceMonitor/PodMonitor resources stops "
        "alerting and metric collection for the targeted services.",
        suggestions=(
            PatternSuggestion(command="kubectl get {resource} -n {ns}", description="List the monitoring resources first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="kubectl get {resource} {name} -n {ns} -o yaml > backup.yaml", description="Export the resource before deleting", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="datadog-ci-monitors-delete",
        regex=r"datadog-ci\b.*?\bmonitors\s+delete\b",
        reason="datadog-ci monitors delete removes a Datadog monitor",
        severity=Severity.HIGH,
        description="Deleting a Datadog monitor stops all alerting for that check. You "
        "will no longer be notified if the monitored condition occurs, potentially "
        "missing critical production issues.",
        suggestions=(
            PatternSuggestion(command="datadog-ci monitors get {id}", description="Review the monitor configuration first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="datadog-ci monitor mute {id}", description="Mute the monitor temporarily instead of deleting", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="datadog-ci-dashboards-delete",
        regex=r"datadog-ci\b.*?\bdashboards\s+delete\b",
        reason="datadog-ci dashboards delete removes a Datadog dashboard",
        severity=Severity.HIGH,
        description="Deleting a dashboard removes all widgets, queries, and layout "
        "configuration. Team members relying on this dashboard for visibility lose "
        "access immediately.",
        suggestions=(
            PatternSuggestion(command="datadog-ci dashboards get {id}", description="Export dashboard JSON first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="curl -X GET https://api.datadoghq.com/api/v1/dashboard/{id}", description="Download the dashboard definition before deleting", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="datadog-api-delete",
        regex=r"(?i)\bcurl\b(?=.*(?:-X\s*|--request(?:=|\s+))DELETE\b)(?=.*api\.datadoghq\.com.*\/(?:monitor|dashboard|synthetics)\/).*",
        reason="curl DELETE to api.datadoghq.com removes monitors/dashboards/synthetics",
        severity=Severity.HIGH,
        description="Direct API DELETE calls permanently remove Datadog resources "
        "without confirmation prompts. Monitors, dashboards, and synthetic tests are "
        "deleted immediately.",
        suggestions=(
            PatternSuggestion(command="curl -X GET https://api.datadoghq.com/api/v1/dashboard/{id}", description="Get the resource first and export its configuration", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="datadog-ci monitors get {id}", description="Verify the resource id via the CLI before deletion", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="terraform-datadog-destroy",
        regex=r"terraform\b(?!\s+plan\b)[\s\S]*?\bdestroy\b[\s\S]*\bdatadog_[a-zA-Z0-9_]+\b",
        reason="terraform destroy targeting Datadog resources removes monitoring infrastructure",
        severity=Severity.HIGH,
        description="Terraform destroy removes Datadog monitors, dashboards, and other "
        "resources defined in the configuration. The monitoring resources are deleted "
        "immediately from Datadog.",
        suggestions=(
            PatternSuggestion(command="terraform plan -destroy -target={resource}", description="Preview the deletions before destroying", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="terraform state rm {resource}", description="Stop managing the resource without deleting it", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="newrelic-entity-delete",
        regex=r"\bnewrelic\b(?:\s+--?\S+(?:\s+\S+)?)*\s+entity\s+delete\b",
        reason="newrelic entity delete removes a New Relic entity, impacting observability",
        severity=Severity.HIGH,
        description="Deleting a New Relic entity removes all associated telemetry data, "
        "relationships, and alert configurations. Historical metrics for this entity "
        "are no longer accessible.",
        suggestions=(
            PatternSuggestion(command="newrelic entity search --name {name}", description="Find and verify the entity first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="newrelic entity tag add {guid} deprecated:true", description="Mark the entity deprecated instead of deleting", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="newrelic-apm-app-delete",
        regex=r"\bnewrelic\b(?:\s+--?\S+(?:\s+\S+)?)*\s+apm\s+application\s+delete\b",
        reason="newrelic apm application delete removes an APM application",
        severity=Severity.HIGH,
        description="Deleting an APM application removes all application performance "
        "data, traces, and associated alert policies. Visibility into application "
        "behavior and historical performance trends is lost.",
        suggestions=(
            PatternSuggestion(command="newrelic apm application get {id}", description="Review application details first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="newrelic apm application update {id} --enable=false", description="Disable the application instead of deleting", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="newrelic-workload-delete",
        regex=r"\bnewrelic\b(?:\s+--?\S+(?:\s+\S+)?)*\s+workload\s+delete\b",
        reason="newrelic workload delete removes a workload definition",
        severity=Severity.HIGH,
        description="Deleting a workload removes the logical grouping of entities and "
        "any associated health status calculations. Teams using this workload for "
        "service overview lose their aggregated view.",
        suggestions=(
            PatternSuggestion(command="newrelic entity search --type WORKLOAD --name {name}", description="Review workload entities and dependencies first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="curl -X GET https://api.newrelic.com/v2/alerts_workflows.json", description="Export the workload definition before deleting", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="newrelic-synthetics-delete",
        regex=r"\bnewrelic\b(?:\s+--?\S+(?:\s+\S+)?)*\s+synthetics\s+delete\b",
        reason="newrelic synthetics delete removes a synthetics monitor",
        severity=Severity.HIGH,
        description="Deleting a synthetics monitor stops all uptime and availability "
        "checking for the monitored endpoint. Alerts for the endpoint will no longer "
        "be received.",
        suggestions=(
            PatternSuggestion(command="newrelic synthetics search --name {name}", description="Verify you're deleting the correct monitor", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="newrelic synthetics update monitor {id} --enabled false", description="Disable the monitor temporarily instead of deleting", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="newrelic-api-delete",
        regex=r"(?i)\bcurl\b(?=.*(?:-X\s*|--request(?:=|\s+))DELETE\b)(?=.*api\.newrelic\.com).*",
        reason="curl DELETE to api.newrelic.com removes monitoring/alerting resources",
        severity=Severity.HIGH,
        description="Direct API DELETE calls permanently remove New Relic resources "
        "without confirmation. Alert policies, dashboards, and monitors are deleted "
        "immediately.",
        suggestions=(
            PatternSuggestion(command="curl -X GET https://api.newrelic.com/v2/alerts_policies.json", description="Get the resource first to verify the id", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="newrelic entity search --guid {guid}", description="Confirm the target resource via the CLI", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="newrelic-graphql-delete-mutation",
        regex=r"(?i)\bcurl\b(?=.*api\.newrelic\.com[^\s]*?/graphql\b)(?=.*\bmutation\b)(?=.*\bdelete\w*\b).*",
        reason="New Relic GraphQL delete mutations can remove monitoring resources",
        severity=Severity.HIGH,
        description="GraphQL delete mutations remove New Relic resources via the "
        "NerdGraph API. This affects entities, dashboards, alert policies, and other "
        "observability resources.",
        suggestions=(
            PatternSuggestion(command="curl -X POST https://api.newrelic.com/graphql -d '{\"query\":\"{ actor { entity(guid: \\\"{guid}\\\") { name } } }\"}'", description="Query the resource first to verify the GUID", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="newrelic entity search --guid {guid}", description="Verify the entity exists before the mutation", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="pd-service-delete",
        regex=r"\bpd\b(?:\s+--?\S+(?:\s+\S+)?)*\s+service\s+delete\b",
        reason="pd service delete removes a PagerDuty service, breaking incident routing",
        severity=Severity.CRITICAL,
        description="Deleting a PagerDuty service removes all incident routing, "
        "integrations, and escalation policies attached to it. Incidents will no "
        "longer be created for this service, potentially causing outages to go "
        "unnoticed.",
        suggestions=(
            PatternSuggestion(command="pd service list", description="Verify the service before deletion", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="pd service disable {id}", description="Disable the service temporarily instead of deleting", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="pd-schedule-delete",
        regex=r"\bpd\b(?:\s+--?\S+(?:\s+\S+)?)*\s+schedule\s+delete\b",
        reason="pd schedule delete removes a PagerDuty schedule",
        severity=Severity.HIGH,
        description="Deleting a schedule removes all on-call rotations and overrides. "
        "Escalation policies using this schedule will have gaps in coverage, "
        "potentially leaving incidents unassigned.",
        suggestions=(
            PatternSuggestion(command="pd schedule get {id}", description="Review schedule details first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="pd schedule update {id}", description="Update the schedule rather than deleting", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="pd-escalation-policy-delete",
        regex=r"\bpd\b(?:\s+--?\S+(?:\s+\S+)?)*\s+escalation-policy\s+delete\b",
        reason="pd escalation-policy delete breaks incident routing for its services",
        severity=Severity.HIGH,
        description="Deleting an escalation policy breaks incident routing for all "
        "services using it. Incidents may not be escalated properly, leading to "
        "delayed response times.",
        suggestions=(
            PatternSuggestion(command="pd escalation-policy get {id}", description="Review which services use this escalation policy", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="pd service update {service_id} --escalation-policy {new_id}", description="Assign services to a different policy before deletion", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="pd-user-delete",
        regex=r"\bpd\b(?:\s+--?\S+(?:\s+\S+)?)*\s+user\s+delete\b",
        reason="pd user delete removes a PagerDuty user",
        severity=Severity.HIGH,
        description="Deleting a user removes them from all schedules and escalation "
        "policies. On-call coverage may have gaps, and incident history associated "
        "with this user may be affected.",
        suggestions=(
            PatternSuggestion(command="pd user get {id}", description="Review the user's assignments first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="pd user disable {id}", description="Deactivate the user instead of deleting", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="pd-team-delete",
        regex=r"\bpd\b(?:\s+--?\S+(?:\s+\S+)?)*\s+team\s+delete\b",
        reason="pd team delete removes a PagerDuty team",
        severity=Severity.HIGH,
        description="Deleting a team removes the organizational grouping and may "
        "affect access controls, service ownership, and escalation policies.",
        suggestions=(
            PatternSuggestion(command="pd team list", description="Verify the team before deletion", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="pd team-member list {team_id}", description="Reassign team members and services first", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="pagerduty-api-delete-service",
        regex=r"(?i)\bcurl\b(?=.*(?:-X\s*|--request(?:=|\s+))DELETE\b)(?=.*api\.pagerduty\.com[^\s]*?/services/[^\s]+).*",
        reason="PagerDuty API DELETE /services/{id} deletes a PagerDuty service",
        severity=Severity.CRITICAL,
        description="Direct API deletion of a service removes all incident routing "
        "immediately. There is no confirmation prompt when using curl directly.",
        suggestions=(
            PatternSuggestion(command="curl -X GET https://api.pagerduty.com/services/{id}", description="Get the service first to verify the ID", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="pd service disable {id}", description="Disable the service via the CLI instead", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="pagerduty-api-delete-schedule",
        regex=r"(?i)\bcurl\b(?=.*(?:-X\s*|--request(?:=|\s+))DELETE\b)(?=.*api\.pagerduty\.com[^\s]*?/schedules/[^\s]+).*",
        reason="PagerDuty API DELETE /schedules/{id} deletes a PagerDuty schedule",
        severity=Severity.HIGH,
        description="API deletion of a schedule removes on-call rotations immediately. "
        "Escalation policies referencing this schedule will have coverage gaps.",
        suggestions=(
            PatternSuggestion(command="curl -X GET https://api.pagerduty.com/schedules/{id}", description="Get the schedule first to verify the ID", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="pd schedule update {id}", description="Update the schedule via the CLI instead", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="splunk-remove-index",
        regex=r"splunk\b.*?\bremove\s+index\b",
        reason="splunk remove index deletes an index and its data permanently",
        severity=Severity.CRITICAL,
        description="Removing a Splunk index permanently deletes all indexed data "
        "within it. Historical logs, events, and metrics are irretrievably lost. "
        "Any searches, dashboards, or alerts referencing this index will fail.",
        suggestions=(
            PatternSuggestion(command="splunk list index", description="Review the index before removal", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="splunk export index {index} {dir}", description="Archive the data before deletion", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="splunk-clean-eventdata",
        regex=r"splunk\b.*?\bclean\s+eventdata\b",
        reason="splunk clean eventdata permanently deletes indexed data",
        severity=Severity.CRITICAL,
        description="Clean eventdata permanently removes all events from the specified "
        "index. This cannot be undone. Use this only when you're certain the data is "
        "no longer needed.",
        suggestions=(
            PatternSuggestion(command="splunk search 'index={index}'", description="Verify what will be deleted", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="splunk export index {index} {dir}", description="Export the data before cleaning", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="splunk-delete-user-role",
        regex=r"splunk\b.*?\bdelete\s+(?:user|role)\b",
        reason="splunk delete user/role removes access configurations",
        severity=Severity.HIGH,
        description="Deleting a user removes their access and any saved searches or "
        "dashboards owned by them. Deleting a role affects all users assigned to it, "
        "potentially breaking access controls.",
        suggestions=(
            PatternSuggestion(command="splunk list user", description="Review the user or role before deletion", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="splunk disable user {name}", description="Disable the user instead of deleting", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="splunk-api-delete",
        regex=r"(?i)curl\s+.*(?:-X\s*|--request(?:=|\s+))DELETE\b.*splunk.*\/services\/",
        reason="Splunk REST API DELETE calls can permanently remove objects",
        severity=Severity.HIGH,
        description="Direct API DELETE calls to Splunk services can remove indexes, "
        "saved searches, dashboards, alerts, and other objects without confirmation.",
        suggestions=(
            PatternSuggestion(command="curl -X GET https://{host}:8089/services/{object}", description="Get the resource first to verify the object", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="splunk list", description="Use the Splunk CLI for better feedback", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
)


def build_monitoring_pack() -> Pack:
    return Pack(
        id="monitoring",
        name="Monitoring",
        destructive_patterns=MONITORING_PATTERNS,
        keywords=("promtool", "grafana", "influx", "whisper", "prometheus", "kubectl", "curl", "datadog", "newrelic", "pd", "pagerduty", "splunk", "api.datadoghq.com", "api.newrelic.com", "api.pagerduty.com"),
    )
