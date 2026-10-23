"""handoff.py: the files the scan-queue steps pass to each other, read back strictly."""

from __future__ import annotations

import json
import re
import tempfile
import unittest
from pathlib import Path

from support import TESTS

import handoff
import normalize

DAY = "2026-10-23"


def row(pair: tuple[str, str], **changes: object) -> dict:
    base = {"name": pair[0], "by": pair[1], "result": "pass", "summary": "No findings", "checkedAt": DAY}
    base.update(changes)
    return base


class CleanCheck(unittest.TestCase):
    def test_a_good_row_is_rebuilt_from_its_known_fields(self) -> None:
        got = handoff.clean_check(row(normalize.CISCO, details="line one\nline two"), normalize.CISCO)
        self.assertEqual(got, row(normalize.CISCO, details="line one\nline two"))

    def test_anything_off_is_refused(self) -> None:
        bad = {
            "an extra field": row(normalize.CISCO, extra=1),
            "another scanner's name": row(normalize.BETTERLEAKS),
            "an unknown result": row(normalize.CISCO, result="ok"),
            "a summary that is not text": row(normalize.CISCO, summary=["x"]),
            "an empty summary": row(normalize.CISCO, summary="  "),
            "a date in another shape": row(normalize.CISCO, checkedAt="23/10/2026"),
            "details as a list": row(normalize.CISCO, details=["x"]),
            "not an object": [row(normalize.CISCO)],
        }
        for label, raw in bad.items():
            self.assertIsNone(handoff.clean_check(raw, normalize.CISCO), label)

    def test_hidden_characters_are_shown_and_long_text_clipped(self) -> None:
        got = handoff.clean_check(row(normalize.CISCO, summary="a\u202eb" + "x" * 400, details="d" * 5000),
                                  normalize.CISCO)
        self.assertTrue(got["summary"].startswith("a\\u202eb"))
        self.assertLessEqual(len(got["summary"]), normalize.SUMMARY_MAX)
        self.assertEqual(len(got["details"]), normalize.DETAILS_MAX)

    def test_only_the_ai_review_may_carry_a_reason_and_only_when_it_did_not_finish(self) -> None:
        budget = normalize.did_not_finish(normalize.AI_REVIEW, DAY, "spend limit", code=normalize.BUDGET)
        self.assertEqual(handoff.clean_check(budget, normalize.AI_REVIEW, allow_reason=True), budget)
        self.assertIsNone(handoff.clean_check(budget, normalize.AI_REVIEW))  # a scanner's file never may
        passing = {**budget, "result": "pass", "summary": "No findings"}
        self.assertIsNone(handoff.clean_check(passing, normalize.AI_REVIEW, allow_reason=True))
        for details in ({"reason": "other", "text": "x"}, {"reason": "budget"}, {"reason": "budget", "text": 1},
                        {"reason": "budget", "text": "x", "more": 1}):
            self.assertIsNone(handoff.clean_check({**budget, "details": details}, normalize.AI_REVIEW,
                                                  allow_reason=True), details)


class Assemble(unittest.TestCase):
    def test_missing_results_are_did_not_finish_in_order(self) -> None:
        checks = handoff.assemble(0, {}, {}, DAY)
        self.assertEqual([(c["name"], c["by"]) for c in checks],
                         [normalize.CISCO, normalize.BETTERLEAKS, normalize.AI_REVIEW])
        self.assertEqual({(c["result"], c["summary"]) for c in checks}, {("warn", normalize.DID_NOT_FINISH)})

    def test_each_file_fills_only_its_own_rows(self) -> None:
        scanners = {0: [row(normalize.BETTERLEAKS), row(normalize.CISCO, result="fail"), row(normalize.AI_REVIEW)]}
        ai = {0: [row(normalize.AI_REVIEW, result="warn", summary="Overrides on line 3: x"), row(normalize.CISCO)]}
        cisco, betterleaks, review = handoff.assemble(0, scanners, ai, DAY)
        self.assertEqual(cisco["result"], "fail")
        self.assertEqual(betterleaks["result"], "pass")
        self.assertEqual((review["result"], review["summary"]), ("warn", "Overrides on line 3: x"))
        # An AI review row in the scanners' file is ignored, never used.
        cisco, betterleaks, review = handoff.assemble(0, scanners, {}, DAY)
        self.assertEqual(review["summary"], normalize.DID_NOT_FINISH)

    def test_a_check_listed_twice_has_no_answer(self) -> None:
        scanners = {0: [row(normalize.CISCO), row(normalize.CISCO, result="fail"), row(normalize.BETTERLEAKS)]}
        cisco, betterleaks, _ = handoff.assemble(0, scanners, {}, DAY)
        self.assertEqual(cisco["summary"], normalize.DID_NOT_FINISH)
        self.assertEqual(betterleaks["result"], "pass")


