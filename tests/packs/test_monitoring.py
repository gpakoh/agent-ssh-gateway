"""Monitoring pack destructive pattern tests (P18)."""

from __future__ import annotations

from app.packs.registry import build_registry


class TestMonitoringPack:
    def test_monitoring_pack_patterns(self):
        """Monitoring pack (P18) covers promtool, grafana, influx, whisper, SaaS."""
        r = build_registry()
        cases = {
            "promtool tsdb delete --match job=x": "promtool-tsdb-delete",
            "curl -X POST http://localhost:9090/api/v1/admin/tsdb/delete_series --data match[]=up": "prometheus-api-delete-series",
            "grafana-cli plugins uninstall grafana-piechart-panel": "grafana-cli-plugins-uninstall",
            "curl -X DELETE http://localhost:3000/api/dashboards/uid/abc": "grafana-api-delete-dashboard",
            "influx delete --bucket b --start 2020-01-01T00:00:00Z --stop 2020-01-02T00:00:00Z": "influx-delete",
            "influx bucket delete --id 1": "influx-bucket-delete",
            "influx org delete --id 2": "influx-org-delete",
            "whisper-delete.py /var/lib/graphite/whisper/cpu.wsp": "whisper-delete",
            "kubectl delete prometheusrule my-alert -n monitoring": "kubectl-delete-monitoring-resources",
            "datadog-ci monitors delete 123": "datadog-ci-monitors-delete",
            "datadog-ci dashboards delete abc": "datadog-ci-dashboards-delete",
            "curl -X DELETE https://api.datadoghq.com/api/v1/dashboard/abc": "datadog-api-delete",
            "terraform destroy -target=datadog_monitor.alerts": "terraform-datadog-destroy",
            "newrelic entity delete 123": "newrelic-entity-delete",
            "newrelic apm application delete 123": "newrelic-apm-app-delete",
            "newrelic workload delete 123": "newrelic-workload-delete",
            "newrelic synthetics delete 123": "newrelic-synthetics-delete",
            "curl -X DELETE https://api.newrelic.com/v2/alerts_policies/123.json": "newrelic-api-delete",
            "curl -X POST https://api.newrelic.com/graphql -d '{\"query\":\"mutation { deleteEntity(guid: \\\"abc\\\") }\"}'": "newrelic-graphql-delete-mutation",
            "pd service delete P123": "pd-service-delete",
            "pd schedule delete P234": "pd-schedule-delete",
            "pd escalation-policy delete P345": "pd-escalation-policy-delete",
            "pd user delete P456": "pd-user-delete",
            "pd team delete P567": "pd-team-delete",
            "curl -X DELETE https://api.pagerduty.com/services/P123": "pagerduty-api-delete-service",
            "curl -X DELETE https://api.pagerduty.com/schedules/P234": "pagerduty-api-delete-schedule",
            "splunk remove index main": "splunk-remove-index",
            "splunk clean eventdata -index main": "splunk-clean-eventdata",
            "splunk delete user alice": "splunk-delete-user-role",
            "curl -X DELETE https://splunk.example.com:8089/services/data/inputs/abc": "splunk-api-delete",
        }
        for cmd, expected in cases.items():
            matches = r.evaluate(cmd)
            names = {m.pattern_name for m in matches}
            assert expected in names, f"{cmd!r}: expected {expected}, got {names}"

    def test_monitoring_pack_reads_not_blocked(self):
        """Read/list operations on monitoring tools must NOT be blocked."""
        r = build_registry()
        for cmd in (
            "promtool tsdb list",
            "promtool check rules /etc/prometheus/rules.yml",
            "grafana-cli plugins ls",
            "curl http://localhost:3000/api/datasources",
            "curl -X GET http://localhost:9090/api/v1/series?match[]=up",
            "influx bucket list",
            "influx task list",
            "influx query 'from(bucket:\"b\") |> range(start: -1h)'",
            "datadog-ci monitors list",
            "datadog-ci dashboards get 123",
            "curl -X GET https://api.datadoghq.com/api/v1/monitor",
            "terraform plan -destroy -target=datadog_monitor.alerts",
            "newrelic entity search --name my-service",
            "newrelic apm application get 123",
            "newrelic query \"SELECT count(*) FROM Transaction\"",
            "curl -X GET https://api.newrelic.com/v2/alerts_policies.json",
            "pd service list",
            "pd schedule get P234",
            "pd incident list",
            "curl -X GET https://api.pagerduty.com/services",
            "splunk list index",
            "splunk show config",
            "splunk search 'index=main error'",
        ):
            matches = r.evaluate(cmd)
            assert len(matches) == 0, f"False positive for {cmd!r}: {[m.pattern_name for m in matches]}"

    def test_monitoring_saas_curl_get_does_not_mask_delete(self):
        """curl GET plus a later DELETE in the same command must still block."""
        r = build_registry()
        for cmd, expected in {
            "curl -X GET https://api.datadoghq.com/api/v1/monitor -X DELETE https://api.datadoghq.com/api/v1/dashboard/abc": "datadog-api-delete",
            "curl https://api.datadoghq.com/api/v1/dashboard/abc -XDELETE": "datadog-api-delete",
            "curl -X GET https://api.newrelic.com/v2/alerts_policies.json -X DELETE https://api.newrelic.com/v2/alerts_policies/123.json": "newrelic-api-delete",
            "curl https://api.newrelic.com/v2/alerts_policies/123.json --request=DELETE": "newrelic-api-delete",
            "curl -X GET https://api.pagerduty.com/services -X DELETE https://api.pagerduty.com/services/P123": "pagerduty-api-delete-service",
            "curl https://api.pagerduty.com/schedules/P234 -XDELETE": "pagerduty-api-delete-schedule",
        }.items():
            matches = r.evaluate(cmd)
            names = {m.pattern_name for m in matches}
            assert expected in names, f"{cmd!r}: expected {expected}, got {names}"
