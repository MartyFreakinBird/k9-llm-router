#!/usr/bin/env bash
# Reload GitHub SSH key on sandbox boot — single clean secret, no fragment hacks.
source /app/.agents/.env 2>/dev/null || source ~/.agents/.env 2>/dev/null || true

mkdir -p ~/.ssh && chmod 700 ~/.ssh

{
  echo "-----BEGIN OPENSSH PRIVATE KEY-----"
  echo "${VECTOS_GITHUB_SSH_KEY}" | fold -w 70
  echo "-----END OPENSSH PRIVATE KEY-----"
} > ~/.ssh/id_ed25519
chmod 600 ~/.ssh/id_ed25519
ssh-keygen -y -f ~/.ssh/id_ed25519 > ~/.ssh/id_ed25519.pub 2>/dev/null

cat > ~/.ssh/config << EOF
Host github.com
  HostName github.com
  User git
  IdentityFile ~/.ssh/id_ed25519
  StrictHostKeyChecking no
EOF
chmod 600 ~/.ssh/config

pkill ssh-agent 2>/dev/null
eval "$(ssh-agent -s)" > /dev/null
ssh-add ~/.ssh/id_ed25519 2>/dev/null
echo "[vectos] SSH key loaded: $(ssh-keygen -lf ~/.ssh/id_ed25519.pub 2>/dev/null)"
