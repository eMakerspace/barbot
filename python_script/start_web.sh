#!/bin/bash
# Start the BarBot web control panel
cd "$(dirname "$0")"
source venv/bin/activate
echo "Starting BarBot Control Panel at http://$(hostname -I | awk '{print $1}'):7777"
echo "Logs: /tmp/barbot.log"
python barbot_web.py 2>&1 | tee /tmp/barbot.log
