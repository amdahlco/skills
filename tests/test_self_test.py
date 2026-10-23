"""self_test.py's CI modes: items writes the samples, check grades the result files like the post step."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

import support  # noqa: F401 - puts scripts/ on the import path

import handoff
import normalize
import self_test

DAY = "2026-10-23"


def row(pair: tuple[str, str], result: str) -> dict:
    return {"name": pair[0], "by": pair[1], "result": result, "summary": "x", "checkedAt": DAY}


GOOD_SCANNERS = {"results": [
    {"position": 0, "checks": [row(normalize.CISCO, "pass"), row(normalize.BETTERLEAKS, "pass")]},
    {"position": 1, "checks": [row(normalize.CISCO, "fail"), row(normalize.BETTERLEAKS, "pass")]},
    {"position": 2, "checks": [row(normalize.CISCO, "pass"), row(normalize.BETTERLEAKS, "warn")]},
]}


class SelfTest(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name) / "self-test"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def check(self) -> int:
        with redirect_stdout(io.StringIO()):
            return self_test.main(["check", "--dir", str(self.dir)])

    def test_items_writes_the_three_samples_without_ids(self) -> None:
        with redirect_stdout(io.StringIO()):
            self.assertEqual(self_test.main(["items", "--out", str(self.dir)]), 0)
        path = self.dir / "items.json"
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        items = json.loads(path.read_text(encoding="utf-8"))["items"]
        self.assertEqual([(i["position"], i["name"]) for i in items],
                         [(0, "discovery-call-notes"), (1, "meeting-follow-up"), (2, "crm-sync")])
        self.assertEqual({key for i in items for key in i}, {"position", "name", "markdown"})
        self.assertRegex(items[2]["markdown"], r"ghp_[A-Za-z0-9]{36}")

    def test_check_passes_on_the_expected_verdicts_without_a_key(self) -> None:
        self.dir.mkdir()
        handoff.write_private(self.dir / "scanners.json", GOOD_SCANNERS)
        handoff.write_private(self.dir / "ai.json", {"keyed": False, "results": [
            {"position": n, "checks": [normalize.did_not_finish(normalize.AI_REVIEW, DAY, "no key")]}
            for n in range(3)]})
        self.assertEqual(self.check(), 0)

    def test_check_fails_when_a_scanner_misses_the_injection(self) -> None:
        self.dir.mkdir()
        missed = json.loads(json.dumps(GOOD_SCANNERS))
        missed["results"][1]["checks"][0]["result"] = "pass"
        handoff.write_private(self.dir / "scanners.json", missed)
        self.assertEqual(self.check(), 1)

    def test_check_fails_when_the_scanner_user_left_nothing(self) -> None:
        self.dir.mkdir()
        self.assertEqual(self.check(), 1)

    def test_check_grades_the_ai_review_when_it_ran(self) -> None:
        self.dir.mkdir()
        handoff.write_private(self.dir / "scanners.json", GOOD_SCANNERS)
        verdicts = ["pass", "fail", "warn"]
        ai = {"keyed": True, "results": [{"position": n, "checks": [row(normalize.AI_REVIEW, v)]}
                                         for n, v in enumerate(verdicts)]}
        handoff.write_private(self.dir / "ai.json", ai)
        self.assertEqual(self.check(), 0)
        ai["results"][1]["checks"][0]["result"] = "pass"  # the review let the injection through
        handoff.write_private(self.dir / "ai.json", ai)
        self.assertEqual(self.check(), 1)


if __name__ == "__main__":
    unittest.main()
