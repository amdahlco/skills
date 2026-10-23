"""scan.py with fake scanner binaries and a fake model API: commands, error handling and the scrubbed environment."""

from __future__ import annotations

import os
import tempfile
import unittest
import urllib.request
from pathlib import Path
from unittest import mock

from support import FIXTURES, FakeAnthropic, ScannerEnv, fake_scanner, model_answer

import scan


class Commands(unittest.TestCase):
    def test_no_model_no_cloud_no_validation(self) -> None:
        d, o = Path("/s/x"), Path("/o/r.json")
        cisco = scan.cisco_command("skill-scanner", d, o)
        for flag in ("--use-llm", "--use-aidefense", "--use-virustotal", "--use-osv", "--adjudicate"):
            self.assertNotIn(flag, cisco)
        bl = scan.betterleaks_command("betterleaks", d, o, Path("/e"))
        self.assertIn("--redact", bl)
        self.assertIn("--ignore-gitleaks-allow", bl)
        self.assertNotIn("--validation", bl)

    def test_env_has_no_secrets(self) -> None:
        os.environ["SKILLS_SCAN_TOKEN"] = "secret-token"
        os.environ["ANTHROPIC_API_KEY"] = "secret-key"
        try:
            env = scan.scanner_env(Path("/h"))
        finally:
            del os.environ["SKILLS_SCAN_TOKEN"], os.environ["ANTHROPIC_API_KEY"]
        self.assertNotIn("SKILLS_SCAN_TOKEN", env)
        self.assertNotIn("ANTHROPIC_API_KEY", env)


