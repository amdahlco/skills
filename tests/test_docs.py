"""What the public docs promise must match what the site and the scripts do."""

from __future__ import annotations

import re
import unittest

from support import TESTS

import ai_review
import normalize
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

    def test_checks_table_names_every_check_the_scan_posts(self) -> None:
        checks = section(self.text, "## Checks and review")
        for name, by in (normalize.CISCO, normalize.BETTERLEAKS, normalize.AI_REVIEW):
            self.assertRegex(checks, rf"(?m)^\| {name} \| ", name)
        self.assertIn("| AI security review | Amdahl AI review: Claude Haiku 5.5, then Claude Sonnet 5.5 |", checks)
        self.assertNotIn("SkillSpector", self.text)

    def test_publishing_is_automatic_and_says_so(self) -> None:
        # No person approves a skill: every check passing publishes it, a failed check declines it.
        for text in (self.text, read("CONTRIBUTING.md")):
            self.assertNotIn("person at Amdahl", text)
            self.assertNotIn("approves them", text)
            self.assertNotIn("SkillSpector", text)
        checks = section(self.text, "## Checks and review")
        self.assertIn("Skills that pass every check publish automatically.", checks)
        self.assertIn("A failed check declines the skill, with the reason.", checks)
        self.assertIn("If every check passes, the skill publishes automatically", read("CONTRIBUTING.md"))

    def test_ai_review_claims_match_the_code(self) -> None:
        checks = section(self.text, "## Checks and review")
        self.assertNotIn("No AI model sees the skill", self.text)
        self.assertNotIn("holds no model keys", self.text)
        self.assertIn("1. Claude Haiku 5.5 reviews the `SKILL.md`. If its verdict is pass, the check passes.", checks)
        self.assertIn("Claude Sonnet 5.5 reviews the same text. Its verdict is final.", checks)
        self.assertIn("No tools, no files and no credentials.", checks)
        self.assertIn("Their quotes from the skill add up to 120 characters at most.", checks)
        self.assertEqual((ai_review.FIRST_MODEL, ai_review.FINAL_MODEL), ("claude-haiku-5-5", "claude-sonnet-5-5"))
        self.assertEqual(normalize.QUOTE_BUDGET, 120)

    def test_install_covers_both_kinds_from_amdahl_ai(self) -> None:
        # Saving a folder skill's SKILL.md alone leaves out the files it runs.
        install = section(self.text, "## Install a skill")
        self.assertIn("Install from [amdahl.ai/skills](https://amdahl.ai/skills).", install)
        self.assertIn("npx amdahl skills add <publisher>/<skill>", install)
        self.assertIn("this project, or every project on your computer", install)
        self.assertIn("`.claude/skills/<name>/` for Claude Code, and `.agents/skills/<name>/` for Codex, Cursor and other agents", install)
        self.assertIn("saves its `SKILL.md` as `<name>/SKILL.md` in that skills folder", install)
        self.assertIn("writes nothing if the check fails", install)
        self.assertIn("downloads the whole folder from amdahl.ai as one `.tar.gz` file", install)
        self.assertIn("Do not save only the `SKILL.md` of a folder skill", install)


class Contributing(unittest.TestCase):
    def test_body_rule_is_the_site_gate_rule(self) -> None:
        # The site's gate warns past 500 body lines and refuses past 1000.
        text = read("CONTRIBUTING.md")
        self.assertIn("Keep the body after the frontmatter under 500 lines. More than 1000 is refused.", text)
        self.assertNotIn("must be under 500 lines", text)


class LogPromise(unittest.TestCase):
    def test_no_text_says_the_logs_carry_an_id_or_the_ai_verdict(self) -> None:
        # The logs are public. An older submission's id still opens its status on amdahl.ai,
        # and the AI review's result is shown to the author only.
        for label, raw in (("scan_queue.py", scan_queue.__doc__ or ""),
                           ("scan-queue.yml", read(".github", "workflows", "scan-queue.yml"))):
            text = " ".join(raw.replace("#", " ").split())
            self.assertNotIn("its id and the", text, label)
            self.assertNotIn("the three results", text, label)
            self.assertIn("its place in the queue, the two scanners' results and whether the AI review finished",
                          text, label)


def flat(text: str) -> str:
    return " ".join(text.split())


