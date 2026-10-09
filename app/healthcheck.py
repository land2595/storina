"""Healthcheck Docker: il processo aggiorna /data/heartbeat almeno ogni 15 s."""
import os
import sys
import time

path = os.path.join(os.getenv("DATA_DIR", "/data"), "heartbeat")
max_age = int(os.getenv("HEALTH_MAX_AGE", "180"))
try:
    with open(path, encoding="utf-8") as f:
        age = time.time() - int(f.read().strip())
except (OSError, ValueError):
    sys.exit(1)
sys.exit(0 if age < max_age else 1)
