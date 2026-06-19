#!/bin/bash
# BCC: Keep proxy + tunnel alive
# Start proxy if not running
if ! curl -s http://localhost:8888/health > /dev/null 2>&1; then
  echo "[$(date)] Starting proxy..."
  cd /home/clawbox/bcc
  PATH="/home/clawbox/.npm-global/bin:$PATH" BCC_AGENT=main BCC_THINKING=low python3 -u bcc-proxy.py &
  sleep 3
fi

# Start tunnel if not running  
if ! pgrep -f "cloudflared tunnel --url http://localhost:8888" > /dev/null; then
  echo "[$(date)] Starting tunnel..."
  cloudflared tunnel --url http://localhost:8888 &
  sleep 6
fi

echo "[$(date)] Health: $(curl -s http://localhost:8888/health)"
