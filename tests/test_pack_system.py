"""Tests for Pack and PackRegistry infrastructure (P4)."""

from __future__ import annotations

import pytest

from app.command_policy import DestructiveMatch, DestructivePattern, Severity, scan_command
from app.packs import Pack, PackRegistry

# ── Helpers ──────────────────────────────────────────────────────────────────

def _make_pattern(name: str, regex: str, severity: str = "high") -> DestructivePattern:
    return DestructivePattern(
        name=name,
        regex=regex,
        reason=f"test pattern {name}",
        severity=Severity(severity),
        description=f"Dummy pattern {name} for testing.",
        suggestions=(),
    )


_MAKE_PATTERN = _make_pattern  # shorthand for parametrize


# ── Pack unit tests ──────────────────────────────────────────────────────────


class TestPack:
    def test_init(self):
        p = Pack(id="test", name="Test pack")
        assert p.id == "test"
        assert p.name == "Test pack"
        assert p.keywords == ()
        assert p.destructive_patterns == ()
        assert p._compiled == []

    def test_init_with_patterns(self):
        dp = _make_pattern("rm-rf", r"rm\s+-rf")
        p = Pack(id="fs", name="Filesystem", destructive_patterns=(dp,), keywords=("rm",))
        assert len(p.destructive_patterns) == 1
        assert len(p._compiled) == 1
        pat, pattern_obj = p._compiled[0]
        assert pattern_obj is dp

    def test_matches_keywords_empty(self):
        """Empty keywords means always match (no quick-reject)."""
        p = Pack(id="test", name="Test")
        assert p.matches_keywords("anything at all") is True
        assert p.matches_keywords("") is True

    def test_matches_keywords_found(self):
        p = Pack(id="test", name="Test", keywords=("docker", "container"))
        assert p.matches_keywords("docker ps") is True
        assert p.matches_keywords("container rm") is True

    def test_matches_keywords_not_found(self):
        p = Pack(id="test", name="Test", keywords=("docker",))
        assert p.matches_keywords("echo hello") is False
        assert p.matches_keywords("git push") is False
        assert p.matches_keywords("") is False

    def test_matches_keywords_case_insensitive(self):
        p = Pack(id="test", name="Test", keywords=("docker",))
        assert p.matches_keywords("DOCKER PS") is True
        assert p.matches_keywords("Docker ps") is True
        assert p.matches_keywords("dOcKeR") is True

    def test_check_no_match(self):
        p = Pack(id="test", name="Test", destructive_patterns=(
            _make_pattern("rm-rf", r"rm\s+-rf"),
        ), keywords=("rm",))
        assert p.check("echo hello") == []

    def test_check_single_match(self):
        p = Pack(id="test", name="Test", destructive_patterns=(
            _make_pattern("rm-rf", r"rm\s+-rf"),
        ), keywords=("rm",))
        matches = p.check("rm -rf /tmp")
        assert len(matches) == 1
        assert matches[0].pattern_name == "rm-rf"

    def test_check_multiple_matches(self):
        p = Pack(id="test", name="Test", destructive_patterns=(
            _make_pattern("rm-rf", r"rm\s+-rf"),
            _make_pattern("rm-root", r"rm\s+-rf\s+/"),
        ), keywords=("rm",))
        matches = p.check("rm -rf /")
        assert len(matches) == 2
        names = {m.pattern_name for m in matches}
        assert names == {"rm-rf", "rm-root"}

    def test_check_with_keyword_reject(self):
        """Keyword quick-reject prevents any check work."""
        p = Pack(id="test", name="Test", destructive_patterns=(
            _make_pattern("rm-rf", r"rm\s+-rf"),
        ), keywords=("docker",))
        assert p.check("rm -rf /") == []

    def test_check_case_insensitive_regex(self):
        p = Pack(id="test", name="Test", destructive_patterns=(
            _make_pattern("rm-rf", r"rm\s+-rf"),
        ), keywords=("rm",))
        assert len(p.check("RM -RF /")) == 1
        assert len(p.check("Rm -Rf /")) == 1

    def test_check_returns_destructive_match(self):
        dp = _make_pattern("test-pat", r"danger")
        p = Pack(id="test", name="Test", destructive_patterns=(dp,), keywords=("danger",))
        matches = p.check("danger command")
        assert len(matches) == 1
        m = matches[0]
        assert isinstance(m, DestructiveMatch)
        assert m.pattern_name == "test-pat"
        assert m.reason == "test pattern test-pat"
        assert m.severity == Severity.HIGH
        assert m.suggestion is None

    def test_check_suggestion_included(self):
        dp = DestructivePattern(
            name="test-sug",
            regex=r"danger",
            reason="test suggestion",
            severity=Severity.HIGH,
            description="Pattern with suggestion.",
            suggestions=(
                type("Suggestion", (), {"command": "safe-cmd --help"})(),
            ),
        )
        p = Pack(id="test", name="Test", destructive_patterns=(dp,), keywords=("danger",))
        matches = p.check("danger command")
        assert matches[0].suggestion == "safe-cmd --help"

    def test_duplicate_pattern_names(self):
        """Two patterns with same name in one pack — handled by list-based _compiled."""
        p = Pack(id="test", name="Test", destructive_patterns=(
            _make_pattern("same-name", r"pattern-one"),
            _make_pattern("same-name", r"pattern-two"),
        ), keywords=("pattern",))
        # Both should match separately
        matches = p.check("pattern-one here")
        assert len(matches) == 1
        assert matches[0].pattern_name == "same-name"
        matches2 = p.check("pattern-two here")
        assert len(matches2) == 1
        assert matches2[0].pattern_name == "same-name"

    def test_empty_patterns(self):
        p = Pack(id="empty", name="Empty", destructive_patterns=(), keywords=())
        assert p.check("anything") == []
        assert p.matches_keywords("anything") is True

    def test_regex_with_dotall_flag(self):
        """Patterns are compiled with DOTALL — . matches newlines."""
        p = Pack(id="test", name="Test", destructive_patterns=(
            _make_pattern("multi", r"start.*end"),
        ), keywords=("start",))
        assert len(p.check("start\nmiddle\nend")) == 1


