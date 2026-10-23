"""The workflow files: the keepalive that stops GitHub from turning the schedules off.

Standard library only, so these read the YAML as text.
"""

from __future__ import annotations

import re
import unittest

from support import TESTS

WORKFLOWS = TESTS.parent / ".github" / "workflows"


def read(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def line_with(text: str, needle: str) -> str:
    """The one stripped line of text that contains needle."""
    found = [line.strip() for line in text.splitlines() if needle in line]
    if len(found) != 1:
        raise AssertionError(f"expected one line with {needle!r}, found {len(found)}")
    return found[0]


class Keepalive(unittest.TestCase):
    # GitHub turns off scheduled workflows in a public repository after 60 days
    # with no activity. Every approval on amdahl.ai waits for the scheduled scans.

    def setUp(self) -> None:
        self.text = read("keepalive.yml")
        self.mirror = read("mirror.yml")

    def test_runs_twice_every_month(self) -> None:
        crons = re.findall(r'^\s*- cron: "([^"]+)"\s*$', self.text, re.MULTILINE)
        self.assertEqual(len(crons), 1)
        minute, hour, day, month, weekday = crons[0].split()
        self.assertTrue(minute.isdigit() and hour.isdigit(), crons[0])
        # Two fixed days that every month has, at least 10 days apart, so one
        # failed run still leaves the next well inside the 60-day window.
        days = day.split(",")
        self.assertEqual(len(days), 2, crons[0])
        self.assertTrue(all(d.isdigit() and 1 <= int(d) <= 28 for d in days), crons[0])
        self.assertGreaterEqual(int(days[1]) - int(days[0]), 10, crons[0])
        self.assertEqual((month, weekday), ("*", "*"))
        self.assertRegex(self.text, r"(?m)^  workflow_dispatch:\s*$")

    def test_pushes_with_the_mirror_bot_token(self) -> None:
        # main is protected: only the GitHub App the mirror uses may push to it.
        for needle in ("uses: actions/create-github-app-token@", "client-id:", "private-key:",
                       "permission-contents:", "uses: actions/checkout@", "token: ${{ steps.app-token"):
            self.assertEqual(line_with(self.text, needle), line_with(self.mirror, needle), needle)
        self.assertEqual(line_with(self.text, "if: github.repository"), line_with(self.mirror, "if: github.repository"))
        self.assertRegex(self.text, r"(?m)^permissions:\n  contents: write$")

    def test_commits_only_the_keepalive_file(self) -> None:
        self.assertIn("> .github/keepalive", self.text)
        self.assertEqual(re.findall(r"git add .*", self.text), ["git add -- .github/keepalive"])
        self.assertIn('git push origin "HEAD:${GITHUB_REF_NAME}"', self.text)


if __name__ == "__main__":
    unittest.main()
