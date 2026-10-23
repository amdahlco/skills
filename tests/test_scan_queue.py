"""scan_queue.py against a local fake of the amdahl.ai scan API."""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from support import FIXTURES, ScannerEnv, fake_scanner

import scan_queue
from skillmd import sha256_hex

TOKEN = "test-token-not-real"
MARKDOWN = (FIXTURES / "inputs" / "meeting-follow-up.md").read_text(encoding="utf-8")


def item(**changes: object) -> dict:
    base = {
        "kind": "submission",
        "id": "0b9f1c2e-3d4a-4b5c-8d6e-7f8091a2b3c4",
        "name": "meeting-follow-up",
        "publisher": "someone",
        "digest": sha256_hex(MARKDOWN),
        "markdown": MARKDOWN,
    }
    base.update(changes)
    return base


class FakeSite(BaseHTTPRequestHandler):
    queue: dict = {"items": []}
    post_status = 200
    posts: list = []
    auth_seen: list = []

    def log_message(self, *args: object) -> None:  # keep test output quiet
        pass

    def _reply(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        FakeSite.auth_seen.append(self.headers.get("Authorization"))
        if self.path == scan_queue.QUEUE_PATH:
            self._reply(200, FakeSite.queue)
        else:
            self._reply(404, {})

    def do_POST(self) -> None:  # noqa: N802
        FakeSite.auth_seen.append(self.headers.get("Authorization"))
        length = int(self.headers.get("Content-Length", "0"))
        FakeSite.posts.append(json.loads(self.rfile.read(length)))
        self._reply(FakeSite.post_status, {"ok": FakeSite.post_status == 200})


class SiteBase(unittest.TestCase):
    def test_https_only_except_localhost(self) -> None:
        self.assertEqual(scan_queue.site_base(None), "https://amdahl.ai")
        self.assertEqual(scan_queue.site_base("https://amdahl.ai/"), "https://amdahl.ai")
        self.assertEqual(scan_queue.site_base("http://127.0.0.1:8000"), "http://127.0.0.1:8000")
        for bad in ("http://amdahl.ai", "https://user:pw@amdahl.ai", "https://amdahl.ai/x", "ftp://a", "https://a?q=1"):
            with self.assertRaises(scan_queue.QueueError, msg=bad):
                scan_queue.site_base(bad)


class Items(unittest.TestCase):
    def test_valid_and_publisher_ignored(self) -> None:
        got = scan_queue.validate_item(item())
        self.assertIsNotNone(got)
        self.assertNotIn("publisher", got)

    def test_invalid(self) -> None:
        for change in ({"kind": "other"}, {"id": "not-a-uuid"}, {"name": "../x"}, {"digest": "abc"},
                       {"markdown": None}):
            self.assertIsNone(scan_queue.validate_item(item(**change)), change)


class Flow(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), FakeSite)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls) -> None:
        cls.server.shutdown()
        cls.server.server_close()

    def setUp(self) -> None:
        FakeSite.posts, FakeSite.auth_seen, FakeSite.post_status = [], [], 200
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)
        os.environ["AMDAHL_SITE"] = self.base
        os.environ["SKILLS_SCAN_TOKEN"] = TOKEN

    def tearDown(self) -> None:
        self.tmp.cleanup()
        os.environ.pop("AMDAHL_SITE", None)
        os.environ.pop("SKILLS_SCAN_TOKEN", None)

    def test_fetch_skips_bad_items_and_digest_mismatches(self) -> None:
        FakeSite.queue = {"items": [item(), {"kind": "skill"}, item(digest="0" * 64)]}
        items, invalid = scan_queue.fetch(self.base, TOKEN)
        self.assertEqual(len(items), 1)
        self.assertEqual(invalid, 2)
        self.assertEqual(FakeSite.auth_seen, [f"Bearer {TOKEN}"])

    def test_post_outcomes(self) -> None:
        good = scan_queue.validate_item(item())
        for status, outcome in ((200, "posted"), (409, "stale"), (404, "unknown"), (400, "error")):
            FakeSite.post_status = status
            self.assertEqual(scan_queue.post_result(self.base, TOKEN, good, []), outcome)

    def test_end_to_end_with_fake_scanners(self) -> None:
        FakeSite.queue = {"items": [item()]}
        queue_file = self.dir / "queue.json"
        self.assertEqual(scan_queue.main(["fetch", "--out", str(queue_file)]), 0)
        self.assertEqual(oct(queue_file.stat().st_mode & 0o777), "0o600")
        bins = [fake_scanner(self.dir, name, report=FIXTURES / tool / "injection.json")
                for name, tool in (("ss", "skillspector"), ("cs", "cisco"))]
        bins.append(fake_scanner(self.dir, "bl", report=FIXTURES / "betterleaks" / "clean.json"))
        with ScannerEnv(*bins):
            self.assertEqual(scan_queue.main(["run", "--queue", str(queue_file)]), 0)
        self.assertEqual(len(FakeSite.posts), 1)
        posted = FakeSite.posts[0]
        self.assertEqual(set(posted), {"kind", "id", "digest", "checks"})
        self.assertEqual(posted["digest"], sha256_hex(MARKDOWN))
        self.assertEqual([c["result"] for c in posted["checks"]], ["fail", "fail", "pass"])

    def test_logs_never_carry_an_id(self) -> None:
        # The Actions logs are public, and a submission's id opens its status on amdahl.ai.
        scanned_id = "5c1d2e3f-4a5b-4c6d-8e7f-90a1b2c3d4e5"
        skipped_id = "6d2e3f4a-5b6c-4d7e-8f90-a1b2c3d4e5f6"
        FakeSite.queue = {"items": [{"kind": "skill"}, item(id=skipped_id, digest="0" * 64), item(id=scanned_id)]}
        queue_file = self.dir / "queue.json"
        bins = [fake_scanner(self.dir, name, report=FIXTURES / "betterleaks" / "clean.json") for name in ("ss", "cs", "bl")]
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err), ScannerEnv(*bins):
            scan_queue.main(["fetch", "--out", str(queue_file)])
            scan_queue.main(["run", "--queue", str(queue_file)])
        log = out.getvalue() + err.getvalue()
        self.assertEqual([p["id"] for p in FakeSite.posts], [scanned_id])
        for secret in (scanned_id, skipped_id, "meeting-follow-up"):
            self.assertNotIn(secret, log)
        # Both steps name an item by its place in the site's queue.
        self.assertIn("Skipped queue item 0: it does not match the contract", log)
        self.assertIn("Skipped queue item 1 (submission): digest does not match the markdown", log)
        self.assertRegex(log, r"Queue item 2 \(submission\): \S+ -> posted")

    def test_missing_token(self) -> None:
        del os.environ["SKILLS_SCAN_TOKEN"]
        self.assertEqual(scan_queue.main(["fetch", "--out", str(self.dir / "q.json")]), 1)


if __name__ == "__main__":
    unittest.main()
