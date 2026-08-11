FROM alpine:3.19

# python3: the generated agent script (agent_tools.py) runs python3 heredocs
# on the SSH target for proxy lease (fetch) and limit reporting (report).
# opencode: new opencode.ai CLI (v1.18.16) -- matches the flags the agent
# script generates (opencode run --dangerously-skip-permissions --model ...).
# The installer puts the binary in $HOME/.opencode/bin (root's, which is also
# the script's OPCODE_BIN fallback). A symlink from /usr/local/bin would be
# useless for mcpuser (root's home is 0700, untraversable), so copy the binary:
# `command -v opencode` then resolves for mcpuser's SSH session (PATH includes
# /usr/local/bin, same as uv).
RUN apk add --no-cache openssh-server git curl bash python3 && \
    adduser -D mcpuser && \
    mkdir -p /home/mcpuser/.ssh && \
    chmod 700 /home/mcpuser/.ssh && \
    curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh && \
    curl -fsSL https://opencode.ai/install | bash -s -- --version 1.18.16 --no-modify-path && \
    install -Dm 0755 /root/.opencode/bin/opencode /usr/local/bin/opencode

COPY authorized_keys /home/mcpuser/.ssh/authorized_keys

RUN chown -R mcpuser:mcpuser /home/mcpuser/.ssh && \
    chmod 600 /home/mcpuser/.ssh/authorized_keys && \
    mkdir -p /etc/ssh/hostkeys && \
    ssh-keygen -q -t rsa -b 4096 -f /etc/ssh/hostkeys/ssh_host_rsa_key -N "" && \
    ssh-keygen -q -t ecdsa -f /etc/ssh/hostkeys/ssh_host_ecdsa_key -N "" && \
    ssh-keygen -q -t ed25519 -f /etc/ssh/hostkeys/ssh_host_ed25519_key -N "" && \
    chmod 700 /etc/ssh/hostkeys && \
    chmod 600 /etc/ssh/hostkeys/ssh_host_*_key && \
    echo "HostKey /etc/ssh/hostkeys/ssh_host_rsa_key" >> /etc/ssh/sshd_config && \
    echo "HostKey /etc/ssh/hostkeys/ssh_host_ecdsa_key" >> /etc/ssh/sshd_config && \
    echo "HostKey /etc/ssh/hostkeys/ssh_host_ed25519_key" >> /etc/ssh/sshd_config && \
    echo "PasswordAuthentication yes" >> /etc/ssh/sshd_config && \
    echo "PubkeyAuthentication yes" >> /etc/ssh/sshd_config && \
    echo "mcpuser:sshpass123" | chpasswd && \
    git config --system safe.directory '*'

EXPOSE 2222

CMD ["/usr/sbin/sshd", "-D", "-p", "2222"]
