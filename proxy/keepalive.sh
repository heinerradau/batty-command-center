#!/bin/bash
# BCC Keepalive V2 — Proxy + Funnel-Watchdog
LOG="/tmp/bcc-proxy-keepalive.log"
PROXY_PID_FILE="/tmp/bcc-proxy.pid"
SOCKET="/tmp/tailscale.sock"

running_proxy() {
    pgrep -f "python3.*bcc-proxy.py" > /dev/null 2>&1
}

running_funnel() {
    tailscale --socket="$SOCKET" funnel status 2>/dev/null | grep -q "Funnel on"
}

start_proxy() {
    echo "[$(date)] Starting BCC Proxy..." >> "$LOG"
    cd /home/clawbox/bcc
    nohup python3 bcc-proxy.py >> /tmp/bcc-proxy.log 2>&1 & disown
    echo $! > "$PROXY_PID_FILE"
    sleep 2
    if curl -s --max-time 5 http://localhost:8888/health > /dev/null 2>&1; then
        echo "[$(date)] Proxy healthy" >> "$LOG"
    else
        echo "[$(date)] WARN: Proxy started but not responding" >> "$LOG"
    fi
}

start_funnel() {
    echo "[$(date)] Restoring Tailscale Funnel..." >> "$LOG"
    tailscale --socket="$SOCKET" serve --https=443 off 2>/dev/null
    sleep 1
    tailscale --socket="$SOCKET" funnel --bg 8888 2>/dev/null
    sleep 2
    if tailscale --socket="$SOCKET" funnel status 2>/dev/null | grep -q "Funnel on"; then
        echo "[$(date)] Funnel restored" >> "$LOG"
    else
        echo "[$(date)] WARN: Funnel restore failed" >> "$LOG"
    fi
}

if ! running_proxy; then
    echo "[$(date)] Proxy down — restarting..." >> "$LOG"
    start_proxy
fi

if ! running_funnel; then
    echo "[$(date)] Funnel down — restoring..." >> "$LOG"
    start_funnel
fi
