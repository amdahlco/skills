"""What the public docs promise must match what the site and the scripts do."""

from __future__ import annotations

import unittest

from support import TESTS

import scan_queue

ROOT = TESTS.parent


def read(*parts: str) -> str:
    return ROOT.joinpath(*parts).read_text(encoding="utf-8")


def section(markdown: str, heading: str) -> str:
    """The text under one '## ' heading, up to the next one."""
    start = markdown.index(heading + "\n")
    end = markdown.find("\n## ", start + len(heading))
    return markdown[start:] if end == -1 else markdown[start:end]


class Readme(unittest.TestCase):
    def setUp(self) -> None:
        self.text = read("README.md")

    def test_scan_claim_leaves_out_folder_skills(self) -> None:
        # A folder skill is Amdahl's own code from its own repository; no scanner runs on it.
        checks = section(self.text, "## Checks and review")
        self.assertNotIn("Every skill gets", checks)
        self.assertIn("Every single-file skill gets three automated checks.", checks)
        self.assertIn("A folder skill, such as system-diagram, is not scanned here.", checks)

    def test_install_covers_both_kinds_from_amdahl_ai(self) -> None:
        # Saving a folder skill's SKILL.md alone leaves out the files it runs.
        install = section(self.text, "## Install a skill")
        self.assertIn("Install from [amdahl.ai/skills](https://amdahl.ai/skills).", install)
        self.assertIn("saves its `SKILL.md` as `.claude/skills/<name>/SKILL.md`", install)
        self.assertIn("downloads the whole folder from amdahl.ai as one `.tar.gz` file", install)
        self.assertIn("Do not save only the `SKILL.md` of a folder skill", install)


class Contributing(unittest.TestCase):
    def test_body_rule_is_the_site_gate_rule(self) -> None:
        # The site's gate warns past 500 body lines and refuses past 1000.
        text = read("CONTRIBUTING.md")
        self.assertIn("Keep the body after the frontmatter under 500 lines. More than 1000 is refused.", text)
        self.assertNotIn("must be under 500 lines", text)


class LogPromise(unittest.TestCase):
    def test_no_text_says_the_logs_carry_an_id(self) -> None:
        # A submission's id is the only key to its status on amdahl.ai, and the logs are public.
        for label, raw in (("scan_queue.py", scan_queue.__doc__ or ""),
                           ("scan-queue.yml", read(".github", "workflows", "scan-queue.yml"))):
            text = " ".join(raw.replace("#", " ").split())
            self.assertNotIn("its id and the three results", text, label)
            self.assertIn("its place in the queue and the three results", text, label)


if __name__ == "__main__":
    unittest.main()
