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

  # Account must be enabled for SSH public keys but have no usable password.
  grep -Eq "^mcpuser:NP:" /etc/shadow || {
    echo "mcpuser must have a non-crypt shadow password sentinel" >&2
    exit 1
  }

  # Disposable key only in this one-shot test container, never in the image.
  ssh-keygen -q -t ed25519 -N "" -f /tmp/smoke_key
  # Confirm that the test key really contains a readable signing private key.
  ssh-keygen -y -f /tmp/smoke_key >/tmp/derived.pub
  # The .pub line can include an optional comment; compare the key itself.
  cut -d " " -f 1,2 /tmp/derived.pub >/tmp/derived.normalized
  cut -d " " -f 1,2 /tmp/smoke_key.pub >/tmp/expected.normalized
  cmp /tmp/derived.normalized /tmp/expected.normalized
  cat /tmp/smoke_key.pub >> /home/mcpuser/.ssh/authorized_keys
  chown mcpuser:mcpuser /home/mcpuser/.ssh/authorized_keys
  chmod 600 /home/mcpuser/.ssh/authorized_keys

  # Keep diagnostics bounded and local to this disposable container.
  /usr/sbin/sshd -D -e -o LogLevel=DEBUG1 -p 2222 >/tmp/sshd.log 2>&1 &
  sshd_pid=$!
  trap "kill $sshd_pid 2>/dev/null || true" EXIT HUP INT TERM

  # Positive control: server starts and accepts a valid public key.
  connected=false
  n=0
  while [ "$n" -lt 8 ]; do
    if ssh -F /dev/null -vv -n \
      -o StrictHostKeyChecking=no -o UserKnownHostsFile=/dev/null \
      -o IdentityAgent=none -o IdentitiesOnly=yes \
      -o ConnectTimeout=2 -o BatchMode=yes \
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
    echo "--- client diagnostic ---" >&2
    tail -n 85 /tmp/pubkey.log >&2
    echo "--- server diagnostic ---" >&2
    tail -n 85 /tmp/sshd.log >&2
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
