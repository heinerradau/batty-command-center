# BCC Deployment Guide

## Prerequisites

- Python 3.9+
- A modern browser
- Optional but recommended: [OpenClaw](https://docs.openclaw.ai) workspace

## Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `BCC_PORT` | `8888` | Port the proxy listens on |
| `BCC_STATIC_DIR` | *auto-detect* | Path to the `frontend/` directory |
| `BCC_WORKSPACE` | `/tmp/bcc-workspace` | Path to your OpenClaw workspace |
| `BCC_AGENT` | `main` | OpenClaw agent ID for chat relay |
| `BCC_THINKING` | `medium` | Thinking level for relayed messages |

## Quick Deploy (standalone)

```bash
git clone https://github.com/heinerradau/batty-command-center.git
cd batty-command-center

export BCC_STATIC_DIR=$(pwd)/frontend
export BCC_WORKSPACE=/path/to/your/workspace

cd proxy
python3 bcc-proxy.py
# → http://localhost:8888
```

## systemd Service (Linux)

```bash
# Copy service file
cp proxy/bcc-proxy.service ~/.config/systemd/user/

# Edit paths
nano ~/.config/systemd/user/bcc-proxy.service
# Change BCC_STATIC_DIR and BCC_WORKSPACE to match your setup

# Enable and start
systemctl --user daemon-reload
systemctl --user enable --now bcc-proxy
```

### Sample systemd unit
```ini
[Unit]
Description=BCC Backend Proxy (Batty Command Center)
After=network.target

[Service]
Type=simple
Environment="BCC_PORT=8888"
Environment="BCC_STATIC_DIR=/home/user/batty-command-center/frontend"
Environment="BCC_WORKSPACE=/home/user/.openclaw/workspace"
ExecStart=/usr/bin/python3 /home/user/batty-command-center/proxy/bcc-proxy.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=default.target
```

## With ClawBox / OpenClaw

BCC is designed to integrate with OpenClaw. Your agents can read and write `TASKS.md` files directly:

```markdown
## 🔴 Today
- [x] Update product prices
- [~] Fix NYT rendering bug  ← BCC shows this as active
- [ ] Write newsletter draft

## 💡 Ideas
- [ ] Add Paris to the city map
```

BCC reads the same project files your agents use. No duplication, no sync issues.

### Task Markers BCC understands

| Marker | BCC shows as |
|--------|-------------|
| `- [~] task` | 🟡 In Progress (blinks in sidebar) |
| `- [x] task` | ✅ Done |
| `- [ ] task` | Depends on section (🔴 Today, 💡 Idea, etc.) |

## PWA Installation

BCC is a Progressive Web App. Open it in Chrome/Edge and click "Install" in the address bar (or Add to Home Screen on mobile). It'll work offline for cached content and feel like a native app.

## Updating

```bash
cd batty-command-center
git pull origin main
# If the proxy is running via systemd:
systemctl --user restart bcc-proxy
```
