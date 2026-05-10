import json
import os
import sys
from pathlib import Path

import requests


BASE_DIR = Path(__file__).resolve().parents[1]
CONFIG_PATH = BASE_DIR / "config.json"
BASE_URL = "http://127.0.0.1:8765"
POST_ACTIONS = {"start", "end", "disconnect", "reconnect"}
GET_ACTIONS = {"state", "health"}


def load_config() -> dict:
    try:
        config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        config = {}

    env_token = os.environ.get("SUNSHINE_AUDITOR_TOKEN")
    if env_token:
        config["api_token"] = env_token
    return config


def call(action: str) -> None:
    config = load_config()
    headers = {}
    if config.get("api_token"):
        headers["X-API-Token"] = config["api_token"]

    url = f"{BASE_URL}/{action}"
    method = requests.get if action in GET_ACTIONS else requests.post
    response = method(url, headers=headers, timeout=5)

    print(f"{action.upper()} -> {response.status_code}")
    print(response.text)
    response.raise_for_status()


def main() -> None:
    if len(sys.argv) < 2:
        print("Missing action")
        sys.exit(1)

    action = sys.argv[1].lower()
    if action not in POST_ACTIONS | GET_ACTIONS:
        print(f"Invalid action: {action}")
        sys.exit(1)

    try:
        call(action)
    except requests.RequestException as exc:
        print(f"Request failed: {exc}")
        sys.exit(1)


if __name__ == "__main__":
    main()
