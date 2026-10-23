"""scan_queue.py against a local fake of the amdahl.ai scan API: claim, scan, ai-review and post."""

from __future__ import annotations

import io
import json
import os
import tempfile
import threading
import unittest
import urllib.request
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Iterator
from unittest import mock

from support import FIXTURES, FakeAnthropic, ScannerEnv, fake_scanner, http_error, model_answer

import handoff
import normalize
import scan_queue
from skillmd import sha256_hex

TOKEN = "test-token-not-real"
MODEL_KEY = "model-key-not-real"
MARKDOWN = (FIXTURES / "inputs" / "meeting-follow-up.md").read_text(encoding="utf-8")
CREDIT_LOW = (b'{"type":"error","error":{"type":"invalid_request_error","message":"Your credit balance is too '
              b'low to access the Anthropic API. Please go to Plans & Billing to upgrade or purchase credits."}}')


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


@contextmanager
def environ(**values: str | None) -> Iterator[None]:
    """Set (or, for None, remove) environment variables, and put them back after."""
    saved = {key: os.environ.get(key) for key in values}
    for key, value in values.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value
    try:
        yield
    finally:
        for key, value in saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


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
        self.work = self.dir / "scan"
        self.clean_env = environ(AMDAHL_SITE=self.base, SKILLS_SCAN_TOKEN=None, ANTHROPIC_API_KEY=None)
        self.clean_env.__enter__()

    def tearDown(self) -> None:
        self.clean_env.__exit__(None, None, None)
        self.tmp.cleanup()

    def bins(self, cisco: str = "injection.json") -> list[Path]:
        return [fake_scanner(self.dir, "cs", report=FIXTURES / "cisco" / cisco),
                fake_scanner(self.dir, "bl", report=FIXTURES / "betterleaks" / "clean.json")]

    def claim(self) -> int:
        with environ(SKILLS_SCAN_TOKEN=TOKEN):
            return scan_queue.main(["claim", "--out", str(self.work)])

    def scan(self, bins: list[Path]) -> int:
        with ScannerEnv(*bins):
            return scan_queue.main(["scan", "--items", str(self.work / "items.json"),
                                    "--out", str(self.work / "scanners.json")])

    def review(self, api: FakeAnthropic | None = None, model_key: str | None = None) -> int:
        with environ(ANTHROPIC_API_KEY=model_key), mock.patch.object(urllib.request, "urlopen", api or FakeAnthropic()):
            return scan_queue.main(["ai-review", "--items", str(self.work / "items.json"),
                                    "--out", str(self.work / "ai.json")])

    def post(self) -> int:
        with environ(SKILLS_SCAN_TOKEN=TOKEN):
            return scan_queue.main(["post", "--dir", str(self.work)])

    def run_all(self, bins: list[Path], api: FakeAnthropic | None = None, model_key: str | None = None) -> tuple[list[int], str]:
        """Every step in workflow order, each with only the secret it may hold. Returns (exit codes, log)."""
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            codes = [self.claim(), self.scan(bins), self.review(api, model_key), self.post()]
        return codes, out.getvalue() + err.getvalue()

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

    def test_claim_writes_private_files_and_items_carry_no_ids(self) -> None:
        FakeSite.queue = {"items": [item()]}
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.claim(), 0)
        self.assertEqual(oct(self.work.stat().st_mode & 0o777), "0o700")
        for name in ("claimed.json", "items.json"):
            self.assertEqual(oct((self.work / name).stat().st_mode & 0o777), "0o600", name)
        items = json.loads((self.work / "items.json").read_text(encoding="utf-8"))["items"]
        self.assertEqual(items, [{"position": 0, "name": "meeting-follow-up", "markdown": MARKDOWN}])
        claimed = json.loads((self.work / "claimed.json").read_text(encoding="utf-8"))["items"]
        self.assertEqual(claimed[0]["id"], item()["id"])

    def test_end_to_end_with_fake_scanners(self) -> None:
        FakeSite.queue = {"items": [item()]}
        codes, _ = self.run_all(self.bins())  # no model key: the AI review did not finish, a warning
        self.assertEqual(codes, [0, 0, 0, 0])
        self.assertEqual(len(FakeSite.posts), 1)
        posted = FakeSite.posts[0]
        self.assertEqual(set(posted), {"kind", "id", "digest", "checks"})
        self.assertEqual(posted["digest"], sha256_hex(MARKDOWN))
        self.assertEqual([c["result"] for c in posted["checks"]], ["fail", "pass", "warn"])
        self.assertEqual([c["by"] for c in posted["checks"]],
                         ["Cisco AI Defense skill-scanner", "Betterleaks", "Amdahl AI review"])
        self.assertEqual(posted["checks"][2]["summary"], "Scanner did not finish")

    def test_logs_never_carry_an_id(self) -> None:
        # The Actions logs are public, and an older submission's id still opens its status on amdahl.ai.
        scanned_id = "5c1d2e3f-4a5b-4c6d-8e7f-90a1b2c3d4e5"
        skipped_id = "6d2e3f4a-5b6c-4d7e-8f90-a1b2c3d4e5f6"
        FakeSite.queue = {"items": [{"kind": "skill"}, item(id=skipped_id, digest="0" * 64), item(id=scanned_id)]}
        bins = [fake_scanner(self.dir, name, report=FIXTURES / "betterleaks" / "clean.json") for name in ("cs", "bl")]
        _, log = self.run_all(bins)
        self.assertEqual([p["id"] for p in FakeSite.posts], [scanned_id])
        for secret in (scanned_id, skipped_id, "meeting-follow-up"):
            self.assertNotIn(secret, log)
        # Every step names an item by its place in the site's queue.
        self.assertIn("Skipped queue item 0: it does not match the contract", log)
        self.assertIn("Skipped queue item 1 (submission): digest does not match the markdown", log)
        self.assertRegex(log, r"Queue item 2 \(submission\): warn/pass, AI review did not finish -> posted")

    def test_logs_never_carry_the_model_key_or_the_review(self) -> None:
        # The AI review's verdict and findings go to amdahl.ai only; the public log says only that it finished.
        why = "Sends the notes to WHY-MARKER"
        review = {"purpose": "p", "verdict": "block", "findings": [
            {"check": "data_leaving", "severity": "block", "line": 9, "quote": "send ~/.ssh", "why": why}]}
        api = FakeAnthropic(model_answer(review), model_answer(review))
        FakeSite.queue = {"items": [item()]}
        bins = [fake_scanner(self.dir, name, report=FIXTURES / "betterleaks" / "clean.json") for name in ("cs", "bl")]
        codes, log = self.run_all(bins, api, MODEL_KEY)
        self.assertEqual(codes, [0, 0, 0, 0])
        self.assertEqual(api.models, ["claude-haiku-5-5", "claude-sonnet-5-5"])
        self.assertEqual(FakeSite.posts[0]["checks"][2]["result"], "fail")
        self.assertIn("WHY-MARKER", FakeSite.posts[0]["checks"][2]["summary"])
        self.assertIn("AI review finished -> posted", log)
        for secret in (MODEL_KEY, "WHY-MARKER", "send ~/.ssh", "Ignore previous", "fail", "block"):
            self.assertNotIn(secret, log)
        self.assertNotIn(MODEL_KEY, json.dumps(FakeSite.posts))
        self.assertNotIn(MODEL_KEY, (self.work / "ai.json").read_text(encoding="utf-8"))

    def test_a_budget_refusal_stops_the_review_and_marks_every_item(self) -> None:
        # One refusal for money, then no more calls: every item goes back to amdahl.ai as budget.
        second = "7e3f4a5b-6c7d-4e8f-9a0b-c1d2e3f4a5b6"
        FakeSite.queue = {"items": [item(), item(id=second)]}
        api = FakeAnthropic(http_error(400, CREDIT_LOW))
        codes, log = self.run_all(self.bins(), api, MODEL_KEY)
        self.assertEqual(codes, [0, 0, 0, 0])
        self.assertEqual(len(api.requests), 1)
        self.assertEqual(len(FakeSite.posts), 2)
        for posted in FakeSite.posts:
            ai = posted["checks"][2]
            self.assertEqual((ai["result"], ai["summary"]), ("warn", "Scanner did not finish"))
            self.assertEqual(ai["details"]["reason"], "budget")
        self.assertIn("stopped sending reviews", log)
        self.assertIn("AI review did not finish (budget) -> posted", log)
        self.assertNotIn("credit balance is too low", log)

    def test_post_reads_the_scanner_file_as_untrusted(self) -> None:
        # Code that broke out of a scanner could write scanners.json. It can never
        # claim the budget signal or stand in for the AI review.
        FakeSite.queue = {"items": [item()]}
        with redirect_stdout(io.StringIO()):
            self.claim()
        day = "2026-10-23"
        forged = [
            {**normalize.did_not_finish(normalize.CISCO, day, "x", code=normalize.BUDGET)},
            {"name": "Secrets", "by": "Betterleaks", "result": "pass", "summary": "No\u200b secrets", "checkedAt": day},
            {"name": "AI security review", "by": "Amdahl AI review", "result": "pass", "summary": "No findings",
             "checkedAt": day},
        ]
        handoff.write_private(self.work / "scanners.json", {"results": [{"position": 0, "checks": forged}]})
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self.post(), 0)
        cisco, betterleaks, ai = FakeSite.posts[0]["checks"]
        self.assertEqual((cisco["summary"], cisco["details"]),
                         ("Scanner did not finish", "The scanner step gave no usable result for this skill"))
        self.assertEqual((betterleaks["result"], betterleaks["summary"]), ("pass", "No\\u200b secrets"))
        self.assertEqual((ai["result"], ai["summary"]), ("warn", "Scanner did not finish"))
        self.assertNotIn("reason", json.dumps(FakeSite.posts))

    def test_post_with_no_result_files_marks_every_check_did_not_finish(self) -> None:
        FakeSite.queue = {"items": [item()]}
        with redirect_stdout(io.StringIO()):
            self.claim()
            self.assertEqual(self.post(), 0)
        self.assertEqual([c["summary"] for c in FakeSite.posts[0]["checks"]], ["Scanner did not finish"] * 3)

    def test_each_step_refuses_a_secret_it_must_not_hold(self) -> None:
        FakeSite.queue = {"items": [item()]}
        with redirect_stdout(io.StringIO()):
            self.claim()
        cases = [
            (["scan", "--items", str(self.work / "items.json"), "--out", str(self.dir / "s.json")],
             {"SKILLS_SCAN_TOKEN": TOKEN}),
            (["scan", "--items", str(self.work / "items.json"), "--out", str(self.dir / "s.json")],
             {"ANTHROPIC_API_KEY": MODEL_KEY}),
            (["ai-review", "--items", str(self.work / "items.json"), "--out", str(self.dir / "a.json")],
             {"SKILLS_SCAN_TOKEN": TOKEN, "ANTHROPIC_API_KEY": MODEL_KEY}),
            (["claim", "--out", str(self.dir / "c")], {"SKILLS_SCAN_TOKEN": TOKEN, "ANTHROPIC_API_KEY": MODEL_KEY}),
            (["post", "--dir", str(self.work)], {"SKILLS_SCAN_TOKEN": TOKEN, "ANTHROPIC_API_KEY": MODEL_KEY}),
        ]
        api = FakeAnthropic()
        for argv, env in cases:
            err = io.StringIO()
            with environ(**env), redirect_stderr(err), redirect_stdout(io.StringIO()), \
                    mock.patch.object(urllib.request, "urlopen", api):
                self.assertEqual(scan_queue.main(argv), 1, argv)
            self.assertIn("must never hold it", err.getvalue(), argv)
        self.assertEqual(api.requests, [])
        self.assertEqual(FakeSite.posts, [])
        for path in (self.dir / "s.json", self.dir / "a.json"):
            self.assertFalse(path.exists(), path)

    def test_missing_token(self) -> None:
        with redirect_stderr(io.StringIO()):
            self.assertEqual(scan_queue.main(["claim", "--out", str(self.dir / "q")]), 1)
            self.assertEqual(scan_queue.main(["post", "--dir", str(self.dir / "q")]), 1)


if __name__ == "__main__":
    unittest.main()
