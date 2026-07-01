# K-9 Command Surface — Octos Wallpaper Package

## File Structure

```
k9-wallpaper/
├── manifest.json       ← Octos metadata (name, version, tags)
├── wallpaper.html      ← Main interactive dashboard
├── thumbnail.png       ← Generate from a screenshot (400×225px)
├── background.png      ← Your static zone-layout image (Figma/Canva export)
└── README.md
```

## Install in Octos

1. Place this folder somewhere permanent:
   `C:\Users\<you>\Documents\OctosWallpapers\k9-wallpaper\`

2. Open Octos → **Add Wallpaper** → **From Folder**

3. Select the `k9-wallpaper\` folder — Octos reads `manifest.json` automatically.

4. Set as active wallpaper.

## Background Image

`background.png` is your static zone layout. It sits behind all HTML widgets.
Design it in Figma/Canva with labeled regions matching these absolute positions:

| Zone          | Approx position        |
|---------------|------------------------|
| Agent Status  | Top-left column        |
| Tailscale     | Bottom-left column     |
| Topology      | Center-top             |
| Quant/Crypto  | Center-bottom          |
| Quick Launch  | Top-right column       |
| Log Stream    | Bottom-right column    |

## Wiring Real Data

### Live agent health (replace simulated log):
```js
const ws = new WebSocket('ws://localhost:8080/ws/events');
ws.onmessage = e => {
  const d = JSON.parse(e.data);
  pushLog(d.source, d.level === 'ok' ? 'l-ok' : 'l-er', d.message);
};
```

### Agent status dots (poll your FastAPI health endpoints):
```js
async function checkAgents() {
  const agents = [
    { id: 'orch', url: 'http://localhost:8080/swarm/health' },
    { id: 'llm',  url: 'http://localhost:8888/swarm/health' },
    { id: 'quant',url: 'http://localhost:9001/swarm/health' },
  ];
  for (const a of agents) {
    try {
      const r = await fetch(a.url, { signal: AbortSignal.timeout(2000) });
      // update dot color based on r.ok
    } catch { /* mark offline */ }
  }
}
setInterval(checkAgents, 15000);
```

### Tailscale peer status:
```js
// Call your local Tailscale API proxy or use the CLI output
// via a thin FastAPI wrapper on the orchestrator
fetch('http://localhost:8080/api/tailscale/peers').then(...)
```

## Octos System Stats

Octos injects stats via `window.octos.system.onUpdate()` when running inside
the wallpaper engine. The CPU/RAM/VRAM fields in the Tailscale panel will
auto-populate. No action needed.

## Performance Tips

- Octos: enable **Pause on Fullscreen** (set in manifest.json ✓)
- The Monte Carlo sparklines regenerate every 5s — reduce to 10s if needed
- CoinGecko polls every 45s — safe for free tier, adjust in `fetchPrices()`
- Font import is Google Fonts CDN — cache locally if offline use is needed

## Thumbnail

After first run, take a screenshot and crop to **400×225px**, save as
`thumbnail.png` in this folder. Octos displays it in the wallpaper picker.
