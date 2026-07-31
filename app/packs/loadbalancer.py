from __future__ import annotations

from app.command_policy import DestructivePattern, PatternSuggestion, Severity, SuggestionKind
from app.packs import Pack

LOADBALANCER_PATTERNS: tuple[DestructivePattern, ...] = (
    # nginx
    DestructivePattern(
        name="nginx-stop",
        regex=r"nginx\s+-s\s+stop\b",
        reason="nginx -s stop shuts down nginx and stops the load balancer.",
        severity=Severity.HIGH,
        description="Sending the stop signal terminates nginx immediately. All in-flight requests are dropped.",
        suggestions=(
            PatternSuggestion(command="nginx -s quit", description="Graceful shutdown instead of immediate stop", kind=SuggestionKind.SAFER_ALTERNATIVE),
            PatternSuggestion(command="systemctl status nginx", description="Check status first", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
    DestructivePattern(
        name="nginx-quit",
        regex=r"nginx\s+-s\s+quit\b",
        reason="nginx -s quit gracefully stops nginx and halts traffic handling.",
        severity=Severity.HIGH,
        description="The quit signal stops accepting new connections. No new traffic is routed.",
        suggestions=(
            PatternSuggestion(command="systemctl status nginx", description="Check status first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="nginx -t", description="Test configuration before restart", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
    DestructivePattern(
        name="systemctl-stop-nginx",
        regex=r"systemctl\b.*?\s+stop\s+nginx(?:\.service)?\b",
        reason="systemctl stop nginx stops the nginx service and disrupts traffic.",
        severity=Severity.HIGH,
        description="Stopping nginx via systemctl shuts down all worker processes.",
        suggestions=(
            PatternSuggestion(command="nginx -s reload", description="Reload configuration without stopping", kind=SuggestionKind.SAFER_ALTERNATIVE),
            PatternSuggestion(command="systemctl restart nginx", description="Restart instead of stop", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
    DestructivePattern(
        name="service-stop-nginx",
        regex=r"service\s+nginx\s+stop\b",
        reason="service nginx stop stops the nginx service and disrupts traffic.",
        severity=Severity.HIGH,
        description="Stopping nginx via the service command terminates all worker processes.",
        suggestions=(
            PatternSuggestion(command="nginx -s reload", description="Reload configuration without stopping", kind=SuggestionKind.SAFER_ALTERNATIVE),
            PatternSuggestion(command="service nginx restart", description="Restart instead of stop", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
    DestructivePattern(
        name="nginx-config-delete",
        regex=r"\brm\b.*\s+['\"]?/etc/nginx(?:/|\b)",
        reason="Removing files from /etc/nginx deletes nginx configuration.",
        severity=Severity.CRITICAL,
        description="Deleting nginx config removes site definitions, upstream blocks, and SSL references.",
        suggestions=(
            PatternSuggestion(command="cp /etc/nginx /tmp/nginx.backup", description="Backup config before deleting", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="ls -la /etc/nginx/", description="List config files before deletion", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
    # haproxy
    DestructivePattern(
        name="haproxy-soft-stop",
        regex=r"\bhaproxy\s+.*-sf\b",
        reason="haproxy -sf sends a soft stop signal to the load balancer.",
        severity=Severity.HIGH,
        description="Soft-stop gracefully finishes current connections before shutting down.",
        suggestions=(
            PatternSuggestion(command="systemctl status haproxy", description="Check status first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="haproxy -c -f {cfg}", description="Validate config before restart", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
    DestructivePattern(
        name="haproxy-hard-stop",
        regex=r"\bhaproxy\s+.*-st\b",
        reason="haproxy -st sends a hard stop signal, immediately terminating the load balancer.",
        severity=Severity.HIGH,
        description="Hard-stop kills HAProxy immediately. Active connections are dropped.",
        suggestions=(
            PatternSuggestion(command="haproxy -sf {pid}", description="Use soft-stop instead of hard stop", kind=SuggestionKind.SAFER_ALTERNATIVE),
            PatternSuggestion(command="systemctl restart haproxy", description="Restart instead of hard stop", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
    DestructivePattern(
        name="haproxy-systemctl-stop",
        regex=r"systemctl\b.*?\s+stop\s+haproxy(?:\.service)?\b",
        reason="systemctl stop haproxy stops the HAProxy service.",
        severity=Severity.HIGH,
        description="Stopping HAProxy via systemctl terminates all proxy processes.",
        suggestions=(
            PatternSuggestion(command="systemctl reload haproxy", description="Reload config without stopping", kind=SuggestionKind.SAFER_ALTERNATIVE),
            PatternSuggestion(command="haproxy -c -f {cfg}", description="Validate config before restart", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
    DestructivePattern(
        name="haproxy-service-stop",
        regex=r"service\s+haproxy\s+stop\b",
        reason="service haproxy stop stops the HAProxy service.",
        severity=Severity.HIGH,
        description="Stopping HAProxy via service command terminates all proxy processes.",
        suggestions=(
            PatternSuggestion(command="service haproxy restart", description="Restart instead of stop", kind=SuggestionKind.SAFER_ALTERNATIVE),
            PatternSuggestion(command="service haproxy status", description="Check status first", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
    DestructivePattern(
        name="haproxy-socat-disable-server",
        regex=r"(?:echo|printf)\s+['\"]?disable\s+server\b.*\|\s*socat\b",
        reason="Disabling a server via HAProxy runtime API removes it from the pool.",
        severity=Severity.HIGH,
        description="Disabling a server via socat removes it from the load balancer pool immediately.",
        suggestions=(
            PatternSuggestion(command="socat /var/run/haproxy.sock 'show stat'", description="Check server status first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="socat /var/run/haproxy.sock 'set server {backend}/{server} weight 0'", description="Drain connections before disabling", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
    DestructivePattern(
        name="haproxy-socat-shutdown-sessions",
        regex=r"(?:echo|printf)\s+['\"]?shutdown\s+sessions\b.*\|\s*socat\b",
        reason="Shutting down sessions via HAProxy runtime API terminates active connections.",
        severity=Severity.HIGH,
        description="Shutting down sessions terminates all active connections to the backend.",
        suggestions=(
            PatternSuggestion(command="socat /var/run/haproxy.sock 'show sess'", description="List active sessions first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="socat /var/run/haproxy.sock 'set server {backend}/{server} weight 0'", description="Drain traffic gradually", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
    DestructivePattern(
        name="haproxy-socat-disable-frontend",
        regex=r"(?:echo|printf)\s+['\"]?disable\s+frontend\b.*\|\s*socat\b",
        reason="Disabling a frontend via HAProxy runtime API stops accepting new connections.",
        severity=Severity.HIGH,
        description="Disabling a frontend immediately stops accepting new connections.",
        suggestions=(
            PatternSuggestion(command="socat /var/run/haproxy.sock 'show stat'", description="Check frontend stats first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="socat /var/run/haproxy.sock 'set frontend {frtnd} maxconn 0'", description="Limit connections instead of disabling", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
    DestructivePattern(
        name="haproxy-socat-shutdown-frontend",
        regex=r"(?:echo|printf)\s+['\"]?shutdown\s+frontend\b.*\|\s*socat\b",
        reason="Shutting down a frontend via HAProxy runtime API terminates it immediately.",
        severity=Severity.HIGH,
        description="Shutting down a frontend terminates the frontend and all its connections.",
        suggestions=(
            PatternSuggestion(command="socat /var/run/haproxy.sock 'show frontend {frtnd}'", description="Check frontend state first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="socat /var/run/haproxy.sock 'disable frontend {frtnd}'", description="Disable instead of full shutdown", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
    DestructivePattern(
        name="haproxy-config-delete",
        regex=r"\brm\b.*\s+['\"]?/etc/haproxy(?:/|\b)",
        reason="Removing files from /etc/haproxy deletes HAProxy configuration.",
        severity=Severity.HIGH,
        description="Deleting HAProxy config removes backend definitions and frontend configurations.",
        suggestions=(
            PatternSuggestion(command="cp -r /etc/haproxy /tmp/haproxy.backup", description="Backup config first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="ls -la /etc/haproxy/", description="List config files before deleting", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
    # traefik
    DestructivePattern(
        name="traefik-docker-stop",
        regex=r"docker\b.*?\s+(?:stop|kill)\s+.*\btraefik\b",
        reason="Stopping the Traefik container halts all traffic routing.",
        severity=Severity.CRITICAL,
        description="Stopping or killing the Traefik container immediately halts all traffic routing.",
        suggestions=(
            PatternSuggestion(command="docker ps -f name=traefik", description="Check container status first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="docker restart {container}", description="Restart instead of stop", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
    DestructivePattern(
        name="traefik-docker-rm",
        regex=r"docker\b.*?\s+rm\s+.*\btraefik\b",
        reason="Removing the Traefik container destroys the load balancer.",
        severity=Severity.CRITICAL,
        description="Removing the Traefik container deletes it entirely, including runtime state.",
        suggestions=(
            PatternSuggestion(command="docker stop {container}", description="Stop without removing", kind=SuggestionKind.SAFER_ALTERNATIVE),
            PatternSuggestion(command="docker commit {container} traefik-backup", description="Backup container state first", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
    DestructivePattern(
        name="traefik-compose-down",
        regex=r"docker[\s-]compose\s+.*\bdown\b.*\btraefik\b",
        reason="docker-compose down on Traefik stops and removes the load balancer.",
        severity=Severity.CRITICAL,
        description="docker-compose down stops and removes Traefik containers and networks.",
        suggestions=(
            PatternSuggestion(command="docker-compose stop traefik", description="Stop service without removing", kind=SuggestionKind.SAFER_ALTERNATIVE),
            PatternSuggestion(command="docker-compose ps", description="Check service status first", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
    DestructivePattern(
        name="traefik-kubectl-delete-pod",
        regex=r"kubectl\b.*?\s+delete\s+(?:pod|deployment|daemonset)\s+.*\btraefik\b",
        reason="Deleting Traefik pods/deployments disrupts traffic routing.",
        severity=Severity.CRITICAL,
        description="Deleting Traefik pods or deployments removes the load balancer from the cluster.",
        suggestions=(
            PatternSuggestion(command="kubectl get pods -l app.kubernetes.io/name=traefik", description="List traefik pods first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="kubectl rollout restart deployment/traefik", description="Restart instead of delete", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
    DestructivePattern(
        name="traefik-kubectl-delete-ingressroute",
        regex=r"kubectl\b.*?\s+delete\s+ingressroute\b",
        reason="Deleting IngressRoute CRDs removes Traefik routing rules.",
        severity=Severity.HIGH,
        description="Deleting IngressRoute CRDs removes routing rules, making services unreachable.",
        suggestions=(
            PatternSuggestion(command="kubectl get ingressroute -A", description="List all ingress routes first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="kubectl describe ingressroute {name}", description="Describe route before deleting", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
    DestructivePattern(
        name="traefik-config-delete",
        regex=r"\brm\b.*\btraefik\b.*\.(?:ya?ml|toml)\b",
        reason="Removing Traefik config files disrupts load balancer configuration.",
        severity=Severity.CRITICAL,
        description="Deleting Traefik config removes entrypoints, middleware, and provider settings.",
        suggestions=(
            PatternSuggestion(command="cp {config} {config}.backup", description="Backup config file first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="ls -la /etc/traefik/", description="List config files before deleting", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
    DestructivePattern(
        name="traefik-api-delete",
        regex=r"(?i)\bcurl\b(?=.*(?:-X\s*|--request(?:=|\s+))DELETE\b)(?=.*\btraefik\b.*\b/api/).*",
        reason="DELETE operations against Traefik API can remove routing configuration.",
        severity=Severity.HIGH,
        description="Sending DELETE to Traefik API removes routers, services, or middleware.",
        suggestions=(
            PatternSuggestion(command="curl -X GET {traefik-url}/api/http/routers", description="List routers before deleting", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="curl -X PUT {traefik-url}/api/http/routers/{name}", description="Update instead of delete", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
    DestructivePattern(
        name="traefik-systemctl-stop",
        regex=r"systemctl\b.*?\s+stop\s+traefik(?:\.service)?\b",
        reason="systemctl stop traefik stops the Traefik service.",
        severity=Severity.HIGH,
        description="Stopping Traefik via systemctl shuts down the load balancer process.",
        suggestions=(
            PatternSuggestion(command="systemctl status traefik", description="Check status first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="systemctl reload traefik", description="Reload instead of stop", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
    DestructivePattern(
        name="traefik-service-stop",
        regex=r"service\s+traefik\s+stop\b",
        reason="service traefik stop stops the Traefik service.",
        severity=Severity.HIGH,
        description="Stopping Traefik via service command terminates the load balancer.",
        suggestions=(
            PatternSuggestion(command="service traefik status", description="Check status first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="service traefik restart", description="Restart instead of stop", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
    # AWS ELB
    DestructivePattern(
        name="elbv2-delete-load-balancer",
        regex=r"\baws\b(?:\s+(?:--profile|--region|--output|--endpoint-url)\s+\S+|\s+--\S+)*\s+elbv2\s+delete-load-balancer\b",
        reason="aws elbv2 delete-load-balancer permanently deletes the load balancer.",
        severity=Severity.HIGH,
        description="Deletes an ALB or NLB. All traffic to that load balancer stops immediately.",
        suggestions=(
            PatternSuggestion(command="aws elbv2 describe-load-balancers", description="List load balancers first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="aws elbv2 describe-listeners --load-balancer-arn {arn}", description="Check listeners before deletion", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
    DestructivePattern(
        name="elbv2-delete-target-group",
        regex=r"\baws\b(?:\s+(?:--profile|--region|--output|--endpoint-url)\s+\S+|\s+--\S+)*\s+elbv2\s+delete-target-group\b",
        reason="aws elbv2 delete-target-group permanently deletes the target group.",
        severity=Severity.HIGH,
        description="Deletes an ELBv2 target group. Instances in the group become unreachable.",
        suggestions=(
            PatternSuggestion(command="aws elbv2 describe-target-groups", description="List target groups first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="aws elbv2 describe-target-health --target-group-arn {arn}", description="Check target health first", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
    DestructivePattern(
        name="elbv2-deregister-targets",
        regex=r"\baws\b(?:\s+(?:--profile|--region|--output|--endpoint-url)\s+\S+|\s+--\S+)*\s+elbv2\s+deregister-targets\b",
        reason="aws elbv2 deregister-targets removes targets from the load balancer.",
        severity=Severity.HIGH,
        description="Deregisters targets from an ALB/NLB target group. Live traffic is disrupted.",
        suggestions=(
            PatternSuggestion(command="aws elbv2 describe-target-health --target-group-arn {arn}", description="Check target health first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="aws elbv2 register-targets --target-group-arn {arn}", description="Register alternative targets first", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
    DestructivePattern(
        name="elbv2-delete-listener",
        regex=r"\baws\b(?:\s+(?:--profile|--region|--output|--endpoint-url)\s+\S+|\s+--\S+)*\s+elbv2\s+delete-listener\b",
        reason="aws elbv2 delete-listener deletes a listener, potentially breaking traffic routing.",
        severity=Severity.HIGH,
        description="Deletes a listener. All rules in the listener are removed.",
        suggestions=(
            PatternSuggestion(command="aws elbv2 describe-rules --listener-arn {arn}", description="List rules first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="aws elbv2 modify-listener --listener-arn {arn}", description="Modify instead of delete", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
    DestructivePattern(
        name="elbv2-delete-rule",
        regex=r"\baws\b(?:\s+(?:--profile|--region|--output|--endpoint-url)\s+\S+|\s+--\S+)*\s+elbv2\s+delete-rule\b",
        reason="aws elbv2 delete-rule deletes a listener rule, potentially breaking routing.",
        severity=Severity.HIGH,
        description="Deletes a listener rule. Associated routing logic is removed.",
        suggestions=(
            PatternSuggestion(command="aws elbv2 describe-rules --listener-arn {arn}", description="List all rules first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="aws elbv2 modify-rule --rule-arn {arn}", description="Modify instead of delete", kind=SuggestionKind.SAFER_ALTERNATIVE),
        ),
    ),
    DestructivePattern(
        name="elb-delete-load-balancer",
        regex=r"\baws\b(?:\s+(?:--profile|--region|--output|--endpoint-url)\s+\S+|\s+--\S+)*\s+elb\s+delete-load-balancer\b",
        reason="aws elb delete-load-balancer permanently deletes the classic load balancer.",
        severity=Severity.HIGH,
        description="Deletes a Classic ELB. All traffic stops immediately.",
        suggestions=(
            PatternSuggestion(command="aws elb describe-load-balancers", description="List load balancers first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="aws elb describe-instance-health --load-balancer-name {name}", description="Check instance health first", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
    DestructivePattern(
        name="elb-deregister-instances",
        regex=r"\baws\b(?:\s+(?:--profile|--region|--output|--endpoint-url)\s+\S+|\s+--\S+)*\s+elb\s+deregister-instances-from-load-balancer\b",
        reason="aws elb deregister-instances-from-load-balancer removes instances from the load balancer.",
        severity=Severity.HIGH,
        description="Deregisters EC2 instances from a Classic ELB. Live traffic is disrupted.",
        suggestions=(
            PatternSuggestion(command="aws elb describe-instance-health --load-balancer-name {name}", description="Check instance health first", kind=SuggestionKind.PREVIEW_FIRST),
            PatternSuggestion(command="aws elb register-instances-with-load-balancer --load-balancer-name {name}", description="Register new instances first", kind=SuggestionKind.PREVIEW_FIRST),
        ),
    ),
)


def build_loadbalancer_pack() -> Pack:
    return Pack(id="loadbalancer", name="Loadbalancer patterns",
        destructive_patterns=LOADBALANCER_PATTERNS,
        keywords=("nginx", "haproxy", "traefik", "aws", "kubectl"),
    )
