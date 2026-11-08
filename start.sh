#!/bin/bash
cd /app
export PYTHONUNBUFFERED=1
nohup xvfb-run -a python -u main.py --web --port 4295 --host 0.0.0.0 > /app/output.log 2>&1 &
sleep 2
echo "Started" >> /app/output.log
tail -f /dev/null