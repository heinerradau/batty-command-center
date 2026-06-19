#!/bin/bash
cd /home/clawbox/bcc
PATH="/home/clawbox/.npm-global/bin:$PATH" BCC_AGENT=main BCC_THINKING=low exec python3 -u bcc-proxy.py
