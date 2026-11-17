#!/usr/bin/env python3
"""Capture a REAL Universal Tracker exchange into the test fixtures — responsibly.

It claims one item batch with POST /request, then IMMEDIATELY returns it with POST
/release, so no work item is left dangling on the production tracker. It writes the raw
200 body to tests/fixtures/<project>_tracker/request_response.json so the contract test
is pinned to the live wire format instead of our reading of it.

Usage:
    python scripts/capture_tracker_exchange.py --downloader YOUR_HANDLE
    python scripts/capture_tracker_exchange.py --downloader YOUR_HANDLE \
        --tracker https://legacy-api.arpa.li/urls --version 20261027.01 --multi 1

Only --downloader is required. The handle is YOUR public leaderboard identity and is the
only thing sent that identifies you; nothing else about your machine is transmitted.
Stdlib only, so it runs without installing anything.
"""

from __future__ import annotations

import argparse
import json
import pathlib
import sys
import urllib.error
import urllib.request

REPO = pathlib.Path(__file__).resolve().parents[1]
USER_AGENT = "ArchiveTeam Warrior/0.10.3 Warrior"  # identical to the ArchiveTeam Warrior


def post(url: str, body: dict) -> tuple[int, str]:
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--downloader", required=True, help="your leaderboard handle")
    ap.add_argument("--tracker", default="https://legacy-api.arpa.li/urls")
    ap.add_argument(
        "--version",
        default="20261027.01",
        help="project pipeline VERSION (a stale value may get a 455)",
    )
    ap.add_argument("--multi", type=int, default=1)
    ap.add_argument("--project", default="urls", help="fixture dir name")
    args = ap.parse_args()

    base = f"{args.tracker.rstrip('/')}/multi={args.multi}"
    out_dir = REPO / "tests" / "fixtures" / f"{args.project}_tracker"
    out_dir.mkdir(parents=True, exist_ok=True)

    print(f"POST {base}/request  (downloader={args.downloader}, version={args.version})")
    code, body = post(f"{base}/request", {"downloader": args.downloader, "version": args.version})
    print(f"  -> HTTP {code}")
    if code != 200:
        print(f"  body: {body[:300]}")
        print("No item claimed; nothing to release. (404 = no items available right now.)")
        return 0 if code == 404 else 1

    (out_dir / "request_response.json").write_text(body.rstrip() + "\n")
    print(f"  saved {out_dir / 'request_response.json'}")

    data = json.loads(body)
    items = data.get("items") or [data["item_name"]]
    print(f"Releasing {len(items)} item(s) back to the tracker: {items}")
    rcode, rbody = post(
        f"{args.tracker.rstrip('/')}/release",
        {"downloader": args.downloader, "version": args.version, "items": items},
    )
    print(f"  -> HTTP {rcode}: {rbody[:200]}")
    if rcode != 200:
        print(
            "WARNING: release did not return 200 — verify the item was returned, "
            "so it isn't left claimed under your handle.",
            file=sys.stderr,
        )
        return 1
    print("Done. Item returned; no dangling claim.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
