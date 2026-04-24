#!/bin/bash
cd /Users/chengang/.openclaw/workspace/projects/tinder-automation
rm -f /Users/chengang/.tinder-automation/browser-profile/SingletonLock 2>/dev/null
python3 run_watcher.py -y 2>&1
