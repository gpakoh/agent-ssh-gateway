from __future__ import annotations

from app.command_policy import DestructivePattern, PatternSuggestion, Severity
from app.packs import Pack

# Every DestructivePattern verbatim from command_policy.py's CLOUD_DESTRUCTIVE_PATTERNS
# AWS (19) + GCP (16) + Azure (15) = 50 patterns

CLOUD_PATTERNS: tuple[DestructivePattern, ...] = (
    # ---- AWS CLI ----
    DestructivePattern(
        name="aws-ec2-terminate",
        regex=r"aws\b.*?\bec2\s+terminate-instances",
        reason="aws ec2 terminate-instances permanently destroys EC2 instances",
        severity=Severity.CRITICAL,
        description="Instance is stopped and deleted. EBS volumes and Elastic IPs are lost.",
        suggestions=(
            PatternSuggestion("aws ec2 stop-instances --instance-ids i-xxx",
                              "Stop instead of terminate for recoverable pause"),
            PatternSuggestion("aws ec2 describe-instances --instance-ids i-xxx",
                              "Verify instance details before terminating"),
        ),
    ),
    DestructivePattern(
        name="aws-ec2-delete",
        regex=r"aws\b.*?\bec2\s+delete-",
        reason="aws ec2 delete-* permanently removes EC2 resources (snapshots, volumes, VPCs, AMIs)",
        severity=Severity.HIGH,
        description="EC2 delete commands: delete-snapshot, delete-volume, delete-vpc, delete-image.",
        suggestions=(
            PatternSuggestion(command="aws ec2 describe-snapshots/volumes/vpcs/images", description="List resources before deletion"),
            PatternSuggestion(command="aws ec2 create-snapshot --volume-id {vol}", description="Backup volume before deleting"),
        ),
    ),
    DestructivePattern(
        name="aws-s3-rm-recursive",
        regex=r"aws\b.*?\bs3\s+rm\s+.*--recursive",
        reason="aws s3 rm --recursive permanently deletes ALL objects in the S3 path",
        severity=Severity.CRITICAL,
        description="Recursive deletion of all objects under the prefix. "
        "No recovery unless bucket versioning is enabled.",
        suggestions=(
            PatternSuggestion("aws s3 rm s3://bucket/path/ --recursive --dryrun",
                              "Preview deletions with --dryrun"),
            PatternSuggestion("aws s3 ls s3://bucket/path/ --recursive",
                              "List objects to verify before deleting"),
        ),
    ),
    DestructivePattern(
        name="aws-s3-rb",
        regex=r"aws\b.*?\bs3\s+rb\b",
        reason="aws s3 rb removes the entire S3 bucket",
        severity=Severity.CRITICAL,
        description="rb removes an S3 bucket. With --force, deletes all contents first.",
        suggestions=(
            PatternSuggestion(command="aws s3 ls s3://{bucket}", description="List bucket contents first"),
            PatternSuggestion(command="aws s3api get-bucket-versioning --bucket {bucket}", description="Check versioning status first"),
        ),
    ),
    DestructivePattern(
        name="aws-s3api-delete-bucket",
        regex=r"aws\b.*?\bs3api\s+delete-bucket",
        reason="aws s3api delete-bucket removes the S3 bucket",
        severity=Severity.CRITICAL,
        description="Bucket must be empty. Check contents before deleting.",
        suggestions=(
            PatternSuggestion(command="aws s3api list-objects --bucket {bucket} --max-items 10", description="List objects before deleting"),
            PatternSuggestion(command="aws s3api get-bucket-versioning --bucket {bucket}", description="Check versioning status first"),
        ),
    ),
    DestructivePattern(
        name="aws-s3api-delete-object",
        regex=r"aws\b.*?\bs3api\s+delete-object",
        reason="aws s3api delete-object[s] permanently removes objects unless versioning is enabled",
        severity=Severity.HIGH,
        description="delete-objects is BATCH (up to 1000 keys). "
        "Without versioning, objects are permanently gone.",
        suggestions=(
            PatternSuggestion("aws s3api get-bucket-versioning --bucket xxx",
                              "Check if versioning is enabled first"),
        ),
    ),
    DestructivePattern(
        name="aws-rds-delete",
        regex=r"aws\b.*?\brds\s+delete-",
        reason="aws rds delete-* destroys database resources (instance, cluster, snapshot)",
        severity=Severity.CRITICAL,
        description="RDS delete commands remove database instances, clusters, and snapshots. "
        "Create a final snapshot before deletion.",
        suggestions=(
            PatternSuggestion(command="aws rds describe-db-instances", description="List instances before deletion"),
            PatternSuggestion(command="aws rds delete-db-instance --skip-final-snapshot false", description="Create final snapshot before deletion"),
        ),
    ),
    DestructivePattern(
        name="aws-cfn-delete-stack",
        regex=r"aws\b.*?\bcloudformation\s+delete-stack",
        reason="aws cloudformation delete-stack removes the stack and ALL resources it created",
        severity=Severity.CRITICAL,
        description="All EC2, RDS, S3, IAM resources created by the stack are deleted.",
        suggestions=(
            PatternSuggestion(command="aws cloudformation describe-stack-resources --stack-name {name}", description="List resources first"),
            PatternSuggestion(command="aws cloudformation get-template --stack-name {name}", description="Save template before deletion"),
        ),
    ),
    DestructivePattern(
        name="aws-lambda-delete",
        regex=r"aws\b.*?\blambda\s+delete-",
        reason="aws lambda delete-* removes Lambda function, alias, or layer version",
        severity=Severity.HIGH,
        description="Function code, versions, aliases, and event source mappings are removed.",
        suggestions=(
            PatternSuggestion(command="aws lambda list-functions", description="List functions before deletion"),
            PatternSuggestion(command="aws lambda get-function --function-name {name}", description="Save function config first"),
        ),
    ),
    DestructivePattern(
        name="aws-iam-delete",
        regex=r"aws\b.*?\biam\s+delete-",
        reason="aws iam delete-* removes IAM resources (user, role, policy, group)",
        severity=Severity.HIGH,
        description="IAM deletions break authentication for users and services using those resources.",
        suggestions=(
            PatternSuggestion(command="aws iam list-attached-role-policies --role-name {role}", description="Check attached policies first"),
            PatternSuggestion(command="aws iam get-policy --policy-arn {arn}", description="Save policy document before deletion"),
        ),
    ),
    DestructivePattern(
        name="aws-dynamodb-delete",
        regex=r"aws\b.*?\bdynamodb\s+delete-table",
        reason="aws dynamodb delete-table permanently deletes the table and ALL data",
        severity=Severity.CRITICAL,
        description="All items, indexes, and table configuration are lost.",
        suggestions=(
            PatternSuggestion(command="aws dynamodb list-tables", description="List tables first"),
            PatternSuggestion(command="aws dynamodb describe-table --table-name {name}", description="Check table details before deletion"),
        ),
    ),
    DestructivePattern(
        name="aws-eks-delete",
        regex=r"aws\b.*?\beks\s+delete-cluster",
        reason="aws eks delete-cluster removes the entire EKS cluster",
        severity=Severity.CRITICAL,
        description="Control plane is deleted. Node groups must be deleted separately first.",
        suggestions=(
            PatternSuggestion(command="aws eks list-nodegroups --cluster-name {name}", description="List nodegroups before deletion"),
            PatternSuggestion(command="aws eks describe-cluster --name {name}", description="Check cluster details first"),
        ),
    ),
    DestructivePattern(
        name="aws-ecr-delete-repository",
        regex=r"aws\b.*?\becr\s+delete-repository",
        reason="aws ecr delete-repository permanently deletes the repository and its images",
        severity=Severity.HIGH,
        description="All images in the repository are deleted.",
        suggestions=(
            PatternSuggestion(command="aws ecr list-images --repository-name {name}", description="List images first"),
            PatternSuggestion(command="aws ecr describe-repositories", description="List repositories before deletion"),
        ),
    ),
    DestructivePattern(
        name="aws-kms-schedule-key-deletion",
        regex=r"aws\b.*?\bkms\s+schedule-key-deletion",
        reason="aws kms schedule-key-deletion schedules irreversible KMS key destruction",
        severity=Severity.CRITICAL,
        description="Data encrypted with this key becomes permanently undecryptable. "
        "CancelKeyDeletion can abort within the waiting window.",
        suggestions=(
            PatternSuggestion("aws kms disable-key --key-id xxx",
                              "Disable key instead of deletion for reversible deactivation"),
        ),
    ),
    DestructivePattern(
        name="aws-secretsmanager-delete-secret",
        regex=r"aws\b.*?\bsecretsmanager\s+delete-secret",
        reason="aws secretsmanager delete-secret destroys a stored secret",
        severity=Severity.CRITICAL,
        description="30-day recovery window unless --force-delete-without-recovery used.",
        suggestions=(
            PatternSuggestion(command="aws secretsmanager describe-secret --secret-id {name}", description="Check secret details first"),
            PatternSuggestion(command="aws secretsmanager list-secrets", description="List secrets before deletion"),
        ),
    ),
    DestructivePattern(
        name="aws-route53-delete-hosted-zone",
        regex=r"aws\b.*?\broute53\s+delete-hosted-zone",
        reason="aws route53 delete-hosted-zone removes DNS zone — domains stop resolving",
        severity=Severity.CRITICAL,
        description="All DNS records deleted. Production traffic can become unroutable immediately.",
        suggestions=(
            PatternSuggestion("aws route53 list-resource-record-sets --hosted-zone-id xxx",
                              "Export records first"),
        ),
    ),
    DestructivePattern(
        name="aws-cloudtrail-delete-trail",
        regex=r"aws\b.*?\bcloudtrail\s+delete-trail",
        reason="aws cloudtrail delete-trail removes audit trail — compliance/forensics impact",
        severity=Severity.CRITICAL,
        description="Historical logs in S3 are preserved, but future events stop being recorded.",
        suggestions=(
            PatternSuggestion(command="aws cloudtrail describe-trails", description="List trails before deletion"),
            PatternSuggestion(command="aws cloudtrail get-trail-status --name {name}", description="Check trail status first"),
        ),
    ),
    DestructivePattern(
        name="aws-redshift-delete-cluster",
        regex=r"aws\b.*?\bredshift\s+delete-cluster",
        reason="aws redshift delete-cluster destroys Redshift cluster and all data",
        severity=Severity.CRITICAL,
        description="With --skip-final-cluster-snapshot, ALL data is destroyed immediately.",
        suggestions=(
            PatternSuggestion(command="aws redshift describe-clusters", description="List clusters before deletion"),
            PatternSuggestion(command="aws redshift delete-cluster --final-cluster-snapshot-identifier {snap}", description="Create final snapshot before delete"),
        ),
    ),
    DestructivePattern(
        name="aws-logs-delete-log-group",
        regex=r"aws\b.*?\blogs\s+delete-log-group",
        reason="aws logs delete-log-group permanently deletes log group and all events",
        severity=Severity.HIGH,
        description="All log streams, events, metric filters, and subscriptions are lost.",
        suggestions=(
            PatternSuggestion(command="aws logs describe-log-groups", description="List log groups first"),
            PatternSuggestion(command="aws logs export-task --log-group-name {name} --destination {bucket}", description="Export logs before deletion"),
        ),
    ),
    # ---- GCP / gcloud CLI ----
    DestructivePattern(
        name="gcp-compute-delete",
        regex=r"gcloud\b.*?\bcompute\s+instances\s+delete",
        reason="gcloud compute instances delete permanently destroys VM instances",
        severity=Severity.CRITICAL,
        description="Boot disk deleted unless --keep-disks specified. External IPs released.",
        suggestions=(
            PatternSuggestion(command="gcloud compute instances list", description="List instances first"),
            PatternSuggestion(command="gcloud compute instances stop {name} --zone={zone}", description="Stop instead of delete"),
        ),
    ),
    DestructivePattern(
        name="gcp-disk-delete",
        regex=r"gcloud\b.*?\bcompute\s+disks\s+delete",
        reason="gcloud compute disks delete permanently destroys disk data",
        severity=Severity.CRITICAL,
        description="All data on disk is lost forever without snapshots.",
        suggestions=(
            PatternSuggestion(command="gcloud compute disks list", description="List disks first"),
            PatternSuggestion(command="gcloud compute disks snapshot {disk} --zone={zone} --snapshot-names {snap}", description="Create snapshot before deletion"),
        ),
    ),
    DestructivePattern(
        name="gcp-sql-delete",
        regex=r"gcloud\b.*?\bsql\s+instances\s+delete",
        reason="gcloud sql instances delete permanently destroys Cloud SQL instance",
        severity=Severity.CRITICAL,
        description="Database and all data deleted along with backups and read replicas.",
        suggestions=(
            PatternSuggestion(command="gcloud sql instances list", description="List instances first"),
            PatternSuggestion(command="gcloud sql instances describe {name}", description="Check instance details first"),
        ),
    ),
    DestructivePattern(
        name="gcp-gsutil-rm-recursive",
        regex=r"gsutil\b.*?\brm\s+.*-r|gsutil\b.*?\brm\s+-[a-z]*r",
        reason="gsutil rm -r permanently deletes ALL objects in the GCS path",
        severity=Severity.CRITICAL,
        description="All objects under path deleted. No recovery without versioning.",
        suggestions=(
            PatternSuggestion("gsutil ls -r gs://bucket/path/",
                              "List objects first"),
            PatternSuggestion("gsutil versioning set on gs://bucket",
                              "Enable versioning for recovery"),
        ),
    ),
    DestructivePattern(
        name="gcp-gsutil-rb",
        regex=r"gsutil\b.*?\brb(?=\s|$)",
        reason="gsutil rb removes the entire GCS bucket",
        severity=Severity.CRITICAL,
        description="Bucket name becomes available to others. Bucket must be empty.",
        suggestions=(
            PatternSuggestion(command="gsutil ls gs://{bucket}", description="List bucket contents first"),
            PatternSuggestion(command="gsutil versioning get gs://{bucket}", description="Check versioning status first"),
        ),
    ),
    DestructivePattern(
        name="gcp-gke-delete",
        regex=r"gcloud\b.*?\bcontainer\s+clusters\s+delete",
        reason="gcloud container clusters delete removes entire GKE cluster",
        severity=Severity.CRITICAL,
        description="All nodes and workloads terminated. Persistent volumes may be deleted.",
        suggestions=(
            PatternSuggestion(command="gcloud container clusters list", description="List clusters first"),
            PatternSuggestion(command="kubectl get all --all-namespaces", description="Check workloads before deleting cluster"),
        ),
    ),
    DestructivePattern(
        name="gcp-project-delete",
        regex=r"gcloud\b.*?\bprojects\s+delete",
        reason="gcloud projects delete removes the ENTIRE GCP project and ALL resources",
        severity=Severity.CRITICAL,
        description="ALL resources deleted: VMs, databases, storage, functions, IAM. "
        "30-day recovery window, then permanent.",
        suggestions=(
            PatternSuggestion(command="gcloud projects list", description="List projects first"),
            PatternSuggestion(command="gcloud services list --project {name}", description="List enabled APIs before deletion"),
        ),
    ),
    DestructivePattern(
        name="gcp-functions-delete",
        regex=r"gcloud\b.*?\bfunctions\s+delete",
        reason="gcloud functions delete removes Cloud Function",
        severity=Severity.HIGH,
        description="Function code, configuration, triggers, and event subscriptions removed.",
        suggestions=(
            PatternSuggestion(command="gcloud functions list", description="List functions first"),
            PatternSuggestion(command="gcloud functions describe {name}", description="Check function details first"),
        ),
    ),
    DestructivePattern(
        name="gcp-firestore-delete",
        regex=r"gcloud\b.*?\bfirestore\s+.*delete",
        reason="gcloud firestore delete removes Firestore documents and collections",
        severity=Severity.CRITICAL,
        description="Documents and collections deleted. No automatic backups by default.",
        suggestions=(
            PatternSuggestion(command="gcloud firestore indexes list", description="List indexes first"),
            PatternSuggestion(command="gcloud firestore export gs://{bucket}", description="Export Firestore data before deletion"),
        ),
    ),
    DestructivePattern(
        name="gcp-secrets-delete",
        regex=r"gcloud\b.*?\bsecrets\s+delete",
        reason="gcloud secrets delete destroys a Secret Manager secret",
        severity=Severity.CRITICAL,
        description="Secret and ALL versions permanently deleted. No recovery window.",
        suggestions=(
            PatternSuggestion(command="gcloud secrets list", description="List secrets first"),
            PatternSuggestion(command="gcloud secrets versions list {name}", description="Check versions before deletion"),
        ),
    ),
    DestructivePattern(
        name="gcp-kms-keys-destroy",
        regex=r"gcloud\b.*?\bkms\s+keys\s+versions\s+destroy",
        reason="gcloud kms keys versions destroy schedules key version destruction",
        severity=Severity.CRITICAL,
        description="Data encrypted under this key version becomes unrecoverable.",
        suggestions=(
            PatternSuggestion(command="gcloud kms keys list --keyring {ring} --location {loc}", description="List keys first"),
            PatternSuggestion(command="gcloud kms keys versions list --key {key}", description="Check key versions before destruction"),
        ),
    ),
    DestructivePattern(
        name="gcp-iam-service-accounts-delete",
        regex=r"gcloud\b.*?\biam\s+service-accounts\s+delete",
        reason="gcloud iam service-accounts delete removes a service account",
        severity=Severity.CRITICAL,
        description="Workloads using this SA lose access. Can undelete within 30 days.",
        suggestions=(
            PatternSuggestion(command="gcloud iam service-accounts list", description="List service accounts first"),
            PatternSuggestion(command="gcloud iam service-accounts get-iam-policy {sa}", description="Check IAM bindings first"),
        ),
    ),
    DestructivePattern(
        name="gcp-dns-managed-zones-delete",
        regex=r"gcloud\b.*?\bdns\s+managed-zones\s+delete",
        reason="gcloud dns managed-zones delete removes DNS zone — domains stop resolving",
        severity=Severity.CRITICAL,
        description="All record sets deleted. Production traffic can go dark.",
        suggestions=(
            PatternSuggestion(command="gcloud dns managed-zones list", description="List zones first"),
            PatternSuggestion(command="gcloud dns record-sets list --zone={zone}", description="Export record sets first"),
        ),
    ),
    DestructivePattern(
        name="gcp-spanner-instances-delete",
        regex=r"gcloud\b.*?\bspanner\s+instances\s+delete",
        reason="gcloud spanner instances delete destroys Spanner instance and all data",
        severity=Severity.CRITICAL,
        description="All databases inside instance deleted. Unrecoverable without export.",
        suggestions=(
            PatternSuggestion(command="gcloud spanner instances list", description="List instances first"),
            PatternSuggestion(command="gcloud spanner databases list --instance={name}", description="List databases before deletion"),
        ),
    ),
    DestructivePattern(
        name="gcp-bigtable-instances-delete",
        regex=r"gcloud\b.*?\bbigtable\s+instances\s+delete",
        reason="gcloud bigtable instances delete destroys Bigtable instance and all data",
        severity=Severity.CRITICAL,
        description="All tables, clusters, and data permanently deleted.",
        suggestions=(
            PatternSuggestion(command="gcloud bigtable instances list", description="List instances first"),
            PatternSuggestion(command="gcloud bigtable instances describe {name}", description="Check instance details first"),
        ),
    ),
    DestructivePattern(
        name="gcp-bq-rm-recursive",
        regex=r"\bbq\b.*?\brm\s+.*-r\b|\bbq\b.*?\brm\s+.*-f\b",
        reason="bq rm -r/-f removes BigQuery datasets, tables — data lost",
        severity=Severity.CRITICAL,
        description="bq rm -r removes dataset + ALL tables/views/models inside. "
        "bq rm -f removes table without confirmation.",
        suggestions=(
            PatternSuggestion(command="bq ls {project}:{dataset}", description="List dataset contents first"),
            PatternSuggestion(command="bq show {project}:{dataset}.{table}", description="Check table metadata before removal"),
        ),
    ),
    # ---- Azure / az CLI ----
    DestructivePattern(
        name="az-vm-delete",
        regex=r"az\b.*?\bvm\s+delete",
        reason="az vm delete permanently destroys virtual machines",
        severity=Severity.CRITICAL,
        description="VM deallocated and deleted. OS disk deleted unless --os-disk=detach.",
        suggestions=(
            PatternSuggestion(command="az vm list", description="List VMs first"),
            PatternSuggestion(command="az vm deallocate --name {name} --resource-group {rg}", description="Deallocate instead of delete"),
        ),
    ),
    DestructivePattern(
        name="az-storage-delete",
        regex=r"az\b.*?\bstorage\s+account\s+delete",
        reason="az storage account delete destroys storage account and ALL data",
        severity=Severity.CRITICAL,
        description="ALL blobs, files, queues, tables deleted. Unrecoverable.",
        suggestions=(
            PatternSuggestion(command="az storage account list", description="List storage accounts first"),
            PatternSuggestion(command="az storage container list --account-name {name}", description="Check containers before deletion"),
        ),
    ),
    DestructivePattern(
        name="az-blob-delete",
        regex=r"az\b.*?\bstorage\s+(?:blob|container)\s+delete",
        reason="az storage blob/container delete permanently removes data",
        severity=Severity.HIGH,
        description="Blob delete removes individual blobs. Container delete removes ALL blobs.",
        suggestions=(
            PatternSuggestion(command="az storage blob list --container-name {container}", description="List blobs first"),
            PatternSuggestion(command="az storage container list", description="List containers before deletion"),
        ),
    ),
    DestructivePattern(
        name="az-sql-delete",
        regex=r"az\b.*?\bsql\s+(?:server|db)\s+delete",
        reason="az sql server/db delete permanently destroys the database",
        severity=Severity.CRITICAL,
        description="Server delete removes ALL databases. Database delete removes specific DB.",
        suggestions=(
            PatternSuggestion(command="az sql server list", description="List servers first"),
            PatternSuggestion(command="az sql db list --server {server} --resource-group {rg}", description="List databases first"),
        ),
    ),
    DestructivePattern(
        name="az-group-delete",
        regex=r"az\b.*?\bgroup\s+delete",
        reason="az group delete removes entire resource group and ALL resources within it",
        severity=Severity.CRITICAL,
        description="ALL resources in the group deleted: VMs, storage, databases, networks.",
        suggestions=(
            PatternSuggestion(command="az resource list --resource-group {rg}", description="List all resources in group first"),
            PatternSuggestion(command="az group export --name {rg}", description="Export resource group ARM template first"),
        ),
    ),
    DestructivePattern(
        name="az-aks-delete",
        regex=r"az\b.*?\baks\s+delete",
        reason="az aks delete removes entire AKS Kubernetes cluster",
        severity=Severity.CRITICAL,
        description="All nodes, workloads, load balancers terminated. Node resource group deleted.",
        suggestions=(
            PatternSuggestion(command="az aks list", description="List clusters first"),
            PatternSuggestion(command="kubectl get all --all-namespaces", description="Check workloads before deleting cluster"),
        ),
    ),
    DestructivePattern(
        name="az-webapp-delete",
        regex=r"az\b.*?\bwebapp\s+delete",
        reason="az webapp delete removes App Service",
        severity=Severity.HIGH,
        description="Application code, configuration, custom domains, and SSL certificates removed.",
        suggestions=(
            PatternSuggestion(command="az webapp list", description="List webapps first"),
            PatternSuggestion(command="az webapp config backup list --webapp-name {name}", description="Check backups first"),
        ),
    ),
    DestructivePattern(
        name="az-cosmosdb-delete",
        regex=r"az\b.*?\bcosmosdb\s+(?:delete|database\s+delete|collection\s+delete)",
        reason="az cosmosdb delete destroys Cosmos DB resources and data",
        severity=Severity.CRITICAL,
        description="Account delete removes entire Cosmos DB. Database/collection delete removes data.",
        suggestions=(
            PatternSuggestion(command="az cosmosdb list", description="List accounts first"),
            PatternSuggestion(command="az cosmosdb sql database list --account-name {name}", description="List databases first"),
        ),
    ),
    DestructivePattern(
        name="az-keyvault-delete",
        regex=r"az\b.*?\bkeyvault\s+delete",
        reason="az keyvault delete removes Key Vault — secrets may be unrecoverable",
        severity=Severity.CRITICAL,
        description="All secrets, keys, certificates deleted. Soft delete allows recovery if enabled.",
        suggestions=(
            PatternSuggestion(command="az keyvault list", description="List vaults first"),
            PatternSuggestion(command="az keyvault secret list --vault-name {name}", description="List secrets before deletion"),
        ),
    ),
    DestructivePattern(
        name="az-acr-delete",
        regex=r"az\b.*?\bacr\s+delete",
        reason="az acr delete removes container registry and ALL images",
        severity=Severity.CRITICAL,
        description="ALL repositories and images deleted. Registry name becomes available.",
        suggestions=(
            PatternSuggestion(command="az acr list", description="List registries first"),
            PatternSuggestion(command="az acr repository list --name {name}", description="List repositories first"),
        ),
    ),
    DestructivePattern(
        name="az-acr-repository-delete",
        regex=r"az\b.*?\bacr\s+repository\s+delete",
        reason="az acr repository delete permanently deletes repository and its images",
        severity=Severity.HIGH,
        description="All tags and images in the repository deleted. New pulls will fail.",
        suggestions=(
            PatternSuggestion(command="az acr repository show-tags --name {name} --repository {repo}", description="List tags before deletion"),
            PatternSuggestion(command="az acr repository show-manifests --name {name} --repository {repo}", description="List manifests first"),
        ),
    ),
    DestructivePattern(
        name="az-keyvault-item-delete-or-purge",
        regex=r"az\b.*?\bkeyvault\s+(?:key|secret|certificate|storage)\s+(?:delete|purge)",
        reason="Key Vault item delete/purge — purge bypasses soft-delete and is irreversible",
        severity=Severity.CRITICAL,
        description="Purge is PERMANENT. Applications/services bound to the item fail immediately.",
        suggestions=(
            PatternSuggestion(command="az keyvault key/secret/certificate list --vault-name {name}", description="List items before deletion"),
            PatternSuggestion(command="az keyvault {secret} show --vault-name {name}", description="Check item details first"),
        ),
    ),
    DestructivePattern(
        name="az-ad-sp-delete",
        regex=r"az\b.*?\bad\s+sp\s+delete",
        reason="az ad sp delete removes service principal — workloads using it lose auth",
        severity=Severity.CRITICAL,
        description="All workloads authenticating via this SP lose access. "
        "Can restore within 30 days via Graph API.",
        suggestions=(
            PatternSuggestion(command="az ad sp list", description="List service principals first"),
            PatternSuggestion(command="az ad sp show --id {id}", description="Check SP details before deletion"),
        ),
    ),
    DestructivePattern(
        name="az-ad-app-delete",
        regex=r"az\b.*?\bad\s+app\s+delete",
        reason="az ad app delete removes Azure AD app registration — all SPs break",
        severity=Severity.CRITICAL,
        description="All service principals derived from this app stop working. "
        "OAuth grants invalidated.",
        suggestions=(
            PatternSuggestion(command="az ad app list", description="List app registrations first"),
            PatternSuggestion(command="az ad app show --id {id}", description="Check app details first"),
        ),
    ),
    DestructivePattern(
        name="az-network-dns-zone-delete",
        regex=r"az\b.*?\bnetwork\s+dns\s+zone\s+delete",
        reason="az network dns zone delete removes DNS zone — domains stop resolving",
        severity=Severity.CRITICAL,
        description="All record sets deleted. Production traffic goes dark.",
        suggestions=(
            PatternSuggestion(command="az network dns zone list", description="List zones first"),
            PatternSuggestion(command="az network dns record-set list --zone-name {zone}", description="Export record sets first"),
        ),
    ),
)


def build_cloud_pack() -> Pack:
    return Pack(id="cloud", name="Cloud CLI patterns",
        destructive_patterns=CLOUD_PATTERNS,
        keywords=("aws", "gcloud", "az", "gsutil", "bq"),
    )
