#!/usr/bin/env python3
"""Live metrics viewer for the Sunshine daemon.

Usage:
    python tools/metrics_cli.py [--url URL] [--window SECONDS] [--interval SECONDS]

Works in PowerShell, zsh, bash, or any terminal with Python 3.9+.
No dependencies beyond stdlib.
"""
import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error


def fetch(url: str) -> dict:
    with urllib.request.urlopen(url, timeout=3) as resp:
        return json.loads(resp.read())


def clear():
    os.system("cls" if os.name == "nt" else "clear")


ANSI_RED    = "\033[91m"
ANSI_YELLOW = "\033[93m"
ANSI_GREEN  = "\033[92m"
ANSI_BLUE   = "\033[94m"
ANSI_GRAY   = "\033[90m"
ANSI_BOLD   = "\033[1m"
ANSI_RESET  = "\033[0m"


def color_ms(val: float) -> str:
    s = f"{val:>8.1f}"
    if val > 50:
        return ANSI_RED + s + ANSI_RESET
    if val > 20:
        return ANSI_YELLOW + s + ANSI_RESET
    return ANSI_GREEN + s + ANSI_RESET


def render_stats(stats: dict, window: int) -> None:
    if not stats:
        print(f"{ANSI_GRAY}  (no samples in the last {window}s){ANSI_RESET}")
        return

    header = f"{'Operation':<30} {'Count':>6} {'Avg ms':>8} {'P50 ms':>8} {'P99 ms':>8} {'Max ms':>8}"
    print(ANSI_BOLD + header + ANSI_RESET)
    print(ANSI_GRAY + "-" * len(header) + ANSI_RESET)

    for op, s in sorted(stats.items(), key=lambda x: x[1]["max_ms"], reverse=True):
        print(
            f"{ANSI_BLUE}{op:<30}{ANSI_RESET}"
            f" {s['count']:>6}"
            f" {color_ms(s['avg_ms'])}"
            f" {color_ms(s['p50_ms'])}"
            f" {color_ms(s['p99_ms'])}"
            f" {color_ms(s['max_ms'])}"
        )


def render_spikes(samples: list, threshold_ms: float = 20.0) -> None:
    spikes = [s for s in samples if s["ms"] >= threshold_ms][-20:]
    if not spikes:
        print(f"{ANSI_GRAY}  (no spikes >{threshold_ms:.0f}ms in window){ANSI_RESET}")
        return

    for s in reversed(spikes):
        ts = time.strftime("%H:%M:%S", time.localtime(s["ts"]))
        details = f"  {s['d']}" if s.get("d") else ""
        ms_str = f"{s['ms']:.1f}ms"
        color = ANSI_RED if s["ms"] > 50 else ANSI_YELLOW
        print(f"  {ANSI_GRAY}{ts}{ANSI_RESET}  {ANSI_BLUE}{s['op']:<30}{ANSI_RESET}  {color}{ms_str:>8}{ANSI_RESET}{ANSI_GRAY}{details}{ANSI_RESET}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Sunshine daemon live metrics")
    parser.add_argument("--url", default="http://127.0.0.1:8765", help="Daemon base URL")
    parser.add_argument("--window", type=int, default=300, metavar="SECS", help="Lookback window in seconds (default: 300)")
    parser.add_argument("--interval", type=float, default=2.0, metavar="SECS", help="Refresh interval in seconds (default: 2)")
    args = parser.parse_args()

    base = args.url.rstrip("/")

    print(f"Connecting to {base} … (Ctrl+C to quit)")

    while True:
        try:
            stats_data = fetch(f"{base}/metrics?window={args.window}")
            recent_data = fetch(f"{base}/metrics/recent?window={args.window}&limit=200")

            clear()
            now = time.strftime("%H:%M:%S")
            print(f"{ANSI_BOLD}Sunshine Daemon Metrics{ANSI_RESET}  {ANSI_GRAY}{now}  window={args.window}s  refresh={args.interval}s{ANSI_RESET}\n")

            print(f"{ANSI_BOLD}Per-operation stats{ANSI_RESET}  {ANSI_GRAY}(green <20ms  yellow 20-50ms  red >50ms){ANSI_RESET}")
            render_stats(stats_data.get("stats", {}), args.window)

            print(f"\n{ANSI_BOLD}Recent spikes (>20ms){ANSI_RESET}")
            render_spikes(recent_data.get("samples", []))

        except urllib.error.URLError as exc:
            clear()
            print(f"{ANSI_RED}Cannot reach daemon at {base}: {exc.reason}{ANSI_RESET}")
            print("Is the service running?  nssm status StartStreaming")
        except KeyboardInterrupt:
            print("\nBye.")
            sys.exit(0)
        except Exception as exc:
            print(f"{ANSI_RED}Error: {exc}{ANSI_RESET}")

        try:
            time.sleep(args.interval)
        except KeyboardInterrupt:
            print("\nBye.")
            sys.exit(0)


if __name__ == "__main__":
    main()
