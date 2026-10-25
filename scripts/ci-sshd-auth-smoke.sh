#!/usr/bin/env bash
# Offline isolated smoke of the EXACT built SSHD image: no host ports or live volumes.
set -euo pipefail

if [ "$#" -ne 1 ]; then
  echo "::error::sshd smoke requires exactly one immutable image ID" >&2
  exit 2
fi
image_id="$1"
if [[ ! "$image_id" =~ ^sha256:[0-9a-f]{64}$ ]]; then
  echo "::error::sshd smoke requires an immutable local sha256 image ID" >&2
  exit 2
fi

docker run --rm --network none --entrypoint /usr/local/bin/sshd-entrypoint \
  "$image_id" /bin/sh -ec '
  set -eu
  effective="$(sshd -T -C user=mcpuser,host=localhost,addr=127.0.0.1)"
  for setting in \
    "passwordauthentication no" \
    "kbdinteractiveauthentication no" \
    "permitemptypasswords no" \
    "pubkeyauthentication yes" \
    "authenticationmethods publickey"; do
    if ! printf "%s\n" "$effective" | grep -Fqx "$setting"; then
      echo "sshd effective config is missing: $setting" >&2
      exit 1
    fi
  done

  # /etc/shadow must not contain an active password hash for mcpuser.
  grep -Eq "^mcpuser:[!*]" /etc/shadow || {
    echo "mcpuser must have a locked system password" >&2
    exit 1
  }

  # Disposable key only in this one-shot test container, never in the image.
  ssh-keygen -q -t ed25519 -N "" -f /tmp/smoke_key
  cat /tmp/smoke_key.pub >> /home/mcpuser/.ssh/authorized_keys
  chown mcpuser:mcpuser /home/mcpuser/.ssh/authorized_keys
  chmod 600 /home/mcpuser/.ssh/authorized_keys

  /usr/sbin/sshd -D -p 2222 >/tmp/sshd.log 2>&1 &
  sshd_pid=$!
  trap "kill $sshd_pid 2>/dev/null || true" EXIT HUP INT TERM

  # Positive control: server starts and accepts a valid public key.
  connected=false
  n=0
  while [ "$n" -lt 8 ]; do
    if ssh -F /dev/null -n \
      -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o IdentitiesOnly=yes -o ConnectTimeout=2 -o BatchMode=yes \
      -o PreferredAuthentications=publickey \
      -i /tmp/smoke_key -p 2222 mcpuser@127.0.0.1 true \
      >/tmp/pubkey.log 2>&1; then
      connected=true
      break
    fi
    n=$((n+1))
    sleep 1
  done
  if [ "$connected" != true ]; then
    echo "isolated public-key authentication failed" >&2
    cat /tmp/pubkey.log >&2
    cat /tmp/sshd.log >&2
    exit 1
  fi

  # Negative control: password-only login must never be accepted or offered.
  if ssh -F /dev/null -vv -n \
    -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
    -o PubkeyAuthentication=no -o KbdInteractiveAuthentication=no \
    -o PasswordAuthentication=yes -o PreferredAuthentications=password \
    -o NumberOfPasswordPrompts=0 -o BatchMode=yes -o ConnectTimeout=3 \
    -p 2222 mcpuser@127.0.0.1 true >/tmp/deny.log 2>&1; then
    echo "password-only login unexpectedly succeeded" >&2
    exit 1
  fi
  if ! grep -Fq "Authentications that can continue: publickey" /tmp/deny.log; then
    echo "SSH client did not prove server offered publickey-only auth" >&2
    cat /tmp/deny.log >&2
    exit 1
  fi
  if grep -Eq "Authentications that can continue:.*(password|keyboard-interactive)" /tmp/deny.log; then
    echo "SSHD advertised disallowed password or interactive auth" >&2
    exit 1
  fi
  echo "isolated SSHD config + publickey success + password denial: PASS"
'