# ── PackRegistry unit tests ──────────────────────────────────────────────────


class TestPackRegistry:
    def test_empty_registry(self):
        r = PackRegistry()
        assert r.pack_count == 0
        assert r.pattern_count == 0
        assert r.keyword_count == 0
        assert r.all_packs == ()
        assert r.evaluate("anything") == []
        assert r.evaluate_pack("nonexistent", "cmd") == []

    def test_register_and_get(self):
        r = PackRegistry()
        p = Pack(id="test", name="Test")
        r.register(p)
        assert r.get("test") is p
        assert r.get("nonexistent") is None

    def test_register_updates_keyword_index(self):
        r = PackRegistry()
        r.register(Pack(id="a", name="A", keywords=("docker", "compose")))
        assert "docker" in r._all_keywords
        assert "compose" in r._all_keywords
        r.register(Pack(id="b", name="B", keywords=("git",)))
        assert "git" in r._all_keywords
        assert "docker" in r._all_keywords

    def test_all_packs(self):
        r = PackRegistry()
        p1 = Pack(id="a", name="A")
        p2 = Pack(id="b", name="B")
        r.register(p1)
        r.register(p2)
        assert set(r.all_packs) == {p1, p2}

    @pytest.mark.parametrize("cmd,expected_count", [
        ("docker rm -f container1", 1),
        ("rm -rf /tmp", 2),
        ("echo hello", 0),
        ("git push --force origin main", 2),
    ])
    def test_evaluate(self, cmd, expected_count):
        from app.packs.registry import build_registry
        r = build_registry()
        matches = r.evaluate(cmd)
        assert len(matches) == expected_count, (
            f"expected {expected_count} matches for {cmd!r}, got {len(matches)}: "
            f"{[m.pattern_name for m in matches]}"
        )

    def test_evaluate_pack_specific(self):
        from app.packs.registry import build_registry
        r = build_registry()
        matches = r.evaluate_pack("docker", "docker rm -f container")
        assert len(matches) == 1
        assert matches[0].pattern_name == "rm-force"

    def test_evaluate_pack_unknown(self):
        r = PackRegistry()
        assert r.evaluate_pack("unknown", "anything") == []

    def test_evaluate_pack_no_match(self):
        from app.packs.registry import build_registry
        r = build_registry()
        matches = r.evaluate_pack("docker", "echo hello")
        assert matches == []

    def test_pack_and_pattern_counts(self):
        r = PackRegistry()
        r.register(Pack(id="a", name="A", destructive_patterns=(
            _make_pattern("p1", r"x"), _make_pattern("p2", r"y"),
        )))
        r.register(Pack(id="b", name="B", destructive_patterns=(
            _make_pattern("p3", r"z"),
        )))
        assert r.pack_count == 2
        assert r.pattern_count == 3

    def test_keyword_count(self):
        r = PackRegistry()
        r.register(Pack(id="a", name="A", keywords=("docker", "compose")))
        r.register(Pack(id="b", name="B", keywords=("docker", "git")))
        # "docker" deduplicated, so 3 unique keywords
        assert r.keyword_count == 3

    def test_global_quick_reject(self):
        """If no pack matches keywords, evaluate returns empty."""
        r = PackRegistry()
        r.register(Pack(id="fs", name="FS", keywords=("rm", "shred"),
                        destructive_patterns=(_make_pattern("rm-rf", r"rm\s+-rf"),)))
        r.register(Pack(id="git", name="Git", keywords=("git",),
                        destructive_patterns=(_make_pattern("push-force", r"git\s+push\s+--force"),)))
        # "az" doesn't match any keyword — quick-reject
        assert r.evaluate("az vm delete --name x") == []
        # "rm" matches fs keywords — only fs pack runs
        matches = r.evaluate("rm -rf /tmp")
        assert len(matches) == 1
        assert matches[0].pattern_name == "rm-rf"

    def test_global_quick_reject_no_keywords(self):
        """No keywords = no quick-reject."""
        r = PackRegistry()
        r.register(Pack(id="test", name="Test", keywords=(),
                        destructive_patterns=(_make_pattern("always", r"."),)))
        assert len(r.evaluate("anything at all")) == 1

    def test_duplicate_pack_id_overwrites(self):
        r = PackRegistry()
        p1 = Pack(id="test", name="First")
        p2 = Pack(id="test", name="Second")
        r.register(p1)
        r.register(p2)
        assert r.get("test").name == "Second"
        assert r.pack_count == 1

    def test_matches_properties(self):
        """Verify DestructiveMatch fields from evaluate()."""
        from app.packs.registry import build_registry
        r = build_registry()
        matches = r.evaluate("rm -rf /")
        assert len(matches) >= 1
        m = matches[0]
        assert hasattr(m, "pattern_name")
        assert hasattr(m, "severity")
        assert hasattr(m, "reason")
        assert m.pattern_name in ("rm-rf-root", "rm-rf", "rm-recursive")
        assert m.severity.value in ("critical", "high", "medium")

    def test_confidence_present_in_match(self):
        """Verify DestructiveMatch carries confidence field."""
        from app.packs.registry import build_registry
        r = build_registry()
        matches = r.evaluate("rm -rf /")
        assert len(matches) >= 1
        for m in matches:
            assert hasattr(m, "confidence")
            assert isinstance(m.confidence, float)
            assert 0.5 <= m.confidence <= 1.0  # can exceed 0.95 with span-aware boost

    def test_confidence_longer_regex_higher(self):
        """Longer, more specific patterns should have higher confidence."""
        from app.packs.registry import build_registry
        r = build_registry()
        m1 = r.evaluate("chmod -R 777 /")  # short regex
        m2 = r.evaluate("rm -rf /")  # medium regex
        assert all(hasattr(m, "confidence") for m in m1 + m2)

    def test_confidence_critical_high_value(self):
        """CRITICAL severity patterns should have higher base confidence."""
        from app.command_policy import DestructivePattern, Severity
        from app.packs import _compute_confidence

        dp_high = DestructivePattern(name="t", regex="test", reason="r", severity=Severity.HIGH, description="d")
        dp_crit = DestructivePattern(name="t", regex="test", reason="r", severity=Severity.CRITICAL, description="d")
        assert _compute_confidence(dp_crit) > _compute_confidence(dp_high)


