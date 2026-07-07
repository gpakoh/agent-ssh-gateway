FROM alpine:3.19

RUN apk add --no-cache openssh-server git && \
    adduser -D mcpuser && \
    mkdir -p /home/mcpuser/.ssh && \
    chmod 700 /home/mcpuser/.ssh

COPY authorized_keys /home/mcpuser/.ssh/authorized_keys

RUN chown -R mcpuser:mcpuser /home/mcpuser/.ssh && \
    chmod 600 /home/mcpuser/.ssh/authorized_keys && \
    ssh-keygen -A && \
    echo "PasswordAuthentication yes" >> /etc/ssh/sshd_config && \
    echo "PubkeyAuthentication yes" >> /etc/ssh/sshd_config && \
    echo "mcpuser:sshpass123" | chpasswd && \
    git config --system safe.directory '*'

EXPOSE 2222

CMD ["/usr/sbin/sshd", "-D", "-p", "2222"]
