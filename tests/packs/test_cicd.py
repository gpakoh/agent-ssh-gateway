"""CI/CD pack destructive pattern tests (P18)."""

from __future__ import annotations

from app.packs.registry import build_registry


class TestCicdPack:
    def test_cicd_pack_patterns(self):
        """CI/CD pack (P18) covers gh, glab, gitlab-runner, circleci, jenkins."""
        r = build_registry()
        cases = {
            "gh secret delete FOO": "gh-actions-secret-remove",
            "gh variable delete FOO": "gh-actions-variable-remove",
            "gh workflow disable ci.yml": "gh-actions-workflow-disable",
            "gh run cancel 12345": "gh-actions-run-cancel",
            "gh run delete 12345": "gh-actions-run-delete",
            "gh api -X DELETE repos/o/r/actions/secrets/FOO": "gh-actions-api-delete-secrets",
            "glab variable delete FOO": "glab-variable-delete",
            "glab ci delete 123": "glab-ci-delete",
            "glab api -X DELETE projects/1/variables/FOO": "glab-api-delete-variables",
            "gitlab-runner unregister --all-runners": "gitlab-runner-unregister",
            "circleci context delete github org ctx": "circleci-context-delete",
            "circleci context remove-secret github org ctx SECRET": "circleci-context-remove-secret",
            "circleci orb delete org/orb@1.0.0": "circleci-orb-delete",
            "circleci namespace delete org": "circleci-namespace-delete",
            "circleci pipeline delete 123": "circleci-pipeline-delete",
            "curl -X DELETE https://circleci.com/api/v2/project/gh/org/repo/envvar/FOO": "circleci-api-delete-envvar",
            "jenkins-cli delete-job myjob": "jenkins-cli-delete-job",
            "jenkins-cli delete-node node1": "jenkins-cli-delete-node",
            "java -jar jenkins-cli.jar delete-credentials store cred": "jenkins-cli-delete-credentials",
            "jenkins-cli delete-builds myjob 1-10": "jenkins-cli-delete-builds",
            "jenkins-cli delete-view myview": "jenkins-cli-delete-view",
            "curl -X POST http://localhost:8080/job/myjob/doDelete": "jenkins-curl-do-delete",
        }
        for cmd, expected in cases.items():
            matches = r.evaluate(cmd)
            names = {m.pattern_name for m in matches}
            assert expected in names, f"{cmd!r}: expected {expected}, got {names}"

    def test_cicd_pack_reads_not_blocked(self):
        """Read/list operations on CI/CD tools must NOT be blocked."""
        r = build_registry()
        for cmd in (
            "gh secret list",
            "gh variable list",
            "gh workflow list",
            "gh run list",
            "gh run view 12345",
            "gh api repos/o/r/actions/secrets",
            "glab variable list",
            "glab ci list",
            "gitlab-runner list",
            "gitlab-runner verify",
            "circleci context list gh org",
            "circleci context show gh org ctx",
            "circleci orb list org",
            "jenkins-cli list-jobs",
            "jenkins-cli list-nodes",
            "jenkins-cli list-views",
            "curl http://localhost:8080/job/myjob/api/json",
        ):
            matches = r.evaluate(cmd)
            assert len(matches) == 0, f"False positive for {cmd!r}: {[m.pattern_name for m in matches]}"