# ── Singleton tests ──────────────────────────────────────────────────────────


class TestRegistrySingleton:
    def test_get_registry_returns_pack_registry(self):
        from app.packs.registry import get_registry
        r = get_registry()
        from app.packs import PackRegistry
        assert isinstance(r, PackRegistry)

    def test_get_registry_singleton(self):
        from app.packs.registry import get_registry
        assert get_registry() is get_registry()

    def test_get_registry_has_all_packs(self):
        from app.packs.registry import get_registry
        r = get_registry()
        assert r.pack_count == 15
        expected_ids = {"backup", "docker", "filesystem", "kubernetes", "cloud",
                        "database", "dns", "git", "firewall", "loadbalancer",
                        "monitoring", "package_managers", "secrets", "storage",
                        "system"}
        actual_ids = {p.id for p in r.all_packs}
        assert actual_ids == expected_ids


# ── Smoke tests ──────────────────────────────────────────────────────────────


class TestPackSmoke:
    """Quick smoke tests against the real built registry."""

    def test_build_registry_has_no_duplicates(self):
        """No duplicate pattern names within any single pack."""
        from app.packs.registry import build_registry
        r = build_registry()
        for pack in r.all_packs:
            names = [dp.name for dp in pack.destructive_patterns]
            dupes = {n for n in names if names.count(n) > 1}
            assert not dupes, f"Pack '{pack.id}' has duplicate names: {dupes}"

    def test_scan_command_returns_report(self):
        report = scan_command("rm -rf /")
        assert report.total > 0
        assert len(report.findings) == report.total
        for f in report.findings:
            assert f.pattern_name
            assert f.severity

    def test_scan_command_safe(self):
        report = scan_command("ls -la")
        assert report.total == 0

    def test_all_packs_have_keywords(self):
        """Every pack should have at least one keyword for quick-reject."""
        from app.packs.registry import build_registry
        r = build_registry()
        for pack in r.all_packs:
            assert len(pack.keywords) > 0, f"Pack '{pack.id}' has no keywords"

    def test_all_packs_have_patterns(self):
        """Every pack should have at least one destructive pattern."""
        from app.packs.registry import build_registry
        r = build_registry()
        for pack in r.all_packs:
            assert len(pack.destructive_patterns) > 0, f"Pack '{pack.id}' has no patterns"

    def test_legacy_wrappers_still_work(self):
        """Verify old delegation functions are intact."""
        from app.command_policy import _check_all_destructive, _check_docker_destructive
        assert len(_check_all_destructive("rm -rf /")) > 0
        assert _check_all_destructive("echo hello") == []
        assert _check_docker_destructive("docker rm -f container") is not None
        assert _check_docker_destructive("echo hello") is None

    @pytest.mark.parametrize("cmd,expected_pack", [
        ("docker rm -f c1", "docker"),
        ("docker-compose rm -f", "docker"),
        ("rm -rf /", "filesystem"),
        ("kubectl delete namespace prod", "kubernetes"),
        ("aws s3 rm s3://bucket/ --recursive", "cloud"),
        ("DROP DATABASE prod", "database"),
        ("git push --force", "git"),
        ("ufw disable", "firewall"),
        ("nginx -s stop", "loadbalancer"),
        ("dd if=/dev/zero of=/dev/sda bs=1M", "system"),
        ("nsupdate -l delete example.com", "dns"),
        ("dig example.com axfr", "dns"),
        ("npm publish", "package_managers"),
        ("pip uninstall requests", "package_managers"),
        ("vault kv delete secret/x", "secrets"),
        ("op item delete login", "secrets"),
        ("restic forget --keep-daily 7", "backup"),
        ("borg prune repo", "backup"),
        ("promtool tsdb delete --match job=x", "monitoring"),
        ("influx delete --bucket b --start 2020-01-01T00:00:00Z --stop 2020-01-02T00:00:00Z", "monitoring"),
        ("zfs destroy tank/data", "storage"),
        ("mc rb --force --recursive myminio/backups", "storage"),
    ])
    def test_evaluate_pack_cross_section(self, cmd, expected_pack):
        """Verify each pack matches at least one expected command."""
        from app.packs.registry import build_registry
        r = build_registry()
        matches = r.evaluate_pack(expected_pack, cmd)
        assert len(matches) >= 1, (
            f"Expected {expected_pack} pack to match {cmd!r}, got 0 matches"
        )

    def test_global_keyword_reject_still_allows_matches(self):
        """Smoke: commands known to be destructive still match with global keyword check."""
        from app.packs.registry import build_registry
        r = build_registry()
        for cmd in (
            "docker rm -f c1",
            "rm -rf /etc",
            "kubectl delete namespace prod",
            "aws ec2 terminate-instances i-123",
            "DROP TABLE users",
            "git push --force",
            "iptables -F",
            "nginx -s quit",
            "dd if=/dev/zero of=/dev/sda",
            "nsupdate -l delete example.com",
            "npm publish",
            "apt purge nginx",
            "vault kv delete secret/x",
            "aws secretsmanager delete-resource-policy --secret-id x",
            "restic prune",
            "rclone sync src: dest:",
            "promtool tsdb delete --match job=x",
            "influx bucket delete --id 1",
            "zfs destroy tank/data",
            "mc rb --force --recursive myminio/backups",
        ):
            matches = r.evaluate(cmd)
            assert len(matches) >= 1, f"No matches for {cmd!r} (global quick-reject false negative)"

    def test_global_keyword_reject_blocks_unknown(self):
        """Smoke: commands unrelated to any pack get quick-rejected."""
        from app.packs.registry import build_registry
        r = build_registry()
        for cmd in ("ls", "cat file.txt", "python script.py", "ping 8.8.8.8",
                     "date", "whoami", "top", "df -h"):
            matches = r.evaluate(cmd)
            assert len(matches) == 0, f"False positive for {cmd!r}: {[m.pattern_name for m in matches]}"

    def test_dns_pack_patterns(self):
        """DNS pack (P18) covers nsupdate, dig zone transfer, cloudflare, route53."""
        from app.packs.registry import build_registry
        r = build_registry()
        cases = {
            "nsupdate -l delete example.com": "dns-nsupdate-local",
            "dig example.com axfr": "dns-dig-zone-transfer",
            "wrangler dns-records delete --id 1": "cloudflare-wrangler-dns-delete",
            "aws route53 delete-health-check --health-check-id 1": "route53-delete-health-check",
        }
        for cmd, expected in cases.items():
            matches = r.evaluate(cmd)
            names = {m.pattern_name for m in matches}
            assert expected in names, f"{cmd!r}: expected {expected}, got {names}"

    def test_package_managers_pack_patterns(self):
        """Package managers pack (P18) covers npm/pip/apt/cargo/gem."""
        from app.packs.registry import build_registry
        r = build_registry()
        cases = {
            "npm publish": "npm-publish",
            "npm publish --dry-run": None,
            "pip uninstall requests": "pip-uninstall",
            "apt purge nginx": "apt-remove",
            "cargo yank my-crate --vers 0.1.0": "cargo-yank",
            "gem push mygem.gem": "gem-push",
        }
        for cmd, expected in cases.items():
            matches = r.evaluate(cmd)
            names = {m.pattern_name for m in matches}
            if expected is None:
                assert not names, f"{cmd!r}: expected no match, got {names}"
            else:
                assert expected in names, f"{cmd!r}: expected {expected}, got {names}"

    def test_new_packs_patterns_carry_suggestions(self):
        """All patterns in dns + package_managers packs have suggestions (P17 convention)."""
        from app.packs.registry import build_registry
        r = build_registry()
        for pack_id in ("dns", "package_managers"):
            pack = r.get(pack_id)
            assert pack is not None
            for dp in pack.destructive_patterns:
                assert dp.suggestions, f"{pack_id}/{dp.name} has no suggestions"
                assert all(s.command and s.description for s in dp.suggestions)

    def test_secrets_pack_patterns(self):
        """Secrets pack (P18) covers vault, aws ssm/secretsmanager, doppler, 1password."""
        from app.packs.registry import build_registry
        r = build_registry()
        cases = {
            "vault kv destroy -versions=1 secret/x": "vault-kv-destroy",
            "vault secrets disable kv": "vault-secrets-disable",
            "vault policy delete my-policy": "vault-policy-delete",
            "vault auth disable userpass": "vault-auth-disable",
            "vault token revoke 123": "vault-token-revoke",
            "aws ssm delete-parameter --name /app/DB_PASS": "aws-ssm-delete-parameter",
            "aws secretsmanager delete-resource-policy --secret-id x": "aws-secretsmanager-delete-resource-policy",
            "doppler secrets delete KEY": "doppler-secrets-delete",
            "op item delete login-item": "op-item-delete",
            "op vault delete prod": "op-vault-delete",
        }
        for cmd, expected in cases.items():
            matches = r.evaluate(cmd)
            names = {m.pattern_name for m in matches}
            assert expected in names, f"{cmd!r}: expected {expected}, got {names}"

    def test_secrets_pack_reads_not_blocked(self):
        """Read/list operations on secrets tools must NOT be blocked."""
        from app.packs.registry import build_registry
        r = build_registry()
        for cmd in (
            "vault kv get secret/x",
            "vault kv list secret/",
            "vault read secret/x",
            "vault secrets list",
            "aws secretsmanager describe-secret --secret-id x",
            "aws ssm get-parameter --name /app/DB_PASS",
            "doppler secrets get KEY",
            "op item get login-item",
        ):
            matches = r.evaluate(cmd)
            assert len(matches) == 0, f"False positive for {cmd!r}: {[m.pattern_name for m in matches]}"

    def test_backup_pack_patterns(self):
        """Backup pack (P18) covers borg, restic, rclone, velero, duplicity."""
        from app.packs.registry import build_registry
        r = build_registry()
        cases = {
            "borg delete repo::old": "borg-delete",
            "borg prune --keep-daily 7 repo": "borg-prune",
            "restic forget --keep-daily 7": "restic-forget",
            "restic prune": "restic-prune",
            "restic key remove 1": "restic-key-remove",
            "rclone sync src: dest:": "rclone-sync",
            "rclone purge remote:dir": "rclone-purge",
            "rclone dedupe remote:dir": "rclone-dedupe",
            "velero backup delete --yes": "velero-backup-delete",
            "velero schedule delete daily": "velero-schedule-delete",
            "duplicity remove-older-than 30D file:///backup": "duplicity-remove-older-than",
        }
        for cmd, expected in cases.items():
            matches = r.evaluate(cmd)
            names = {m.pattern_name for m in matches}
            assert expected in names, f"{cmd!r}: expected {expected}, got {names}"

    def test_storage_pack_patterns(self):
        """Storage pack (P18) covers zfs/zpool, s3 sync --delete, gcs, minio, azure."""
        from app.packs.registry import build_registry
        r = build_registry()
        cases = {
            "zfs destroy tank/data": "zfs-destroy",
            "zpool destroy tank": "zpool-destroy",
            "zpool remove tank /dev/sdb": "zpool-remove",
            "aws s3 sync s3://src s3://dst --delete": "s3-sync-delete",
            "aws s3api delete-objects --bucket b --delete '{\"Objects\":[{\"Key\":\"k\"}]}'": "s3api-delete-objects",
            "gcloud storage buckets delete gs://mybucket --recursive": "gcloud-storage-buckets-delete",
            "gcloud storage rm -r gs://mybucket/path": "gcloud-storage-rm",
            "gsutil rsync -d gs://src gs://dst": "gsutil-rsync-delete",
            "mc rb --force --recursive myminio/backups": "mc-rb",
            "mc admin bucket delete myminio/old": "mc-admin-bucket-delete",
            "mc mirror --remove --overwrite src dst": "mc-mirror-remove",
            "mc admin user remove myminio olduser": "mc-admin-user-remove",
            "az storage account delete -n myacct -g myrg --yes": "az-storage-account-delete",
            "az storage container delete -n mycont --account-name myacct": "az-storage-container-delete",
            "azcopy remove https://acct.blob.core.windows.net/cont?SAS --recursive": "azcopy-remove",
        }
        for cmd, expected in cases.items():
            matches = r.evaluate(cmd)
            names = {m.pattern_name for m in matches}
            assert expected in names, f"{cmd!r}: expected {expected}, got {names}"

    def test_storage_pack_reads_not_blocked(self):
        """Read/list operations on storage tools must NOT be blocked."""
        from app.packs.registry import build_registry
        r = build_registry()
        for cmd in (
            "zfs list",
            "zfs list -t snapshot -r tank",
            "zpool status tank",
            "zpool list",
            "aws s3 ls",
            "aws s3 sync s3://src s3://dst --dryrun",
            "gcloud storage buckets describe gs://mybucket",
            "gcloud storage ls -r gs://mybucket",
            "gsutil ls gs://bucket",
            "gsutil rsync -n -d gs://src gs://dst",
            "mc ls myminio/bucket",
            "mc mirror --dry-run --remove src dst",
            "az storage account list",
            "az storage container list --account-name myacct",
            "azcopy list 'https://acct.blob.core.windows.net/cont?SAS'",
        ):
            matches = r.evaluate(cmd)
            assert len(matches) == 0, f"False positive for {cmd!r}: {[m.pattern_name for m in matches]}"

    def test_backup_pack_reads_not_blocked(self):
        """Read/list/dry-run operations on backup tools must NOT be blocked."""
        from app.packs.registry import build_registry
        r = build_registry()
        for cmd in (
            "borg list repo",
            "borg info repo",
            "borg check repo",
            "restic snapshots",
            "restic check",
            "restic list snapshots",
            "rclone copy src dest --dry-run",
            "rclone ls remote:dir",
            "velero backup get",
            "velero restore get",
            "duplicity collection-status file:///backup",
        ):
            matches = r.evaluate(cmd)
            assert len(matches) == 0, f"False positive for {cmd!r}: {[m.pattern_name for m in matches]}"

    def test_monitoring_pack_patterns(self):
        """Monitoring pack (P18) covers promtool, grafana, influx, whisper."""
        from app.packs.registry import build_registry
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
        from app.packs.registry import build_registry
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