class OpenRegistry(unittest.TestCase):
    # Owner decisions of 2026-10-23: upload first, author only, automatic publishing.

    def setUp(self) -> None:
        self.readme = flat(read("README.md"))
        self.contributing = flat(read("CONTRIBUTING.md"))

    def test_upload_is_the_default_and_github_is_optional(self) -> None:
        for label, text in (("README", self.readme), ("CONTRIBUTING", self.contributing)):
            self.assertIn("No GitHub repo needed.", text, label)
            self.assertIn("A GitHub link is optional.", text, label)
            self.assertNotIn("under your own GitHub user", text, label)
            self.assertNotIn("amdahl.ai/skills/<your-github-user>", text, label)
        self.assertIn("drop the `.md`, `.skill` or `.zip` file", self.contributing)
        self.assertIn("A skill on amdahl.ai is one `SKILL.md` file.", self.contributing)

    def test_only_the_author_can_change_a_skill(self) -> None:
        for label, text in (("README", self.readme), ("CONTRIBUTING", self.contributing)):
            self.assertIn("Only a skill's author can change it.", text, label)
            self.assertIn("This repo takes no pull requests for skills", text, label)

    def test_warnings_publish_and_a_possible_secret_waits(self) -> None:
        self.assertIn("A warning does not stop a skill", self.readme)
        self.assertIn("A possible secret waits for the author's answer.", self.readme)
        self.assertIn("A warning does not stop it.", self.contributing)
        self.assertIn("A possible secret waits for your answer.", self.contributing)

    def test_no_text_says_a_person_approves_a_skill(self) -> None:
        for label in ("README.md", "CONTRIBUTING.md", "e2e/README.md"):
            text = read(*label.split("/"))
            # "the reviewer" in the AI review section is the model; these are the old human-review phrases.
            self.assertNotRegex(text.lower(), r"approv|review queue|shown to the reviewer|person at amdahl", label)

    def test_open_source_describes_only_this_repos_code(self) -> None:
        for label in ("README.md", "CONTRIBUTING.md", "SECURITY.md", "e2e/README.md"):
            sentences = re.findall(r"[^.]*open source[^.]*\.", flat(read(*label.split("/"))), re.IGNORECASE)
            allowed = [" The code in this repo is open source."] if label == "README.md" else []
            self.assertEqual(sentences, allowed, label)

    def test_licence_of_code_and_skills_is_stated_and_marked_for_legal_review(self) -> None:
        for label, raw in (("README", read("README.md")), ("CONTRIBUTING", read("CONTRIBUTING.md"))):
            license_part = raw[raw.index("## License\n"):]
            self.assertIn("<!-- LEGAL REVIEW NEEDED", license_part, label)
            text = flat(license_part)
            self.assertIn("A skill has the licence its `license` field names.", text, label)
            self.assertIn("If it names none, no licence is granted beyond reading it and installing it from "
                          "amdahl.ai.", text, label)
            self.assertIn("covers only this repo's code", text, label)
            self.assertLess(text.index("LEGAL REVIEW NEEDED"), text.index("covers only this repo's code"), label)

    def test_e2e_fixture_says_two_scanners_and_that_it_publishes(self) -> None:
        text = flat(read("e2e", "README.md"))
        self.assertNotIn("three scanners", text)
        self.assertIn("the two scanners and the AI security review", text)
        self.assertIn("a clean run lists this skill", text)


class Security(unittest.TestCase):
    def setUp(self) -> None:
        self.text = flat(read("SECURITY.md"))

    def test_every_secret_a_workflow_uses_is_named(self) -> None:
        used = set()
        for path in (ROOT / ".github" / "workflows").glob("*.yml"):
            used |= set(re.findall(r"secrets\.([A-Z0-9_]+)", path.read_text(encoding="utf-8")))
        self.assertEqual(used, {"SKILLS_SCAN_TOKEN", "SKILLS_BOT_PRIVATE_KEY", "SKILLS_REVIEW_ANTHROPIC_KEY"})
        for name in used:
            self.assertIn(f"`{name}`", self.text, name)

    def test_the_model_key_is_marked_as_needing_sign_off(self) -> None:
        self.assertIn("**A model API key. Needs agu's sign-off; not approved yet.** The repo also holds "
                      "`SKILLS_REVIEW_ANTHROPIC_KEY`", self.text)

    def test_residual_risk_is_stated(self) -> None:
        self.assertIn("Residual risk: code that runs inside the AI review step could read the Anthropic key, and "
                      "the runner user that claims and posts holds the token.", self.text)
        self.assertIn("Nothing passes between its steps as a workflow artifact", self.text)
        self.assertNotIn("approved skills", self.text)


if __name__ == "__main__":
    unittest.main()
