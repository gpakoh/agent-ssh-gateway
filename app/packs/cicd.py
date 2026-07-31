from __future__ import annotations

from app.command_policy import DestructivePattern, PatternSuggestion, Severity, SuggestionKind
from app.packs import Pack

CICD_PATTERNS: tuple[DestructivePattern, ...] = (
DestructivePattern(
        name="gh-actions-secret-remove",
        regex=r"gh(?:\s+--?[A-Za-z][A-Za-z0-9-]*\b(?:\s+(?!(?:secret|variable|workflow|run|api)\b)\S+)?)*\s+secret\s+(?:delete|remove)\b",
        reason="gh secret delete/remove deletes GitHub Actions secrets",
        severity=Severity.HIGH,
        description="Deletes a GitHub Actions secret. Workflows depending on it fail "
        "at runtime, and the secret value is permanently lost.",
        suggestions=(
            PatternSuggestion(command="gh secret list", description="Review configured secrets first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="gh secret set {NAME} --repo {owner}/{repo}", description="Recreate the secret with a fresh value", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="gh-actions-variable-remove",
        regex=r"gh(?:\s+--?[A-Za-z][A-Za-z0-9-]*\b(?:\s+(?!(?:secret|variable|workflow|run|api)\b)\S+)?)*\s+variable\s+(?:delete|remove)\b",
        reason="gh variable delete/remove deletes GitHub Actions variables",
        severity=Severity.MEDIUM,
        description="Deletes a GitHub Actions configuration variable. Workflows using "
        "it break or behave differently until it is recreated.",
        suggestions=(
            PatternSuggestion(command="gh variable list", description="Review configured variables first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="gh variable set {NAME} --body {value} --repo {owner}/{repo}", description="Recreate the variable with the correct value", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="gh-actions-workflow-disable",
        regex=r"gh(?:\s+--?[A-Za-z][A-Za-z0-9-]*\b(?:\s+(?!(?:secret|variable|workflow|run|api)\b)\S+)?)*\s+workflow\s+disable\b",
        reason="gh workflow disable disables workflows",
        severity=Severity.LOW,
        description="Disables a workflow — it stops running on any trigger. Reversible "
        "with gh workflow enable, but can halt deployments while disabled.",
        suggestions=(
            PatternSuggestion(command="gh workflow list", description="Review workflow status first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="gh workflow enable {WORKFLOW}", description="Re-enable the workflow when ready", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="gh-actions-run-cancel",
        regex=r"gh(?:\s+--?[A-Za-z][A-Za-z0-9-]*\b(?:\s+(?!(?:secret|variable|workflow|run|api)\b)\S+)?)*\s+run\s+cancel\b",
        reason="gh run cancel cancels a running workflow",
        severity=Severity.LOW,
        description="Cancels a workflow run mid-execution. In-progress deployments, "
        "tests or builds are interrupted; partial work may leave systems in an "
        "inconsistent state.",
        suggestions=(
            PatternSuggestion(command="gh run view {RUN_ID}", description="Check run status and progress first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="gh run list", description="Review running workflows first", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="gh-actions-run-delete",
        regex=r"gh(?:\s+--?[A-Za-z][A-Za-z0-9-]*\b(?:\s+(?!(?:secret|variable|workflow|run|api)\b)\S+)?)*\s+run\s+delete\b",
        reason="gh run delete permanently removes workflow run logs and artifacts",
        severity=Severity.MEDIUM,
        description="Deletes a workflow run and its logs. Audit trail for that run is "
        "permanently lost, making post-incident analysis impossible.",
        suggestions=(
            PatternSuggestion(command="gh run view --log {RUN_ID} > run.log", description="Save the run logs before deleting", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="gh run list", description="Confirm the run ID before deletion", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="gh-actions-api-delete-secrets",
        regex=r"gh(?:\s+--?[A-Za-z][A-Za-z0-9-]*\b(?:\s+(?!(?:secret|variable|workflow|run|api)\b)\S+)?)*\s+api\b.*(?:-X\s*|--method(?:=|\s+))DELETE\b.*\b/?repos/[^\s/]+/[^\s/]+/actions/secrets\b",
        reason="gh api DELETE against /actions/secrets deletes GitHub Actions secrets",
        severity=Severity.HIGH,
        description="Deletes a GitHub Actions secret via the REST API. Workflows "
        "depending on it fail, and the secret value is permanently lost.",
        suggestions=(
            PatternSuggestion(command="gh api repos/{owner}/{repo}/actions/secrets", description="List secrets before deletion", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="gh secret set {NAME} --repo {owner}/{repo}", description="Recreate the secret with a fresh value", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="gh-actions-api-delete-variables",
        regex=r"gh(?:\s+--?[A-Za-z][A-Za-z0-9-]*\b(?:\s+(?!(?:secret|variable|workflow|run|api)\b)\S+)?)*\s+api\b.*(?:-X\s*|--method(?:=|\s+))DELETE\b.*\b/?repos/[^\s/]+/[^\s/]+/actions/variables\b",
        reason="gh api DELETE against /actions/variables deletes GitHub Actions variables",
        severity=Severity.MEDIUM,
        description="Deletes a GitHub Actions variable via the REST API. Workflows "
        "using it break or behave differently until recreated.",
        suggestions=(
            PatternSuggestion(command="gh api repos/{owner}/{repo}/actions/variables", description="List variables before deletion", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="gh variable set {NAME} --body {value} --repo {owner}/{repo}", description="Recreate the variable with the correct value", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="glab-variable-delete",
        regex=r"glab(?:\s+--?\S+(?:\s+\S+)?)*\s+variable\s+delete\b",
        reason="glab variable delete removes GitLab CI variables",
        severity=Severity.HIGH,
        description="Deletes a GitLab CI/CD variable. Pipelines relying on it fail, "
        "and the masked value cannot be recovered.",
        suggestions=(
            PatternSuggestion(command="glab variable list", description="Review CI variables first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="glab variable set {KEY} --value {VALUE}", description="Recreate the variable with a new value", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="glab-ci-delete",
        regex=r"glab(?:\s+--?\S+(?:\s+\S+)?)*\s+ci\s+delete\b",
        reason="glab ci delete removes pipeline artifacts or pipelines",
        severity=Severity.MEDIUM,
        description="Deletes pipeline artifacts or pipeline records. Audit trail and "
        "artifacts are permanently lost.",
        suggestions=(
            PatternSuggestion(command="glab ci view {PIPELINE_ID}", description="Review pipeline status before deletion", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="glab ci list", description="Confirm the pipeline ID first", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="glab-api-delete-variables",
        regex=r"glab(?:\s+--?\S+(?:\s+\S+)?)*\s+api\b.*(?:-X\s*|--method(?:=|\s+))DELETE\b.*\bvariables\b",
        reason="glab api DELETE against variables endpoints removes CI variables",
        severity=Severity.HIGH,
        description="Deletes GitLab CI/CD variables via the REST API. Pipelines "
        "depending on them fail; values cannot be recovered.",
        suggestions=(
            PatternSuggestion(command="glab api projects/:id/variables", description="List variables before deletion", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="glab variable set {KEY} --value {VALUE}", description="Recreate the variable with a new value", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="gitlab-runner-unregister",
        regex=r"gitlab-runner(?:\s+--?\S+(?:\s+\S+)?)*\s+unregister\b",
        reason="gitlab-runner unregister removes runners and can halt CI",
        severity=Severity.CRITICAL,
        description="Unregisters a GitLab runner — it stops picking up jobs. Queued "
        "pipelines wait indefinitely and CI comes to a halt.",
        suggestions=(
            PatternSuggestion(command="gitlab-runner list", description="Review registered runners first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="gitlab-runner verify", description="Check runner connectivity before unregistering", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="circleci-context-delete",
        regex=r"circleci(?:\s+--?\S+(?:\s+\S+)?)*\s+context\s+delete\b",
        reason="circleci context delete removes contexts and their secrets",
        severity=Severity.CRITICAL,
        description="Deletes a CircleCI context with ALL secrets stored in it. Every "
        "project using that context breaks immediately.",
        suggestions=(
            PatternSuggestion(command="circleci context list {VCS_TYPE} {ORG_NAME}", description="Review contexts before deletion", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="circleci context show {VCS_TYPE} {ORG_NAME} {CONTEXT}", description="Inspect context secrets before deletion", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="circleci-context-remove-secret",
        regex=r"circleci(?:\s+--?\S+(?:\s+\S+)?)*\s+context\s+remove-secret\b",
        reason="circleci context remove-secret deletes secrets from a context",
        severity=Severity.HIGH,
        description="Deletes a single secret from a CircleCI context. Projects using "
        "it fail until a new value is stored.",
        suggestions=(
            PatternSuggestion(command="circleci context show {VCS_TYPE} {ORG_NAME} {CONTEXT}", description="Inspect context secrets before removal", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="circleci context store-secret {VCS_TYPE} {ORG_NAME} {CONTEXT} {VAR_NAME}", description="Store a new value before removal", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="circleci-orb-delete",
        regex=r"circleci(?:\s+--?\S+(?:\s+\S+)?)*\s+orb\s+delete\b",
        reason="circleci orb delete removes an orb from the registry",
        severity=Severity.HIGH,
        description="Deletes an orb version from the CircleCI registry. Every pipeline "
        "referencing that version fails to resolve.",
        suggestions=(
            PatternSuggestion(command="circleci orb list {ORG_NAME}", description="Review orbs before deletion", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="circleci orb publish {ORB_FILE} {ORG_NAME}/{ORB}@{VERSION}", description="Re-publish the orb after verifying", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="circleci-namespace-delete",
        regex=r"circleci(?:\s+--?\S+(?:\s+\S+)?)*\s+namespace\s+delete\b",
        reason="circleci namespace delete removes an orb namespace",
        severity=Severity.CRITICAL,
        description="Deletes a CircleCI orb namespace — all orbs published under it "
        "become unavailable and existing pipelines break.",
        suggestions=(
            PatternSuggestion(command="circleci orb list {ORG_NAME}", description="Review orbs in the namespace first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="circleci namespace list", description="Confirm the namespace before deletion", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="circleci-pipeline-delete",
        regex=r"circleci(?:\s+--?\S+(?:\s+\S+)?)*\s+pipeline\s+delete\b",
        reason="circleci pipeline delete removes pipeline history",
        severity=Severity.MEDIUM,
        description="Deletes pipeline records and their history. Audit trail for "
        "those pipelines is permanently lost.",
        suggestions=(
            PatternSuggestion(command="circleci pipeline list {ORG_NAME}", description="Review pipelines before deletion", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="circleci pipeline show {PIPELINE_ID}", description="Confirm the pipeline before deletion", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="circleci-api-delete-envvar",
        regex=r"curl(?:\s+--?\S+(?:\s+\S+)?)*\s+(?:-X\s*|--request(?:=|\s+))DELETE\b.*circleci\.com/api/[^\s]*\b(?:envvar|environment-variable)\b",
        reason="curl DELETE against CircleCI envvar endpoints removes environment variables",
        severity=Severity.HIGH,
        description="Deletes a CircleCI project environment variable via the REST API. "
        "Jobs using it fail; the value cannot be recovered.",
        suggestions=(
            PatternSuggestion(command="curl 'https://circleci.com/api/v2/project/{vcs}/{org}/{repo}/envvar'", description="List environment variables first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="curl -X POST 'https://circleci.com/api/v2/project/{vcs}/{org}/{repo}/envvar' -H 'Circle-Token: {token}' -d '{\"name\":\"{NAME}\",\"value\":\"{VALUE}\"}'", description="Recreate the variable after verifying", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="jenkins-cli-delete-job",
        regex=r"(?:jenkins-cli|java\s+-jar\s+\S*jenkins-cli\.jar)(?:\s+--?\S+(?:\s+\S+)?)*\s+delete-job\b",
        reason="jenkins-cli delete-job deletes Jenkins jobs and can break pipelines",
        severity=Severity.CRITICAL,
        description="Deletes a Jenkins job and its build history, logs and artifacts. "
        "Any pipeline or job referencing it breaks immediately.",
        suggestions=(
            PatternSuggestion(command="jenkins-cli list-jobs", description="Review jobs before deletion", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="curl 'http://{host}:8080/job/{job}/config.xml' > job.xml", description="Export the job config before deleting", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="jenkins-cli-delete-node",
        regex=r"(?:jenkins-cli|java\s+-jar\s+\S*jenkins-cli\.jar)(?:\s+--?\S+(?:\s+\S+)?)*\s+delete-node\b",
        reason="jenkins-cli delete-node deletes Jenkins nodes and can halt CI",
        severity=Severity.HIGH,
        description="Deletes a Jenkins build node. Jobs assigned to it stop running; "
        "overall CI capacity drops.",
        suggestions=(
            PatternSuggestion(command="jenkins-cli list-nodes", description="Review nodes before deletion", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="jenkins-cli offline-node {NODE}", description="Take the node offline first to drain it", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="jenkins-cli-delete-credentials",
        regex=r"(?:jenkins-cli|java\s+-jar\s+\S*jenkins-cli\.jar)(?:\s+--?\S+(?:\s+\S+)?)*\s+delete-credentials\b",
        reason="jenkins-cli delete-credentials removes stored credentials",
        severity=Severity.HIGH,
        description="Deletes credentials from the Jenkins store. Jobs and pipelines "
        "using them fail authentication; values cannot be recovered.",
        suggestions=(
            PatternSuggestion(command="jenkins-cli list-credentials {STORE}", description="Review credential metadata first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="jenkins-cli create-credentials {STORE} --username {USER} --password {PASS}", description="Recreate the credential after verifying", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="jenkins-cli-delete-builds",
        regex=r"(?:jenkins-cli|java\s+-jar\s+\S*jenkins-cli\.jar)(?:\s+--?\S+(?:\s+\S+)?)*\s+delete-builds\b",
        reason="jenkins-cli delete-builds removes build history and artifacts",
        severity=Severity.MEDIUM,
        description="Deletes build records, console logs and artifacts. Audit trail "
        "and debugging capability are permanently lost.",
        suggestions=(
            PatternSuggestion(command="jenkins-cli list-builds {JOB}", description="Review job builds first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="curl 'http://{host}:8080/job/{job}/{build}/artifact' -O", description="Download artifacts before deletion", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="jenkins-cli-delete-view",
        regex=r"(?:jenkins-cli|java\s+-jar\s+\S*jenkins-cli\.jar)(?:\s+--?\S+(?:\s+\S+)?)*\s+delete-view\b",
        reason="jenkins-cli delete-view removes Jenkins views",
        severity=Severity.LOW,
        description="Deletes a Jenkins view configuration. Jobs are not deleted, but "
        "the organizational structure is lost.",
        suggestions=(
            PatternSuggestion(command="jenkins-cli list-views", description="Review views before deletion", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="curl 'http://{host}:8080/view/{view}/config.xml' > view.xml", description="Export the view config before deleting", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="jenkins-curl-do-delete",
        regex=r"(?i)\bcurl\b(?=.*(?:-X\s*|--request(?:=|\s+))POST\b)(?=.*\bdoDelete\b).*",
        reason="curl POST to Jenkins doDelete endpoints deletes jobs or resources",
        severity=Severity.CRITICAL,
        description="POSTing to Jenkins doDelete endpoints triggers immediate deletion "
        "of jobs, builds or other resources, bypassing CLI safety checks.",
        suggestions=(
            PatternSuggestion(command="curl 'http://{host}:8080/job/{job}/api/json'", description="Review the job before deletion", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="curl 'http://{host}:8080/job/{job}/config.xml' > job.xml", description="Export the job config before deleting", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
)


def build_cicd_pack() -> Pack:
    return Pack(
        id="cicd",
        name="CI/CD",
        destructive_patterns=CICD_PATTERNS,
        keywords=("gh ", "glab", "gitlab-runner", "circleci", "jenkins-cli", "doDelete", "actions/secrets", "actions/variables"),
    )
