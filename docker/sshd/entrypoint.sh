#!/bin/sh
set -eu

HOSTKEY_DIR=/etc/ssh/hostkeys
mkdir -p "$HOSTKEY_DIR"
chmod 700 "$HOSTKEY_DIR"

key_is_loadable() {
  # A valid private host key must round-trip through `ssh-keygen -y` (which
  # prints the derived public key). This rejects empty, truncated and
  # partially written key files that `-s` alone would silently accept and
  # that would then fail `sshd -t`/host-key loading at runtime.
  ssh-keygen -y -f "$1" >/dev/null 2>&1
}

ensure_host_key() {
  key_type="$1"
  key_path="$2"
  shift 2
  if [ -f "$key_path" ]; then
    chmod 600 "$key_path"
  fi
  if [ ! -s "$key_path" ] || ! key_is_loadable "$key_path"; then
    rm -f "$key_path" "$key_path.pub"
    ssh-keygen -q -t "$key_type" "$@" -f "$key_path" -N ""
  fi
  chmod 600 "$key_path"
  if [ -f "$key_path.pub" ]; then
    chmod 644 "$key_path.pub"
  fi
}

ensure_host_key rsa "$HOSTKEY_DIR/ssh_host_rsa_key" -b 4096
ensure_host_key ecdsa "$HOSTKEY_DIR/ssh_host_ecdsa_key"
ensure_host_key ed25519 "$HOSTKEY_DIR/ssh_host_ed25519_key"

exec "$@"
