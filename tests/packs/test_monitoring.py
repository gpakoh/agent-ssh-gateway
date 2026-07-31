"""Monitoring pack destructive pattern tests (P18)."""

from __future__ import annotations

from app.packs.registry import build_registry


class TestMonitoringPack:
    def test_monitoring_pack_patterns(self):
        """Monitoring pack (P18) covers promtool, grafana, influx, whisper."""
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
        ):
            matches = r.evaluate(cmd)
            assert len(matches) == 0, f"False positive for {cmd!r}: {[m.pattern_name for m in matches]}"
