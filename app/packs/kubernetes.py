from __future__ import annotations

from app.command_policy import DestructivePattern, PatternSuggestion, Severity, SuggestionKind
from app.packs import Pack

KUBERNETES_PATTERNS: tuple[DestructivePattern, ...] = (
DestructivePattern(
        name="kubectl-delete-namespace",
        regex=r"kubectl\b.*?\bdelete\s+(?:namespace|ns)\b",
        reason="kubectl delete namespace removes the entire namespace and ALL resources within it",
        severity=Severity.CRITICAL,
        description="Deleting a namespace destroys EVERYTHING inside it:\n\n"
        "- All deployments, pods, services\n"
        "- All configmaps and secrets\n"
        "- All persistent volume claims (data may be lost)\n"
        "- All ingresses and network policies\n\n"
        "This is irreversible.",
        suggestions=(
            PatternSuggestion(
                "kubectl delete ns {ns} --dry-run=client -o yaml",
                "Preview what would be deleted without making changes",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "kubectl get all -n {ns}",
                "See all resources in the namespace before deleting",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "kubectl delete ns {ns} --grace-period=60",
                "Allow graceful shutdown with 60-second grace period",
            kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="kubectl-delete-all",
        regex=r"kubectl\b.*?\bdelete\s+.*--all\b",
        reason="kubectl delete --all removes ALL resources of that type",
        severity=Severity.HIGH,
        description="The --all flag deletes EVERY resource of the specified type. "
        "kubectl delete pods --all kills all pods. "
        "kubectl delete pvc --all may delete all persistent data.",
        suggestions=(
            PatternSuggestion(
                "kubectl delete {resource} --all --dry-run=client",
                "Preview what would be deleted without making changes",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "kubectl rollout restart deployment/{name}",
                "Restart pods via deployment for graceful recreation",
            kind=SuggestionKind.SAFER_ALTERNATIVE),
            PatternSuggestion(
                "kubectl delete {resource} {specific-name}",
                "Delete a specific resource instead of all",
            kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="kubectl-delete-all-namespaces",
        regex=r"kubectl\b.*?\bdelete\s+.*(?:-A\b|--all-namespaces)",
        reason="kubectl delete with -A/--all-namespaces affects ALL namespaces — very dangerous",
        severity=Severity.CRITICAL,
        description="The -A/--all-namespaces flag expands deletion to EVERY namespace "
        "including system namespaces (kube-system). This can take down your entire cluster.",
        suggestions=(
            PatternSuggestion(
                "kubectl delete {resource} -n {namespace}",
                "Always specify a namespace explicitly",
            kind=SuggestionKind.SAFER_ALTERNATIVE),
            PatternSuggestion(
                "kubectl get {resource} -A",
                "Preview cluster-wide resources before making changes",
            kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="kubectl-drain-node",
        regex=r"kubectl\b.*?\bdrain\b",
        reason="kubectl drain evicts all pods from a node — can cause service disruption",
        severity=Severity.HIGH,
        description="kubectl drain evicts ALL pods from a node. "
        "Use PodDisruptionBudgets to protect critical workloads. "
        "DaemonSet pods remain unless --ignore-daemonsets is used.",
        suggestions=(
            PatternSuggestion(
                "kubectl get pods -o wide | grep {node}",
                "Check what's running on the node before draining",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "kubectl get pdb -A",
                "Check disruption budgets before eviction",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "kubectl cordon {node}",
                "Cordon first: prevent new pods, then drain gradually",
            kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="kubectl-cordon-node",
        regex=r"kubectl\b.*?\bcordon\b",
        reason="kubectl cordon marks a node unschedulable. Existing pods continue running.",
        severity=Severity.MEDIUM,
        description="kubectl cordon marks a node as unschedulable. "
        "Existing pods continue running but no new pods will be scheduled.",
        suggestions=(
            PatternSuggestion(
                "kubectl uncordon {node}",
                "To reverse: uncordon the node",
            kind=SuggestionKind.SAFER_ALTERNATIVE),
            PatternSuggestion(
                "kubectl describe node {node} | grep Taints",
                "Check node status and taints",
            kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="kubectl-taint-noexecute",
        regex=r"kubectl\b.*?\btaint\s+.*:NoExecute\b",
        reason="kubectl taint with NoExecute evicts existing pods without toleration",
        severity=Severity.HIGH,
        description="A NoExecute taint immediately evicts pods that don't have a matching "
        "toleration. More aggressive than NoSchedule — existing pods are evicted.",
        suggestions=(
            PatternSuggestion(
                "kubectl describe node {node} | grep Taints",
                "Check current taints before modifying",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "kubectl taint nodes {node} key=value:NoSchedule",
                "Consider NoSchedule first (only blocks new pods)",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "kubectl taint nodes {node} key=value:NoExecute-",
                "Remove a NoExecute taint",
            kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="kubectl-delete-workload",
        regex=r"kubectl\b.*?\bdelete\s+(?:deployment|statefulset|daemonset|replicaset)\b",
        reason="kubectl delete deployment/statefulset/daemonset removes the workload and all pods",
        severity=Severity.HIGH,
        description="Deleting a workload terminates all its pods. "
        "Consider scaling down first for controlled shutdown.",
        suggestions=(
            PatternSuggestion(
                "kubectl delete {type} {name} --dry-run=client",
                "Preview without making changes",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "kubectl get pods -l app={name}",
                "Check affected pods before deleting",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "kubectl scale deployment {name} --replicas=0",
                "Scale to zero first for controlled shutdown",
            kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="kubectl-delete-pvc",
        regex=r"kubectl\b.*?\bdelete\s+(?:pvc|persistentvolumeclaim)\b",
        reason="kubectl delete pvc may permanently delete data (depends on ReclaimPolicy)",
        severity=Severity.CRITICAL,
        description="Deleting a PVC can cause permanent data loss. "
        "Check the PV's reclaimPolicy: Delete → data lost, Retain → manual recovery.",
        suggestions=(
            PatternSuggestion(
                "kubectl describe pvc {name}",
                "Check PVC status and usage before deleting",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "kubectl get pv $(kubectl get pvc {name} -o jsonpath='{.spec.volumeName}')",
                "Check the reclaim policy of the backing PV",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "kubectl delete pvc {name} --dry-run=client",
                "Preview deletion without making changes",
            kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="kubectl-delete-pv",
        regex=r"kubectl\b.*?\bdelete\s+(?:pv|persistentvolume)\b",
        reason="kubectl delete pv may permanently delete the underlying storage",
        severity=Severity.CRITICAL,
        description="Deleting a PersistentVolume can destroy the underlying storage: "
        "cloud disks (EBS, GCE PD) may be deleted, NFS mounts orphaned.",
        suggestions=(
            PatternSuggestion(
                "kubectl get pvc -A | grep {pv-name}",
                "Check what PVCs use this PV",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "kubectl get storageclass {class} -o yaml",
                "Check storage class reclaim policy",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "kubectl delete pv {name} --dry-run=client",
                "Preview without making changes",
            kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="kubectl-scale-to-zero",
        regex=r"kubectl\b.*?\bscale\s+.*--replicas=0\b",
        reason="kubectl scale --replicas=0 stops ALL pods for the workload",
        severity=Severity.HIGH,
        description="Scaling to zero terminates ALL pods. "
        "Service becomes unavailable, in-flight requests dropped.",
        suggestions=(
            PatternSuggestion(
                "kubectl get deployment {name} -o jsonpath='{.spec.replicas}'",
                "Check current replica count before scaling",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "kubectl scale deployment {name} --replicas={N}",
                "Scale to a non-zero value to restore service",
            kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="kubectl-delete-force",
        regex=r"kubectl\b.*?\bdelete\s+.*--force.*--grace-period=0|kubectl\b.*?\bdelete\s+.*--grace-period=0.*--force",
        reason="kubectl delete --force --grace-period=0 immediately removes resources "
        "without graceful shutdown",
        severity=Severity.CRITICAL,
        description="Force deletion with zero grace period kills pods immediately "
        "(no SIGTERM). In-flight requests fail, finalizers may be skipped.",
        suggestions=(
            PatternSuggestion(
                "kubectl delete pod {name}",
                "Use default 30-second grace period for graceful shutdown",
            kind=SuggestionKind.SAFER_ALTERNATIVE),
            PatternSuggestion(
                "kubectl describe pod {name} | grep -A5 Status",
                "Check why pod is stuck before force-deleting",
            kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
    DestructivePattern(
        name="kubectl-apply-force",
        regex=r"kubectl\b.*?\bapply\s+.*--force\b",
        reason="kubectl apply --force deletes and recreates resources, causing downtime",
        severity=Severity.HIGH,
        description="kubectl apply --force deletes the resource and recreates it. "
        "Causes downtime and potential data loss for stateful workloads.",
        suggestions=(
            PatternSuggestion(
                "kubectl diff -f {file}",
                "Preview what changes would be applied",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "kubectl apply --server-side -f {file}",
                "Use server-side apply for safer updates",
            kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
    # kubectl delete -f - (stdin),
    DestructivePattern(
        name="kubectl-delete-from-stdin",
        regex=r"kubectl\b.*?\bdelete\b.*?(?:-f(?:=|\s+)?|--filename(?:=|\s+))"
        r"""["']?(?:[^,"'\s]+,)*-(?:,[^,"'\s]+)*["']?(?=\s|$)""",
        reason="kubectl delete -f - deletes resources piped from stdin "
        "without a reviewable manifest path",
        severity=Severity.HIGH,
        description="Deleting from stdin means the manifest isn't reviewable. "
        "Materialize the manifest first and use --dry-run=client.",
        suggestions=(
            PatternSuggestion(
                "kustomize build {dir} > /tmp/manifest.yaml",
                "Save manifest to a file for review first",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "kubectl diff -f /tmp/manifest.yaml",
                "Preview what will change before deleting",
            kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="kubectl-delete-from-directory",
        regex=r"kubectl\b.*?\bdelete\s+-f\s+\.\s*$|kubectl\b.*?\bdelete\s+-f\s+\./|"
        r"kubectl\b.*?\bdelete\s+--recursive\s+-f|kubectl\b.*?\bdelete\s+-f.*--recursive",
        reason="kubectl delete -f with directories or --recursive "
        "deletes many resources at once",
        severity=Severity.HIGH,
        description="Deleting from a directory removes ALL resources defined in those files. "
        "Multiple deployments, services, configmaps deleted at once.",
        suggestions=(
            PatternSuggestion(
                "ls -la {dir}/*.yaml",
                "List files in the directory before deleting",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "kubectl diff -f {dir}",
                "Preview what would change",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "kubectl delete -f {specific-file.yaml}",
                "Delete specific files instead of entire directory",
            kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
DestructivePattern(
        name="helm-uninstall",
        regex=r"helm\b.*?\b(?:uninstall|delete)\b",
        reason="helm uninstall removes the release and ALL its Kubernetes resources",
        severity=Severity.CRITICAL,
        description="helm uninstall deletes all resources created by the chart: "
        "deployments, services, configmaps, secrets, PVCs. Use --dry-run first.",
        suggestions=(
            PatternSuggestion(
                "helm uninstall {release} --dry-run",
                "Preview what will be deleted",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "helm status {release}",
                "Review current release state before deleting",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "helm get manifest {release}",
                "See all resources managed by the release",
            kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="helm-rollback",
        regex=r"helm\b.*?\brollback\b",
        reason="helm rollback reverts to a previous release — can cause unexpected changes",
        severity=Severity.HIGH,
        description="helm rollback reverts to a previous revision. "
        "Database migrations are NOT automatically undone. "
        "Use --dry-run to preview changes.",
        suggestions=(
            PatternSuggestion(
                "helm rollback {release} {revision} --dry-run",
                "Preview changes before rolling back",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "helm history {release}",
                "Review available revisions before rolling back",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "helm diff rollback {release} {revision}",
                "Compare changes before rolling back (requires diff plugin)",
            kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="helm-upgrade-force",
        regex=r"helm\b.*?\bupgrade\s+.*--force\b",
        reason="helm upgrade --force deletes and recreates resources, causing downtime",
        severity=Severity.HIGH,
        description="The --force flag causes Helm to delete and recreate resources "
        "instead of updating them in place. Pods are terminated and recreated.",
        suggestions=(
            PatternSuggestion(
                "helm upgrade {release} {chart}",
                "Remove --force to use rolling updates",
            kind=SuggestionKind.SAFER_ALTERNATIVE),
            PatternSuggestion(
                "helm upgrade --dry-run --debug",
                "Preview changes before upgrading",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "helm diff upgrade {release} {chart}",
                "Compare before upgrading (requires diff plugin)",
            kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="helm-upgrade-reset-values",
        regex=r"helm\b.*?\bupgrade\s+.*--reset-values\b",
        reason="helm upgrade --reset-values discards all previously set values",
        severity=Severity.HIGH,
        description="The --reset-values flag discards all values from previous releases. "
        "Resource limits, replica counts, connection strings may change unexpectedly.",
        suggestions=(
            PatternSuggestion(
                "helm get values {release}",
                "Review current values before upgrading",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "helm upgrade --reuse-values",
                "Keep existing values",
            kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="kustomize-build-delete",
        regex=r"kustomize\b.*?\bbuild\s+.*\|\s*kubectl\b.*?\bdelete\b",
        reason="kustomize build | kubectl delete removes ALL resources in the kustomization",
        severity=Severity.CRITICAL,
        description="Piping kustomize build to kubectl delete removes ALL resources. "
        "Use --dry-run=client first.",
        suggestions=(
            PatternSuggestion(
                "kustomize build {dir} > /tmp/manifest.yaml",
                "Save and review manifests before deleting",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "kustomize build {dir} | kubectl delete --dry-run=client -f -",
                "Preview with dry-run first",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "kustomize build {dir} | kubectl diff -f -",
                "Compare with cluster state before deleting",
            kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="kubectl-kustomize-delete",
        regex=r"kubectl\b.*?\bkustomize\s+.*\|\s*kubectl\b.*?\bdelete\b",
        reason="kubectl kustomize | kubectl delete removes ALL resources in the kustomization",
        severity=Severity.CRITICAL,
        description="Piping kubectl kustomize to kubectl delete removes ALL resources. "
        "Equivalent to kustomize build | kubectl delete.",
        suggestions=(
            PatternSuggestion(
                "kubectl kustomize {dir}",
                "Review manifests first",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "kubectl delete --dry-run=client -k {dir}",
                "Preview deletion with dry-run",
            kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
DestructivePattern(
        name="kubectl-delete-k",
        regex=r"kubectl\b.*?\bdelete\s+-k\b",
        reason="kubectl delete -k removes all resources defined in the kustomization",
        severity=Severity.CRITICAL,
        description="kubectl delete -k removes all resources in a kustomization directory. "
        "Use --dry-run=client first to preview.",
        suggestions=(
            PatternSuggestion(
                "kubectl delete -k {dir} --dry-run=client",
                "Preview what will be deleted",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "kubectl kustomize {dir}",
                "Review manifests before deleting",
            kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(
                "kubectl get -k {dir}",
                "List resources that would be affected",
            kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
)

def build_kubernetes_pack() -> Pack:
    return Pack(id="kubernetes", name="Kubernetes patterns",
        destructive_patterns=KUBERNETES_PATTERNS,
        keywords=("kubectl", "helm", "kustomize"),
    )
