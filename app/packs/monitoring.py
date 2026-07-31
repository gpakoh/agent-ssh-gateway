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
)


def build_monitoring_pack() -> Pack:
    return Pack(
        id="monitoring",
        name="Monitoring",
        destructive_patterns=MONITORING_PATTERNS,
        keywords=("promtool", "grafana", "influx", "whisper", "prometheus", "kubectl", "curl"),
    )
