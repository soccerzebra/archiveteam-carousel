#!/usr/bin/env python3
"""Deterministically check that our shared wget-at build can run every HQ-catalog project.

Our image bakes ONE wget-at (built from source in ``docker/Dockerfile``). Projects
are run from their git repo against that shared binary — unlike the real Warrior, which runs
each project's own Docker image with the wget-at that image bundles. So a project can fail on
us in exactly two deterministic ways, both predictable without crawling anything:

  * VERSION: its pipeline's ``find_executable``/``HigherVersion`` demands a wget-at newer than
    ours -> it raises "No usable Wget+At found." at import.
  * FLAGS:   its pipeline passes a wget-at option our build doesn't accept -> wget exits 2
    ("unrecognized option") and the item fails.

For each project that advertises a Docker image we pull it and read, from that image:
  - its own wget-at ``--version`` and ``--help`` option set (what the project was built for),
  - the ``-at.YYYYMMDD`` version minimum(s) its pipeline source requires,
  - the long options its pipeline.py / *.lua actually reference.
Then we compare against our base image's wget-at. Projects with no wget-at (pure scrapers like
urlteam2) are reported N/A. Exit status is non-zero iff some project would genuinely break, so
this is usable as a CI gate: when ArchiveTeam ships a project needing a newer toolchain, the
job goes red with the specific project + reason, and we bump WGET_AT_COMMIT/WGET_AT_VERSION in
docker/Dockerfile (or extract per-project).

Usage:
  python3 tools/check_catalog_compat.py [--catalog FILE] [--base IMAGE] [--json]

``--base`` defaults to our published ``:edge`` image; pass a locally built tag to check a build
before it ships.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import subprocess
import sys
import urllib.request
from pathlib import Path

CATALOG_URL = "https://warriorhq.archiveteam.org/projects.json"
UA = "ArchiveTeam Warrior/0.10.3"
OPT_RE = re.compile(r"--[a-zA-Z0-9][a-zA-Z0-9-]+")
AT_DATE_RE = re.compile(r"at\.(\d{8})\.(\d{2})")
DEFAULT_BASE = "ghcr.io/soccerzebra/archiveteam-carousel:edge"

# Per-image probe: emit one JSON line describing the image's wget-at and pipeline. Runs with
# the image's own python3 (every grab image has one); reads from stdin via `python3 -`.
PROBE = r"""
import glob, json, re, shutil, subprocess
def run(c):
    try:
        return subprocess.run(c, shell=True, capture_output=True, text=True).stdout
    except Exception:
        return ""
binpath = shutil.which("wget-lua") or shutil.which("wget-at")
version, help_opts = "", []
if binpath:
    version = (run(binpath + " --version").splitlines() or [""])[0].strip()
    _help = run(binpath + " --help 2>&1")
    help_opts = sorted(set(re.findall(r"--[a-zA-Z0-9][a-zA-Z0-9-]+", _help)))
src = ""
for f in ["/grab/pipeline.py"] + sorted(glob.glob("/grab/*.lua")):
    try:
        src += open(f, encoding="utf-8", errors="replace").read() + "\n"
    except Exception:
        pass
used_opts = sorted(set(re.findall(r"--[a-zA-Z0-9][a-zA-Z0-9-]+", src)))
req_versions = sorted(set(re.findall(r"at\.\d{8}\.\d{2}", src)))
print(json.dumps({"has_wget": bool(binpath), "version": version,
                  "help_opts": help_opts, "used_opts": used_opts, "req_versions": req_versions}))
"""


# --- pure helpers (unit-tested; no docker/network) --------------------------------------


def at_date(version_or_tag: str) -> tuple[int, int] | None:
    """Extract the comparable ``-at.YYYYMMDD.NN`` build date from a wget-at version string."""
    m = AT_DATE_RE.search(version_or_tag or "")
    return (int(m.group(1)), int(m.group(2))) if m else None


def required_min(req_versions: list[str]) -> tuple[int, int] | None:
    """The newest ``-at`` date a project's pipeline demands (the effective minimum)."""
    dates = [d for d in (at_date(v) for v in req_versions) if d]
    return max(dates) if dates else None


