# urls tracker fixtures

Sample Universal Tracker exchanges for the `urls` project
(`https://legacy-api.arpa.li/urls`), used by `tests/test_tracker_protocol.py`.

**Provenance:** currently *protocol-derived* — hand-written to match the Universal
Tracker wire format (verified against `reference/seesaw-kit/seesaw/tracker.py`). Replace
with a real recorded exchange by running:

    python scripts/capture_tracker_exchange.py --downloader YOUR_HANDLE

That script does a `/request` then immediately `/release`s whatever it claimed (no
dangling work item) and overwrites these files with the real bytes. Once refreshed from
production, the contract test is pinned to the live tracker, not to our reading of it.

- `request_response.json` — a 200 body from `POST /request` (a multi-item batch).
- `upload_response.json` — a 200 body from `POST /upload` (rsync target).
- `done_response.txt` — the 200 body from `POST /done`.
