"""Remote project hints: a small JSON file we publish so clients learn which projects are
likely inactive (tracker out of items) without needing an image update.

The hints only change how the UI presents projects. A listed project still appears and can
still be enabled, and a hint never stops a running worker. Failure is non-destructive: if a
fetch fails or returns something we can't parse, we keep the last good copy (on-disk cache,
else the copy bundled in the image) instead of falling back to an empty list.

File format (``hints/hints.json`` in this repo)::

    {
      "version": 1,
      "updated": "2026-11-17",
      "inactive": {"someproject": {"note": "finished 2026-08", "since": "2026-08-30"}}
    }

Unknown top-level keys are ignored, so the file can grow new kinds of hints without breaking
older clients.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import httpx

DEFAULT_HINTS_URL = (
    "https://raw.githubusercontent.com/soccerzebra/archiveteam-carousel/master/hints/hints.json"
)
BUNDLED_HINTS = Path(__file__).resolve().parent.parent / "hints" / "hints.json"
DEFAULT_REFRESH_INTERVAL = 6 * 3600.0
USER_AGENT = "Carousel hints"


def parse_hints(data: object) -> dict[str, dict]:
    """Validate a hints document and return its ``inactive`` map (name -> info dict).

    Raises ValueError on anything malformed, so callers can keep their previous copy.
    """
    if not isinstance(data, dict):
        raise ValueError("hints: top level must be an object")
    version = data.get("version")
    if not isinstance(version, int) or version < 1:
        raise ValueError(f"hints: bad version {version!r}")
    inactive = data.get("inactive", {})
    if not isinstance(inactive, dict):
        raise ValueError("hints: 'inactive' must be an object")
    out: dict[str, dict] = {}
    for name, info in inactive.items():
        info = info if isinstance(info, dict) else {}
        note = info.get("note")
        since = info.get("since")
        out[str(name)] = {
            "note": note if isinstance(note, str) else "",
            "since": since if isinstance(since, str) else "",
        }
    return out


class HintsClient:
    """Fetches the hints file on a slow interval with ETag revalidation and a disk cache.

    ``url=None`` (or empty) disables fetching; the cached or bundled copy is still used.
    """

    def __init__(
        self,
        url: str | None = DEFAULT_HINTS_URL,
        cache_path: Path | None = None,
        bundled_path: Path | None = BUNDLED_HINTS,
        refresh_interval: float = DEFAULT_REFRESH_INTERVAL,
        client: httpx.AsyncClient | None = None,
        clock=time.monotonic,
    ):
        self.url = url or None
        self.cache_path = cache_path
        self.bundled_path = bundled_path
        self.refresh_interval = refresh_interval
        self._client = client
        self._clock = clock
        self._etag: str | None = None
        self._last_fetch: float | None = None
        self.inactive: dict[str, dict] = {}
        self._load_local()

    def _load_local(self) -> None:
        """Seed from the disk cache if it's valid, else from the bundled copy."""
        if self.cache_path and self.cache_path.exists():
            try:
                cached = json.loads(self.cache_path.read_text())
                self.inactive = parse_hints(cached["hints"])
                self._etag = cached.get("etag") or None
                return
            except (OSError, ValueError, KeyError, TypeError) as exc:
                print(f"[hints] ignoring bad cache: {exc!r}")
        if self.bundled_path and self.bundled_path.exists():
            try:
                self.inactive = parse_hints(json.loads(self.bundled_path.read_text()))
            except (OSError, ValueError) as exc:
                print(f"[hints] ignoring bad bundled hints: {exc!r}")

    def due(self) -> bool:
        if not self.url:
            return False
        return self._last_fetch is None or (
            self._clock() - self._last_fetch >= self.refresh_interval
        )

    async def maybe_refresh(self, force: bool = False) -> bool:
        """Fetch if the interval has elapsed (or ``force``). Returns True if the hints changed.

        Never raises for network or parse errors; the previous hints stay in place.
        """
        if not self.url or not (force or self.due()):
            return False
        self._last_fetch = self._clock()
        headers = {"User-Agent": USER_AGENT}
        if self._etag:
            headers["If-None-Match"] = self._etag
        try:
            client = self._client or httpx.AsyncClient(timeout=20, follow_redirects=True)
            try:
                resp = await client.get(self.url, headers=headers)
            finally:
                if self._client is None:
                    await client.aclose()
            if resp.status_code == 304:
                return False
            resp.raise_for_status()
            raw = resp.json()
            inactive = parse_hints(raw)
        except (httpx.HTTPError, ValueError) as exc:
            print(f"[hints] refresh failed, keeping previous hints: {exc!r}")
            return False
        changed = inactive != self.inactive
        self.inactive = inactive
        self._etag = resp.headers.get("etag") or None
        self._save_cache(raw)
        return changed

    def _save_cache(self, raw: dict) -> None:
        if not self.cache_path:
            return
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps({"etag": self._etag, "hints": raw}, indent=2))
            os.replace(tmp, self.cache_path)
        except OSError as exc:
            print(f"[hints] could not write cache: {exc!r}")
