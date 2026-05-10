import os
import sys
import requests
from pathlib import Path
from dotenv import load_dotenv
import logging as logger

load_dotenv()

BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.json"

BASE_URL = "http://127.0.0.1:8765"


def load_config():
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        cfg = f.read()

    import json

    cfg = json.loads(cfg)

    env_token = os.environ.get("SUNSHINE_AUDITOR_TOKEN")

    if env_token:
        cfg["api_token"] = env_token

    if not cfg.get("api_token"):
        raise RuntimeError("API token not configured")

    return cfg


CONFIG = load_config()
HEADERS = {"X-API-Token": CONFIG["api_token"]}


def call(action: str):
    url = f"{BASE_URL}/{action}"

    try:
        r = requests.post(url, headers=HEADERS, timeout=5)

        print(f"{action.upper()} -> {r.status_code}")
        print(r.text)

        r.raise_for_status()

    except Exception as e:
        print(f"Request failed: {e}")
        sys.exit(1)


def main():
    if len(sys.argv) < 2:
        print("Missing action")
        sys.exit(1)

    action = sys.argv[1].lower()

    valid = {"start", "end", "disconnect", "reconnect"}

    if action not in valid:
        print(f"Invalid action: {action}")
        sys.exit(1)

    call(action)


if __name__ == "__main__":
    main()