def classify(ours: dict, proj: dict) -> tuple[str, list[str]]:
    """Return (status, reasons). status is PASS / PASS-NOTE / FAIL / N/A."""
    if not proj.get("has_wget"):
        return "N/A", ["no wget-at in image (scraper project)"]
    reasons: list[str] = []
    our_date = at_date(ours.get("version", ""))
    need = required_min(proj.get("req_versions", []))
    if need and our_date and our_date < need:
        reasons.append(
            f"needs wget-at >= at.{need[0]}.{need[1]:02d}, "
            f"ours is at.{our_date[0]}.{our_date[1]:02d}"
        )
    our_opts = set(ours.get("help_opts", []))
    proj_opts = set(proj.get("help_opts", []))
    used = set(proj.get("used_opts", []))
    # options the pipeline references, that the project's OWN wget-at supports, that ours lacks
    missing_used = sorted((used & proj_opts) - our_opts)
    if missing_used:
        reasons.append("uses option(s) our build lacks: " + ", ".join(missing_used))
    if reasons:
        return "FAIL", reasons
    # informational: project's build has options ours lacks, but the pipeline never uses them
    unused_gap = sorted(proj_opts - our_opts - used)
    if unused_gap:
        return "PASS-NOTE", [f"build has {len(unused_gap)} option(s) ours lacks, none used"]
    return "PASS", []


# --- docker / catalog orchestration -----------------------------------------------------


def probe_image(image: str) -> dict:
    """Pull+inspect one image; returns the probe dict, or {'error': ...} on failure."""
    try:
        res = subprocess.run(
            [
                "docker",
                "run",
                "-i",
                "--rm",
                "--platform",
                "linux/amd64",
                "--entrypoint",
                "python3",
                image,
                "-",
            ],
            input=PROBE,
            text=True,
            capture_output=True,
            timeout=600,
        )
    except subprocess.TimeoutExpired:
        return {"error": "probe timed out"}
    if res.returncode != 0:
        return {"error": (res.stderr or res.stdout).strip().splitlines()[-1:] or ["docker failed"]}
    try:
        return json.loads(res.stdout.strip().splitlines()[-1])
    except (json.JSONDecodeError, IndexError):
        return {"error": f"unparseable probe output: {res.stdout[-200:]!r}"}


def load_catalog(path: str | None) -> list[dict]:
    if path:
        return json.loads(Path(path).read_text()).get("projects", [])
    req = urllib.request.Request(CATALOG_URL, headers={"User-Agent": UA, "Accept-Encoding": "gzip"})
    with urllib.request.urlopen(req, timeout=30) as r:
        raw = r.read()
        if r.headers.get("Content-Encoding") == "gzip" or raw[:2] == b"\x1f\x8b":
            raw = gzip.decompress(raw)
    return json.loads(raw).get("projects", [])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", help="read catalog JSON from a file instead of HQ (for tests)")
    ap.add_argument(
        "--base",
        default=DEFAULT_BASE,
        help=f"image whose wget-at we check (default: {DEFAULT_BASE})",
    )
    ap.add_argument("--json", action="store_true", help="emit machine-readable JSON results")
    args = ap.parse_args()

    base = args.base
    print(f"Our shared wget-at comes from image: {base}", file=sys.stderr)
    ours = probe_image(base)
    if ours.get("error") or not ours.get("has_wget"):
        print(f"ERROR: could not read wget-at from base {base}: {ours}", file=sys.stderr)
        return 2
    print(f"  our wget-at: {ours['version']} ({len(ours['help_opts'])} options)\n", file=sys.stderr)

    projects = load_catalog(args.catalog)
    seen: dict[str, dict] = {}
    rows, failures = [], []
    for p in projects:
        image = p.get("docker")
        name = p.get("name", "?")
        if not image:
            rows.append((name, "SKIP", ["no docker image advertised"]))
            continue
        proj = seen.get(image) or seen.setdefault(image, probe_image(image))
        if proj.get("error"):
            rows.append((name, "ERROR", [str(proj["error"])]))
            failures.append(name)
            continue
        status, reasons = classify(ours, proj)
        rows.append((name, status, reasons))
        if status == "FAIL":
            failures.append(name)

    width = max((len(n) for n, _, _ in rows), default=10)
    for name, status, reasons in rows:
        detail = ("  — " + "; ".join(reasons)) if reasons else ""
        print(f"{name:<{width}}  {status:<9}{detail}")

    if args.json:
        print(
            json.dumps(
                {
                    "base": base,
                    "our_version": ours["version"],
                    "results": [{"project": n, "status": s, "reasons": r} for n, s, r in rows],
                    "failures": failures,
                },
            )
        )
    print(f"\n{len(rows)} projects checked, {len(failures)} incompatible.", file=sys.stderr)
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
