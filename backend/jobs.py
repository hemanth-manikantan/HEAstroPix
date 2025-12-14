#backend/jobs.py
'''Job+Logging manager'''

import threading
from datetime import datetime
import logging

jobs = {}
logs = []

def log(msg):
    ts = datetime.now().strftime("%H:%M:%S")
    logs.append(f"[{ts}] {msg}")
    if len(logs) > 300:
        del logs[0]

def start_job(name, target, *args, **kwargs):
    def wrapper():
        jobs[name]["status"] = "running"
        log(f"{name}: started")
        try:
            result = target(*args, **kwargs)
            jobs[name]["status"] = "done"
            jobs[name]["result"] = result
            log(f"{name}: finished successfully")
        except Exception as e:
            jobs[name]["status"] = "failed"
            jobs[name]["error"] = str(e)
            log(f"{name}: FAILED → {e}")

    jobs[name] = {
        "status": "queued",
        "started": datetime.now()
    }

    t = threading.Thread(target=wrapper, daemon=True)
    t.start()