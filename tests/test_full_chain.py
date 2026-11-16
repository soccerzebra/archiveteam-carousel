"""Hermetic full-chain test: claim -> download -> stats -> upload -> done, real binaries.

A local HTTP server stands in for the tracker, the origin being archived, and the upload
sink. Our real stack (loader + Runner pipeline + tracker client + ExternalProcess) drives
it, using real `curl` for both download and upload. A second test drives a real `rsync`
transfer. No network, no production tracker, no real items.

This is the gate before a live crawl: everything our warrior does — claim an item, fetch
bytes, compute stats, upload the file, report done — runs here against real processes, and
the uploaded bytes + the /done stats are asserted exactly. (Real wget-at WARC production is
an external-binary concern, validated by the live crawl, not here.)
"""

import asyncio
import json
import pathlib
import shutil
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "worker"))

import run_pipeline
from seesaw.context import RunContext
from seesaw.externalprocess import RsyncUpload
from seesaw.item import Item

CHAIN_PIPELINE = HERE / "fixtures" / "chain_project" / "pipeline.py"
ORIGIN = b"hello-archive-team\n" * 500  # the bytes we "download" and then upload


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body=b""):
        self.send_response(code)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length", "0"))
        return self.rfile.read(n) if n else b""

    def do_GET(self):
        if self.path == "/origin":
            self._send(200, self.server.origin)
        else:
            self._send(404)

    def do_PUT(self):
        body = self._body()
        self.server.uploads[self.path.rsplit("/", 1)[-1]] = body
        self._send(201)

    def do_POST(self):
        body = self._body()
        if self.path.endswith("/request"):
            self._send(200, json.dumps({"item_name": "itm-1", "items": ["itm-1"]}).encode())
        elif self.path.endswith("/upload"):
            target = f"http://127.0.0.1:{self.server.server_address[1]}/up/"
            self._send(200, json.dumps({"upload_target": target}).encode())
        elif self.path.endswith("/done"):
            self.server.done_body = json.loads(body)
            self._send(200, b"OK")
        else:
            self._send(404)


def _server():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    srv.origin = ORIGIN
    srv.uploads = {}
    srv.done_body = None
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.mark.skipif(not shutil.which("curl"), reason="needs curl")
def test_full_chain_claim_download_upload_done(tmp_path):
    srv = _server()
    base = f"http://127.0.0.1:{srv.server_address[1]}"
    try:
        _, pipeline = run_pipeline.load_pipeline(
            str(CHAIN_PIPELINE),
            downloader="archivist",
            extra_context={"base_url": base, "data_root": str(tmp_path)},
        )

        async def go():
            import httpx

            async with httpx.AsyncClient(timeout=30) as http:
                ctx = RunContext(http=http, data_dir=str(tmp_path), downloader="archivist")
                item = Item()
                await pipeline.run_item(item, ctx)
                return item

        item = asyncio.run(go())
    finally:
        srv.shutdown()

    # 1. the item was claimed from the (fake) tracker
    assert item["item_name"] == "itm-1"
    # 2. download wrote the origin bytes to disk (real curl)
    downloaded = pathlib.Path(item["data_dir"]) / f"{item['warc_file_base']}.warc.zst"
    assert downloaded.read_bytes() == ORIGIN
    # 3. upload transferred identical bytes to the sink (real curl PUT)
    assert srv.uploads["demo-itm-1.warc.zst"] == ORIGIN
    # 4. done reported exact stats
    assert srv.done_body == {
        "downloader": "archivist",
        "version": "1.0",
        "item": "itm-1",
        "bytes": {"data": len(ORIGIN)},
        "id": "demo-itm-1",
    }


@pytest.mark.skipif(not shutil.which("rsync"), reason="needs rsync")
def test_rsync_real_transfer(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    (src / "a.warc.zst").write_bytes(b"x" * 1024)
    (src / "a_data.txt").write_bytes(b"y" * 32)
    dest = tmp_path / "dest"
    dest.mkdir()

    task = RsyncUpload(
        str(dest) + "/",
        [str(src / "a.warc.zst"), str(src / "a_data.txt")],
        target_source_path=str(src) + "/",
        max_tries=1,
    )
    asyncio.run(task.run(Item(None, {"item_name": "x"}), RunContext()))

    assert (dest / "a.warc.zst").read_bytes() == b"x" * 1024
    assert (dest / "a_data.txt").read_bytes() == b"y" * 32
