FROM alpine:3.22.7

ARG OPENCODE_VERSION=1.18.16
ARG UV_VERSION=0.12.5
ARG NODEJS_APK_VERSION=22.23.4-r0
ARG NPM_APK_VERSION=11.6.4-r0

# python3: the generated agent script (agent_tools.py) runs python3 heredocs
# on the SSH target for proxy lease (fetch) and limit reporting (report).
# opencode: pinned release v1.18.16 is the reproducible bootstrap seed. The
# production runner copies it into agent_runtime, runs the serialized daily
# upgrade gate, and selects the unattended flag supported by the upgraded CLI.
# Download the seed artifact directly so retries cover the actual binary archive
# than only a mutable bootstrap installer. Alpine uses musl; x64 without AVX2
# uses the baseline artifact, matching OpenCode's official installer selection.
# Install into /usr/local/bin so `command -v opencode` resolves for mcpuser's
# SSH session without depending on root's untraversable home directory.
# uv: installed inside the retryable build layer instead of copied from an
# external uv OCI stage, so CI does not fail before retries/proxy args can apply
# while fetching an anonymous registry metadata token.
# ripgrep: OpenCode's repo tools (grep/search) shell out to `rg`; the agent's
# SSH session needs the binary on PATH to search the executor workspace.
# nodejs/npm: the identical SHA-pinned SSHD image is used for both OpenCode
# execution and trusted candidate verification (MCP_VERIFIER_IMAGE). Pin the
# Alpine 3.22 Node 22/npm package versions and assert the major at build time;
# JS/Astro checks must not fail because either trusted execution plane lacks npm.
# OpenSSH rejects a locked shadow account even if it has a valid authorized key.
# Use a non-crypt NP shadow sentinel: account stays enabled for public-key auth
# without provisioning an actual password or usable password hash.
RUN apk add --no-cache openssh-server openssh-client-default git curl bash python3 py3-pip ripgrep \
        "nodejs=${NODEJS_APK_VERSION}" "npm=${NPM_APK_VERSION}" && \
    test "$(node -p 'process.versions.node.split(".")[0]')" = "22" && \
    node --version && npm --version && \
    python3 -m pip install --no-cache-dir --break-system-packages "uv==${UV_VERSION}" && \
    adduser -D mcpuser && \
    sed -i 's/^mcpuser:[^:]*:/mcpuser:NP:/' /etc/shadow && \
    mkdir -p /home/mcpuser/.ssh && \
    chmod 700 /home/mcpuser/.ssh && \
    case "$(uname -m)" in \
        x86_64) opencode_arch=x64 ;; \
        aarch64|arm64) opencode_arch=arm64 ;; \
        *) echo "Unsupported OpenCode architecture: $(uname -m)" >&2; exit 1 ;; \
    esac && \
    opencode_target="linux-${opencode_arch}" && \
    if [ "$opencode_arch" = "x64" ] && ! grep -qwi avx2 /proc/cpuinfo 2>/dev/null; then \
        opencode_target="${opencode_target}-baseline"; \
    fi && \
    opencode_target="${opencode_target}-musl" && \
    curl --retry 5 --retry-all-errors --connect-timeout 15 -fL \
        -o /tmp/opencode.tar.gz \
        "https://github.com/anomalyco/opencode/releases/download/v${OPENCODE_VERSION}/opencode-${opencode_target}.tar.gz" && \
    tar -xzf /tmp/opencode.tar.gz -C /tmp && \
    install -Dm 0755 /tmp/opencode /usr/local/bin/opencode && \
    rm -f /tmp/opencode.tar.gz /tmp/opencode && \
    uv --version && \
    opencode --version && \
    mkdir -p /var/lib/mcp-agent/state /var/lib/mcp-agent/workspaces /var/lib/mcp-agent/sources && \
    chown -R mcpuser:mcpuser /var/lib/mcp-agent

COPY authorized_keys /home/mcpuser/.ssh/authorized_keys

COPY entrypoint.sh /usr/local/bin/sshd-entrypoint

RUN chown -R mcpuser:mcpuser /home/mcpuser/.ssh && \
    chmod 600 /home/mcpuser/.ssh/authorized_keys && \
    mkdir -p /etc/ssh/hostkeys && \
    chmod 700 /etc/ssh/hostkeys && \
    chmod 755 /usr/local/bin/sshd-entrypoint && \
    echo "HostKey /etc/ssh/hostkeys/ssh_host_rsa_key" >> /etc/ssh/sshd_config && \
    echo "HostKey /etc/ssh/hostkeys/ssh_host_ecdsa_key" >> /etc/ssh/sshd_config && \
    echo "HostKey /etc/ssh/hostkeys/ssh_host_ed25519_key" >> /etc/ssh/sshd_config && \
    echo "PasswordAuthentication no" >> /etc/ssh/sshd_config && \
    echo "KbdInteractiveAuthentication no" >> /etc/ssh/sshd_config && \
    echo "PermitEmptyPasswords no" >> /etc/ssh/sshd_config && \
    echo "PubkeyAuthentication yes" >> /etc/ssh/sshd_config && \
    echo "AuthenticationMethods publickey" >> /etc/ssh/sshd_config && \
    git config --system safe.directory '*'

ARG BUILD_SHA=unknown
ARG BUILD_TIME=unknown
ENV BUILD_SHA=${BUILD_SHA} BUILD_TIME=${BUILD_TIME}
EXPOSE 2222

ENTRYPOINT ["/usr/local/bin/sshd-entrypoint"]
CMD ["/usr/sbin/sshd", "-D", "-p", "2222"]
