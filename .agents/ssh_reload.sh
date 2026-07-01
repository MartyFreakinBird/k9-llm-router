#!/usr/bin/env bash
# Reload vectos-base44-k9 SSH key on sandbox boot
source /app/.agents/.env 2>/dev/null || source ~/.agents/.env 2>/dev/null || true

mkdir -p ~/.ssh && chmod 700 ~/.ssh

cat > ~/.ssh/id_ed25519 << KEYEOF
-----BEGIN OPENSSH PRIVATE KEY-----
${SSH_PRIVATE_KEY}
QyNTUxOQAAACCXesb+${SSH_PRIVATE_KEY_2}
${SSH_PRIVATE_KEY_3}+${SSH_PRIVATE_KEY_4}
${SSH_PRIVATE_KEY_5}+${SSH_PRIVATE_KEY_6}
${SSH_PRIVATE_KEY_7}=
-----END OPENSSH PRIVATE KEY-----
KEYEOF

chmod 600 ~/.ssh/id_ed25519

cat > ~/.ssh/config << EOF
Host github.com
  HostName github.com
  User git
  IdentityFile ~/.ssh/id_ed25519
  StrictHostKeyChecking no
EOF
chmod 600 ~/.ssh/config

eval "\$(ssh-agent -s)" 2>/dev/null
ssh-add ~/.ssh/id_ed25519 2>/dev/null
echo "[vectos] SSH key loaded: vectos-base44-k9 → MartyFreakinBird"
