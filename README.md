# 🦇 Batty Command Center (BCC)

**A local-first AI agent task manager & project dashboard for OpenClaw/ClawBox.**

BCC gives you visual control over what your AI agents are doing — task queues, project status, calendar planning, and marketing analytics — all in one browser-based interface backed by real Markdown files your agents can read and write.

> Built and battle-tested on a ClawBox since June 2026. Now open source.

## What it looks like

```
┌─ Sidebar ────────────────────┬─ Main View ──────────────────────────────┐
│ 🔴 Active Projects           │  ▸ NYC Art Map Player                    │
│   nyc-art-map-player  [~]   │    ├── Fix WebGL rendering       [x]     │
│   morning-briefing    [~]   │    ├── Add coordinate overlay   [~] ← now│
│ 💡 Ideas                     │    └── Mobile responsive test   [ ]     │
│   stadt-akquise               │                                          │
│ ✅ Done                       │  📅 Calendar (sticky, drag-drop)         │
│   widerrufsbutton             │  ┌──────┬──────┬──────┐                │
│                              │  │  MON │  TUE │  WED  │                │
│  📊 Dashboard                │  │  ○   │ Task │      │                │
│  ── Ads ───────              │  └──────┴──────┴──────┘                │
│  FB: €42/9 Leads              │                                          │
│  Google: €22/5K Impr         │  💬 Task Chat                            │
│  ── Klaviyo ───              │  ─────────────────────────────────────  │
│  NYC List: +20 today         │  AlphaBatty: Frame extraction done.      │
└──────────────────────────────┴──────────────────────────────────────────┘
```

## Key Features

### 🎯 AI-Native Task Management
- **Markdown-driven** — Tasks live in `TASKS.md` files your agents already edit
- **`[~]` in-progress marker** — One character tells BCC "this is being worked on right now"
- **Live status dots** — Red = needs you, Yellow = batty working, Blue = done, Green = paused
- **Blink animation** — Active tasks pulse in the sidebar. You always see what's running.

### 📅 Drag-and-Drop Calendar
- Sticky header during scroll
- Drag tasks from anywhere (status boxes, sidebar) onto calendar days
- Task chips with clean text truncation ("…")

### 💬 Per-Task Chat
- Every task gets its own chat thread
- BCC can relay messages to/from your AI agents
- Review, steer, or pause agent work right from the UI

### 📊 Marketing Dashboard (optional)
- Facebook & Google Ads: impressions, clicks, cost, leads
- Klaviyo: email series performance, list growth
- Auto-updated via cron-based nightly pipeline

### 🔍 Smart Features
- **Project overview** — See all tasks grouped by project, status, and who's working
- **Collapsible status boxes** — Open/urgent/active/press tasks one click away
- **Heiner/Batty filter** — Show only human tasks or only agent tasks
- **Search** across tasks AND chats
- **Version-aware** — BCC knows its own version history

## How It Works

```
┌──────────────┐     HTTP/WebSocket      ┌──────────────┐
│  index.html  │◄──────────────────────►│  bcc-proxy.py │
│  (Browser)   │    :8888                │  (Python 3)  │
└──────────────┘                         └──────┬───────┘
                                                │
                                    Reads/writes TASKS.md
                                    project files, data.json
                                                
                                          ┌─────┴──────┐
                                          │  Workspace  │
                                          │  projects/  │
                                          │  STATUS.md  │
                                          │  TASKS.md   │
                                          │  data.json  │
                                          └────────────┘
```

- **Frontend:** Single-page HTML/CSS/JS app (206 KB). No framework, no build step.
- **Backend:** Python 3 proxy (~70 KB). Serves the frontend and provides a KV store, task API, and chat relay.
- **Storage:** Everything is filesystem-based. Project state lives in your OpenClaw workspace.

## Quick Start

```bash
# 1. Clone
git clone https://github.com/heinerradau/batty-command-center.git
cd batty-command-center

# 2. Set your workspace path
export BCC_STATIC_DIR=/path/to/batty-command-center/frontend
export BCC_WORKSPACE=/path/to/your/openclaw/workspace

# 3. Start the proxy
cd proxy
python3 bcc-proxy.py

# 4. Open in browser
# http://localhost:8888
```

Or use systemd:
```bash
cp proxy/bcc-proxy.service ~/.config/systemd/user/
# Edit the service file to set your paths
systemctl --user enable --now bcc-proxy
```

## Requirements

- Python 3.9+
- A modern browser (Chrome, Firefox, Edge, Safari)
- An OpenClaw workspace with project folders (optional but recommended)

## Architecture

| Component | File | Size | Role |
|-----------|------|------|------|
| Frontend | `frontend/index.html` | ~206 KB | Single-page app: sidebar, project view, calendar, chat, dashboard |
| Backend | `proxy/bcc-proxy.py` | ~70 KB | HTTP server, KV store, task API, WebSocket relay |
| Icons | `frontend/icons/` | ~12 KB | PWA icons (180/192/512) |
| PWA | `frontend/manifest.json`, `service-worker.js` | ~2 KB | Install as standalone app |

The frontend uses Chart.js for the dashboard (`chart.umd.js`, MIT-licensed third-party library).

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md). Issues and pull requests welcome!

Some ideas:
- Weekly calendar view with hour grid
- Editable summary boxes
- Dark/light theme toggle
- Multi-language support
- Plugin system for custom dashboards

## License

MIT — see [LICENSE](LICENSE).

---

Built with 🦇 by [Heiner Radau](https://www.heinerradau.de). Inspired by the need to keep track of what AI agents are actually doing.
