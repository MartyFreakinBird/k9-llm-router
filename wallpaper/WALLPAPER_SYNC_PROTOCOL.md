# K-9 Wallpaper Sync Protocol
> Canonical source: ~/k9-llm-router/wallpaper/wallpaper.html
> Runtime target: C:\K9\vectos\k9-wallpaper\wallpaper.html (OpenClaw hot-reloads on write)

---

## One-Time Setup: Absorb the Vectos folder into the repo

Run this once in WSL2 to eliminate the separate Vectos copy forever:

```bash
# 1. Back up the Vectos folder (safety)
cp -r /mnt/c/K9/vectos/k9-wallpaper /mnt/c/K9/vectos/k9-wallpaper.bak

# 2. Overwrite the Vectos wallpaper with the canonical repo version
cp ~/k9-llm-router/wallpaper/wallpaper.html /mnt/c/K9/vectos/k9-wallpaper/wallpaper.html

# 3. Create a deploy alias in your shell profile (~/.bashrc or ~/.zshrc)
echo 'alias k9wall-deploy="cp ~/k9-llm-router/wallpaper/wallpaper.html /mnt/c/K9/vectos/k9-wallpaper/wallpaper.html && echo DEPLOYED"' >> ~/.bashrc
source ~/.bashrc
```

After this: every time Base44 or Claude updates `wallpaper.html` and you pull,
just run: `k9wall-deploy`

---

## Standard Update Flow (every sprint)

### Option A — Claude Code in project folder (recommended, token-efficient)
```bash
cd ~/k9-llm-router/wallpaper
claude   # or: claude --print "Read WALLPAPER_UPDATE_PROMPT.md and apply [SPRINT NAME] additions"
k9wall-deploy
```

### Option B — Base44 agent (Vectos)
Tell Vectos: "Apply [sprint] changes to the wallpaper using WALLPAPER_UPDATE_PROMPT.md"
After Base44 pushes to GitHub:
```bash
cd ~/k9-llm-router && git pull
k9wall-deploy
```

### Option C — Manual human edit
Edit `~/k9-llm-router/wallpaper/wallpaper.html` directly.
Then: `k9wall-deploy && cd ~/k9-llm-router && git add -A && git commit -m "feat(wallpaper): [desc]" && git push`

---

## Future: Make it automatic (optional)

Add a git post-merge hook so pulling the repo auto-deploys the wallpaper:

```bash
cat > ~/k9-llm-router/.git/hooks/post-merge << 'EOF'
#!/bin/bash
CHANGED=$(git diff-tree -r --name-only --no-commit-id ORIG_HEAD HEAD)
if echo "$CHANGED" | grep -q "wallpaper/wallpaper.html"; then
  cp ~/k9-llm-router/wallpaper/wallpaper.html /mnt/c/K9/vectos/k9-wallpaper/wallpaper.html
  echo "[k9wall] Wallpaper auto-deployed to OpenClaw ✓"
fi
EOF
chmod +x ~/k9-llm-router/.git/hooks/post-merge
```

After this hook is installed, `git pull` will auto-deploy if wallpaper.html changed.

---

## File Locations Reference

| Purpose | Path |
|---------|------|
| Canonical source | `~/k9-llm-router/wallpaper/wallpaper.html` |
| Update instructions | `~/k9-llm-router/wallpaper/WALLPAPER_UPDATE_PROMPT.md` |
| Runtime (OpenClaw) | `C:\K9\vectos\k9-wallpaper\wallpaper.html` |
| Sandbox backup | `C:\K9\vectos\k9-wallpaper.bak\wallpaper.html` |

---

## Deprecation Plan for C:\K9\vectos\k9-wallpaper\

The Vectos folder is now a **read-only deploy target**, not a source.
- Never edit `wallpaper.html` directly in the Vectos folder
- All edits happen in `~/k9-llm-router/wallpaper/`
- The Vectos folder's other files (background.png, thumbnail.png) stay as-is