class ScanWithFakes(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def fakes(self, model_key: str | None = None, **overrides: dict) -> ScannerEnv:
        reports = {
            "cisco": FIXTURES / "cisco" / "injection.json",
            "betterleaks": FIXTURES / "betterleaks" / "clean.json",
        }
        paths = {}
        for key, report in reports.items():
            options = {"report": report}
            options.update(overrides.get(key, {}))
            paths[key] = fake_scanner(self.bin, key, **options)
        return ScannerEnv(paths["cisco"], paths["betterleaks"], model_key=model_key)

    def test_all_checks_ok(self) -> None:
        markdown = "---\nname: x\n---\nhi\n"
        api = FakeAnthropic(model_answer({"purpose": "says hi", "verdict": "pass", "findings": []}))
        with self.fakes(model_key="test-key"), mock.patch.object(urllib.request, "urlopen", api):
            checks = scan.scan_markdown("meeting-follow-up", markdown, "2026-10-22")
        self.assertEqual([c["by"] for c in checks],
                         ["Cisco AI Defense skill-scanner", "Betterleaks", "Amdahl AI review"])
        self.assertEqual([c["name"] for c in checks], ["Threat analysis", "Secrets", "AI security review"])
        self.assertEqual([c["result"] for c in checks], ["fail", "pass", "pass"])
        # The AI review got the SKILL.md the scanners read, byte for byte, one numbered line each.
        self.assertEqual(api.models, ["claude-haiku-5-5"])
        prompt = api.requests[0]["body"]["messages"][0]["content"]
        self.assertIn("<skill_md>\n1| ---\n2| name: x\n3| ---\n4| hi\n</skill_md>", prompt)

    def test_no_model_key_is_did_not_finish(self) -> None:
        api = FakeAnthropic()
        with self.fakes(), mock.patch.object(urllib.request, "urlopen", api):
            checks = scan.scan_markdown("a", "x", "2026-10-22")
        self.assertEqual((checks[2]["result"], checks[2]["summary"]), ("warn", "Scanner did not finish"))
        self.assertIn("ANTHROPIC_API_KEY is not set", checks[2]["details"])
        self.assertEqual(api.requests, [])

    def test_a_review_crash_is_never_a_pass(self) -> None:
        with self.fakes(model_key="test-key"), mock.patch.object(scan.ai_review, "review", side_effect=KeyError("x")):
            checks = scan.scan_markdown("a", "x", "2026-10-22")
        self.assertEqual((checks[2]["result"], checks[2]["summary"]), ("warn", "Scanner did not finish"))
        self.assertIn("crashed: KeyError", checks[2]["details"])

    def test_a_crash_is_never_a_pass(self) -> None:
        with self.fakes(cisco={"report": None, "exit_code": 2}, betterleaks={"report": None}):
            checks = scan.scan_markdown("a", "x", "2026-10-22")
        self.assertEqual(checks[0]["result"], "warn")
        self.assertEqual(checks[0]["summary"], "Scanner did not finish")
        self.assertIn("Exit code 2", checks[0]["details"])
        self.assertEqual(checks[1]["summary"], "Scanner did not finish")
        self.assertIn("no report", checks[1]["details"])

    def test_timeout(self) -> None:
        os.environ["SCAN_TIMEOUT_SECONDS"] = "1"
        try:
            with self.fakes(betterleaks={"sleep": 3}):
                checks = scan.scan_markdown("a", "x", "2026-10-22")
        finally:
            del os.environ["SCAN_TIMEOUT_SECONDS"]
        self.assertEqual(checks[1]["summary"], "Scanner did not finish")
        self.assertIn("Timed out", checks[1]["details"])

    def test_missing_binary(self) -> None:
        with ScannerEnv(self.bin / "nope", self.bin / "nope"):
            checks = scan.scan_markdown("a", "x", "2026-10-22")
        self.assertEqual({c["result"] for c in checks}, {"warn"})
        self.assertTrue(all(c["details"].endswith("not installed") for c in checks[:2]))

    def test_secrets_never_reach_a_scanner(self) -> None:
        dump = self.bin / "env.txt"
        os.environ["SKILLS_SCAN_TOKEN"] = "secret-token"
        try:
            api = FakeAnthropic(model_answer({"purpose": "p", "verdict": "pass", "findings": []}))
            with self.fakes(model_key="secret-model-key", cisco={"env_dump": dump}), \
                    mock.patch.object(urllib.request, "urlopen", api):
                scan.scan_markdown("a", "x", "2026-10-22")
        finally:
            del os.environ["SKILLS_SCAN_TOKEN"]
        seen = dump.read_text(encoding="utf-8")
        self.assertNotIn("secret-token", seen)
        self.assertNotIn("secret-model-key", seen)

    def test_bad_names_never_touch_a_path(self) -> None:
        for name in ("../etc", "a/b", "", "UPPER"):
            with self.assertRaises(ValueError):
                scan.scan_markdown(name, "x")

    def test_oversized_markdown_is_not_scanned(self) -> None:
        api = FakeAnthropic()
        with self.fakes(model_key="test-key"), mock.patch.object(urllib.request, "urlopen", api):
            checks = scan.scan_markdown("a", "x" * (1024 * 1024 + 1), "2026-10-22")
        self.assertEqual({c["summary"] for c in checks}, {"Scanner did not finish"})
        self.assertEqual([c["by"] for c in checks],
                         ["Cisco AI Defense skill-scanner", "Betterleaks", "Amdahl AI review"])
        self.assertEqual(api.requests, [])

    def test_oversized_folder_skill_is_not_reviewed(self) -> None:
        folder = self.bin / "big"
        folder.mkdir()
        (folder / "SKILL.md").write_text("x" * (1024 * 1024 + 1), encoding="utf-8")
        api = FakeAnthropic()
        with ScannerEnv(self.bin / "nope", self.bin / "nope", model_key="test-key"), \
                mock.patch.object(urllib.request, "urlopen", api):
            check = scan.scan_skill_dir(folder, "2026-10-22")[2]
        self.assertEqual((check["result"], check["summary"]), ("warn", "Scanner did not finish"))
        self.assertEqual(api.requests, [])

    def test_the_scanner_half_never_calls_the_model(self) -> None:
        # The queue's scanner step runs as the scanner user with no key; this half must not need one.
        api = FakeAnthropic()
        with self.fakes(model_key="test-key"), mock.patch.object(urllib.request, "urlopen", api):
            checks = scan.scan_markdown_scanners("a", "x", "2026-10-22", timeout=30)
            oversized = scan.scan_markdown_scanners("a", "x" * (1024 * 1024 + 1), "2026-10-22")
        self.assertEqual([c["by"] for c in checks], ["Cisco AI Defense skill-scanner", "Betterleaks"])
        self.assertEqual([c["result"] for c in checks], ["fail", "pass"])
        self.assertEqual([c["by"] for c in oversized], ["Cisco AI Defense skill-scanner", "Betterleaks"])
        self.assertEqual({c["summary"] for c in oversized}, {"Scanner did not finish"})
        self.assertEqual(api.requests, [])
        with self.assertRaises(ValueError):
            scan.scan_markdown_scanners("../x", "x")

    def test_review_markdown_is_the_ai_half(self) -> None:
        api = FakeAnthropic(model_answer({"purpose": "p", "verdict": "pass", "findings": []}))
        with self.fakes(model_key="test-key"), mock.patch.object(urllib.request, "urlopen", api):
            check = scan.review_markdown("---\nname: a\n---\nhi\n", "2026-10-22")
            big = scan.review_markdown("x" * (1024 * 1024 + 1), "2026-10-22")
        self.assertEqual((check["by"], check["result"]), ("Amdahl AI review", "pass"))
        self.assertEqual(big["summary"], "Scanner did not finish")
        self.assertEqual(len(api.requests), 1)


if __name__ == "__main__":
    unittest.main()