class Files(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_write_private_is_owner_only_and_whole(self) -> None:
        path = self.dir / "a.json"
        handoff.write_private(path, {"x": 1})
        handoff.write_private(path, {"x": 2})
        self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"x": 2})
        self.assertEqual(sorted(p.name for p in self.dir.iterdir()), ["a.json"])

    def test_read_json_refuses_missing_broken_and_oversized_files(self) -> None:
        self.assertIsNone(handoff.read_json(self.dir / "none.json"))
        (self.dir / "bad.json").write_text("{", encoding="utf-8")
        self.assertIsNone(handoff.read_json(self.dir / "bad.json"))
        big = self.dir / "big.json"
        with open(big, "w", encoding="utf-8") as handle:
            handle.write('"' + "x" * handoff.MAX_FILE_BYTES + '"')
        self.assertIsNone(handoff.read_json(big))

    def test_by_position_drops_duplicates_and_bad_positions(self) -> None:
        payload = {"results": [{"position": 0, "checks": ["a"]}, {"position": 1, "checks": ["b"]},
                               {"position": 1, "checks": ["c"]}, {"position": True, "checks": ["d"]},
                               {"position": "2", "checks": ["e"]}, "junk"]}
        self.assertEqual(handoff.by_position(payload), {0: ["a"]})
        for junk in (None, [], {"results": "x"}):
            self.assertEqual(handoff.by_position(junk), {})

    def test_read_items_counts_bad_entries(self) -> None:
        path = self.dir / "items.json"
        handoff.write_private(path, {"items": [
            {"position": 0, "name": "ok-name", "markdown": "x", "id": "dropped"},
            {"position": 1, "name": "../escape", "markdown": "x"},
            {"position": False, "name": "ok", "markdown": "x"},
            {"position": 3, "name": "ok", "markdown": None},
        ]})
        items, invalid = handoff.read_items(path)
        self.assertEqual(items, [{"position": 0, "name": "ok-name", "markdown": "x"}])
        self.assertEqual(invalid, 3)
        with self.assertRaises(ValueError):
            handoff.read_items(self.dir / "missing.json")

    def test_sandbox_copies_no_more_than_the_post_step_reads(self) -> None:
        script = (TESTS.parent / "scripts" / "sandbox.sh").read_text(encoding="utf-8")
        found = re.findall(r"(?m)^MAX_RESULT_BYTES=(\d+)$", script)
        self.assertEqual(found, [str(handoff.MAX_FILE_BYTES)])


class AiState(unittest.TestCase):
    def test_states_never_carry_the_verdict(self) -> None:
        self.assertEqual(handoff.ai_state(row(normalize.AI_REVIEW, result="fail", summary="Overrides: x")), "finished")
        self.assertEqual(handoff.ai_state(normalize.did_not_finish(normalize.AI_REVIEW, DAY, "x")), "did not finish")
        budget = normalize.did_not_finish(normalize.AI_REVIEW, DAY, "x", code=normalize.BUDGET)
        self.assertEqual(handoff.ai_state(budget), "did not finish (budget)")


if __name__ == "__main__":
    unittest.main()
