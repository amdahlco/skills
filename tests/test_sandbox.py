"""sandbox.sh: the stop that kills every process of the scanner user, and the output that
lets the steps holding the token and the key run.

The real kill runs on GitHub's Linux runner. Here sudo and sleep are fakes on PATH: the
fake sudo logs each call and plays pgrep and kill against a pretend process table, in
which a process of the scanner user survives until a given number of kills (a fork loop
that keeps making new processes). Nothing here runs the real sudo.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import unittest
from pathlib import Path

from support import TESTS

SANDBOX = TESTS.parent / "scripts" / "sandbox.sh"
KILL_CALL = "-n -u skillscan -- kill -s KILL -- -1"

FAKE_SUDO = """#!/bin/sh
printf '%s\\n' "$*" >> "$FAKE_STATE/log"
case "$*" in
  "-n -u skillscan -- kill -s KILL -- -1")
    n=$(cat "$FAKE_STATE/kills"); echo $((n + 1)) > "$FAKE_STATE/kills"; exit 0 ;;
  "pgrep -c -u skillscan") table=effective ;;
  "pgrep -c -U skillscan") table=real ;;
  *) echo "unexpected sudo call: $*" >&2; exit 99 ;;
esac
until=$(cat "$FAKE_STATE/$table")
case "$until" in
  broken) exit 1 ;;
  never) echo 3; exit 0 ;;
esac
if [ "$(cat "$FAKE_STATE/kills")" -lt "$until" ]; then echo 3; exit 0; fi
echo 0
exit 1
"""


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = Path(self._tmp.name)
        self.state = self.dir / "state"
        self.state.mkdir()
        (self.state / "kills").write_text("0\n", encoding="utf-8")
        (self.state / "log").write_text("", encoding="utf-8")
        bin_dir = self.dir / "bin"
        bin_dir.mkdir()
        for name, body in (("sudo", FAKE_SUDO), ("sleep", "#!/bin/sh\nexit 0\n")):
            (bin_dir / name).write_text(body, encoding="utf-8")
            (bin_dir / name).chmod(0o755)
        # The script's functions and settings, without its command dispatch at the end.
        text = SANDBOX.read_text(encoding="utf-8")
        (self.dir / "lib.sh").write_text(text[:text.index('\ncase "${1:-}" in')] + "\n", encoding="utf-8")
        self.env = {"PATH": f"{bin_dir}:/usr/bin:/bin", "FAKE_STATE": str(self.state)}
        self.output = self.dir / "github_output"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def table(self, effective: str, real: str = "0") -> None:
        """Processes seen by effective and by real user id: alive until N kills, never, or broken."""
        (self.state / "effective").write_text(effective, encoding="utf-8")
        (self.state / "real").write_text(real, encoding="utf-8")

    def run_bash(self, body: str, github_output: bool = True) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        if github_output:
            env["GITHUB_OUTPUT"] = str(self.output)
        script = f'. "{self.dir / "lib.sh"}"\n{body}\n'
        return subprocess.run(["bash", "-c", script], env=env, capture_output=True, text=True, timeout=60)

    def kills(self) -> int:
        return int((self.state / "kills").read_text(encoding="utf-8"))

    def calls(self) -> list[str]:
        return (self.state / "log").read_text(encoding="utf-8").splitlines()

    def reported(self) -> str:
        return self.output.read_text(encoding="utf-8") if self.output.exists() else ""


class StopScanUser(Base):
    def test_nothing_running_sends_no_kill(self) -> None:
        self.table("0")
        result = self.run_bash("stop_scan_user")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.kills(), 0)

    def test_the_kill_is_kill_minus_one_sent_as_the_scanner_user(self) -> None:
        # kill(-1) as that user reaches every process it may signal while the kernel
        # holds the process list; pkill signals a list it read earlier.
        self.table("1")
        result = self.run_bash("stop_scan_user")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([call for call in self.calls() if not call.startswith("pgrep ")], [KILL_CALL])

    def test_a_process_forked_past_one_kill_gets_the_next(self) -> None:
        # A fork loop: new processes keep turning up after the first two kills.
        self.table("3")
        result = self.run_bash("stop_scan_user")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.kills(), 3)

    def test_a_process_seen_only_by_its_real_user_id_keeps_the_stop_going(self) -> None:
        # A set-user-id program the scanner user started: its effective id is root's.
        self.table("0", real="2")
        result = self.run_bash("stop_scan_user")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.kills(), 2)

    def test_fails_when_a_process_is_left_after_every_try(self) -> None:
        self.table("never")
        result = self.run_bash("stop_scan_user")
        self.assertEqual(result.returncode, 1)
        self.assertIn("processes of skillscan are still running", result.stderr)
        self.assertEqual(self.kills(), 50)

    def test_a_pgrep_that_cannot_run_counts_as_running(self) -> None:
        self.table("broken")
        result = self.run_bash("stop_scan_user")
        self.assertEqual(result.returncode, 1)
        self.assertIn("still running", result.stderr)


class ReportStopped(Base):
    # scan_items in order: hand over, scan, stop, then the results are copied out.
    SCAN = "trap report_scan_user_stopped EXIT\nscan_user_live=1\nstop_scan_user\nscan_user_live=0"

    def test_reported_after_a_stop_that_finished(self) -> None:
        self.table("2")
        result = self.run_bash(self.SCAN)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.reported(), "scan_user_stopped=true\n")

    def test_not_reported_after_a_stop_that_did_not_finish(self) -> None:
        self.table("never")
        result = self.run_bash(self.SCAN)
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.reported(), "")

    def test_not_reported_when_the_step_ends_in_the_middle_of_the_scan(self) -> None:
        result = self.run_bash("trap report_scan_user_stopped EXIT\nscan_user_live=1\nexit 124")
        self.assertEqual(result.returncode, 124)
        self.assertEqual(self.reported(), "")

    def test_reported_when_the_scan_failed_before_the_scanner_user_started(self) -> None:
        # A failed wall check: no skill was handed over, so the review and post still run.
        result = self.run_bash('trap report_scan_user_stopped EXIT\nfail "the walls did not hold"')
        self.assertEqual(result.returncode, 1)
        self.assertEqual(self.reported(), "scan_user_stopped=true\n")

    def test_no_output_file_keeps_the_exit_status(self) -> None:
        result = self.run_bash("trap report_scan_user_stopped EXIT\nexit 3", github_output=False)
        self.assertEqual(result.returncode, 3)
        self.assertFalse(self.output.exists())


if __name__ == "__main__":
    unittest.main()
