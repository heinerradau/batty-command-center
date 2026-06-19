# Contributing to Batty Command Center

Cool that you want to help! Here's how.

## How to Contribute

### 🐛 Reporting Bugs
Open an issue. Include:
- What you did
- What you expected
- What happened instead
- BCC version (shown in the bottom-right corner)
- Browser & OS

### 💡 Feature Requests
Open an issue with the `enhancement` label. Describe what you want and why.

### 🔧 Pull Requests

1. **Fork** the repo
2. Create a branch: `feature/your-feature` or `fix/your-fix`
3. Make your changes
4. **Test** that it works
5. Open a Pull Request against `main`

### Code Style

- **Frontend (index.html):** Vanilla JS. Keep it readable. No framework unless absolutely justified.
- **Backend (bcc-proxy.py):** Python 3. Stdlib where possible. Add comments for non-obvious logic.
- **Format:** 4-space indent. Keep lines under 120 chars.

### Before submitting a PR

- Does your change work in Firefox AND Chrome?
- Does it handle the case where no workspace is configured?
- No console errors on load
- No secrets, no personal data, no hardcoded paths (use env vars)

### Project Structure

```
batty-command-center/
├── frontend/
│   ├── index.html          # Main app (everything is here)
│   ├── chart.umd.js        # Chart.js (don't modify)
│   ├── manifest.json       # PWA manifest
│   ├── service-worker.js   # PWA service worker
│   └── icons/              # PWA icons
├── proxy/
│   ├── bcc-proxy.py        # Backend server
│   ├── bcc-proxy.service   # systemd unit
│   ├── start-proxy.sh      # Quick start script
│   └── keepalive.sh        # Auto-restart script
├── docs/
│   └── DEPLOY.md           # Deployment guide
├── README.md
├── LICENSE
└── CONTRIBUTING.md
```

### What NOT to contribute
- Personal project data (`data.js`, `data.json`)
- Secrets, API keys, tokens
- Hardcoded system paths
- Features that require internet APIs without a fallback

## Development Setup

```bash
# Clone your fork
git clone https://github.com/YOUR_USERNAME/batty-command-center.git
cd batty-command-center

# Create a test workspace
mkdir -p /tmp/bcc-test/projects/test-project

# Set up environment
export BCC_STATIC_DIR=$(pwd)/frontend
export BCC_WORKSPACE=/tmp/bcc-test

# Run the proxy
cd proxy && python3 bcc-proxy.py

# Open http://localhost:8888
```

BCC works best with an [OpenClaw](https://docs.openclaw.ai) workspace, but you can test basic functionality with just the proxy and a few project folders.

---

Thanks for contributing! 🦇
