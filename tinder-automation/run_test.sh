#!/bin/bash
cd /Users/chengang/.openclaw/workspace/projects/tinder-automation
rm -f /Users/chengang/.tinder-automation/browser-profile/SingletonLock
python3 run_queue.py 2>&1
