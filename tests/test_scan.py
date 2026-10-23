"""scan.py with fake scanner binaries: commands, error handling and the scrubbed environment."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from support import FIXTURES, ScannerEnv, fake_scanner

import scan


class Commands(unittest.TestCase):
    def test_no_model_no_cloud_no_validation(self) -> None:
        d, o = Path("/s/x"), Path("/o/r.json")
        ss = scan.skillspector_command("skillspector", d, o)
        self.assertIn("--no-llm", ss)
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
        self.assertFalse(any(key.startswith("SKILLSPECTOR_PROVIDER") for key in env))


class ScanWithFakes(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.bin = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def fakes(self, **overrides: dict) -> ScannerEnv:
        reports = {
            "skillspector": FIXTURES / "skillspector" / "injection.json",
            "cisco": FIXTURES / "cisco" / "clean.json",
            "betterleaks": FIXTURES / "betterleaks" / "clean.json",
        }
        paths = {}
        for key, report in reports.items():
            options = {"report": report}
            options.update(overrides.get(key, {}))
            paths[key] = fake_scanner(self.bin, key, **options)
        return ScannerEnv(paths["skillspector"], paths["cisco"], paths["betterleaks"])

    def test_all_scanners_ok(self) -> None:
        with self.fakes():
            checks = scan.scan_markdown("meeting-follow-up", "---\nname: x\n---\nhi\n", "2026-10-22")
        self.assertEqual([c["by"] for c in checks],
                         ["NVIDIA SkillSpector", "Cisco AI Defense skill-scanner", "Betterleaks"])
        self.assertEqual([c["result"] for c in checks], ["fail", "pass", "pass"])

    def test_a_crash_is_never_a_pass(self) -> None:
        with self.fakes(cisco={"report": None, "exit_code": 2}, betterleaks={"report": None}):
            checks = scan.scan_markdown("a", "x", "2026-10-22")
        self.assertEqual(checks[1]["result"], "warn")
        self.assertEqual(checks[1]["summary"], "Scanner did not finish")
        self.assertIn("Exit code 2", checks[1]["details"])
        self.assertEqual(checks[2]["summary"], "Scanner did not finish")
        self.assertIn("no report", checks[2]["details"])

    def test_skillspector_exit_1_is_a_finished_scan(self) -> None:
        with self.fakes(skillspector={"exit_code": 1}):
            checks = scan.scan_markdown("a", "x", "2026-10-22")
        self.assertEqual(checks[0]["result"], "fail")

    def test_timeout(self) -> None:
        os.environ["SCAN_TIMEOUT_SECONDS"] = "1"
        try:
            with self.fakes(betterleaks={"sleep": 3}):
                checks = scan.scan_markdown("a", "x", "2026-10-22")
        finally:
            del os.environ["SCAN_TIMEOUT_SECONDS"]
        self.assertEqual(checks[2]["summary"], "Scanner did not finish")
        self.assertIn("Timed out", checks[2]["details"])

    def test_missing_binary(self) -> None:
        with ScannerEnv(self.bin / "nope", self.bin / "nope", self.bin / "nope"):
            checks = scan.scan_markdown("a", "x", "2026-10-22")
        self.assertEqual({c["result"] for c in checks}, {"warn"})
        self.assertTrue(all(c["details"].endswith("not installed") for c in checks))

    def test_token_never_reaches_a_scanner(self) -> None:
        dump = self.bin / "env.txt"
        os.environ["SKILLS_SCAN_TOKEN"] = "secret-token"
        try:
            with self.fakes(skillspector={"env_dump": dump}):
                scan.scan_markdown("a", "x", "2026-10-22")
        finally:
            del os.environ["SKILLS_SCAN_TOKEN"]
        self.assertNotIn("secret-token", dump.read_text(encoding="utf-8"))

    def test_bad_names_never_touch_a_path(self) -> None:
        for name in ("../etc", "a/b", "", "UPPER"):
            with self.assertRaises(ValueError):
                scan.scan_markdown(name, "x")

    def test_oversized_markdown_is_not_scanned(self) -> None:
        with self.fakes():
            checks = scan.scan_markdown("a", "x" * (1024 * 1024 + 1), "2026-10-22")
        self.assertEqual({c["summary"] for c in checks}, {"Scanner did not finish"})


if __name__ == "__main__":
    unittest.main()
