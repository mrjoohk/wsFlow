"""Owned one-shot stdlib actor; no subprocess descendants or provider calls."""
import json
from pathlib import Path
import sys
import threading
import os

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from wsflow.store import Store
from wsflow.writer_admission import WriterUnavailable

watchdog = threading.Timer(10, lambda: os._exit(97))
watchdog.daemon = True
watchdog.start()
try:
    try:
        project = Store(sys.argv[1]).add_project(sys.argv[2])
        print(json.dumps({'outcome': 'saved', 'project': project}), flush=True)
    except WriterUnavailable as error:
        print(json.dumps({'outcome': 'blocked', 'error': str(error)}), flush=True)
finally:
    watchdog.cancel()
