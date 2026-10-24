"""The workflow files: the keepalive that stops GitHub from turning the schedules off, and
how the scan jobs keep their secrets apart (one job, no artifacts, the token in the claim
and post steps only, the model key in the AI review step only, the scanners run as an
unprivileged user).

Standard library only, so these read the YAML as text.
"""

from __future__ import annotations

import re
import unittest

from support import TESTS

import ai_review
import scan_queue

WORKFLOWS = TESTS.parent / ".github" / "workflows"
SANDBOX = TESTS.parent / "scripts" / "sandbox.sh"


def read(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def line_with(text: str, needle: str) -> str:
    """The one stripped line of text that contains needle."""
    found = [line.strip() for line in text.splitlines() if needle in line]
    if len(found) != 1:
        raise AssertionError(f"expected one line with {needle!r}, found {len(found)}")
    return found[0]


def steps(text: str) -> dict[str, str]:
    """Each step's text, by its name, in the file's order."""
    found = {}
    for chunk in re.split(r"(?m)^      - name: ", text)[1:]:
        name, _, body = chunk.partition("\n")
        found[name.strip()] = body
    return found


def step_env(body: str) -> dict[str, str]:
    """The variables in one step's env block."""
    env: dict[str, str] = {}
    lines = body.splitlines()
    if "        env:" not in lines:
        return env
    for line in lines[lines.index("        env:") + 1:]:
        if not line.startswith("          "):
            break
        key, _, value = line.strip().partition(": ")
        env[key] = value
    return env


def holders(text: str, needle: str) -> list[str]:
    return [name for name, body in steps(text).items() if needle in body]


# Root's own shell and Python (scripts/sandbox.sh STEP_SHELL and SYSTEM_PYTHON): the
# scanner user cannot change them, and walls.py proves it before the scan.
ROOT_SHELL = "/usr/bin/bash --noprofile --norc -eo pipefail {0}"
ROOT_PYTHON = "/usr/bin/python3 -I -S"


def job_shell(text: str) -> str:
    """The job's default run shell."""
    found = re.findall(r"(?m)^    defaults:\n      run:\n        shell: (.+)$", text)
    if len(found) != 1:
        raise AssertionError(f"expected one job-wide default shell, found {len(found)}")
    return found[0].strip()


def python_runs(body: str) -> list[str]:
    """Each Python command line in a step, from the interpreter to its first argument."""
    return re.findall(r"(\S*python3(?: -\S+)*) \S+\.py", body)


class Keepalive(unittest.TestCase):
    # GitHub turns off scheduled workflows in a public repository after 60 days
    # with no activity. No skill publishes until its scheduled checks have run.

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

    def test_comment_says_checks_not_approvals(self) -> None:
        self.assertNotIn("Approve", self.text)
        self.assertIn("no submitted skill can publish on amdahl.ai", " ".join(self.text.replace("#", " ").split()))


class NoArtifacts(unittest.TestCase):
    # This repo is public: anyone can download its workflow artifacts and caches.
    def test_no_workflow_stores_artifacts_or_caches(self) -> None:
        for path in WORKFLOWS.glob("*.yml"):
            text = path.read_text(encoding="utf-8")
            for action in ("upload-artifact", "download-artifact", "actions/cache"):
                self.assertNotIn(action, text, path.name)


class ScanQueue(unittest.TestCase):
    def setUp(self) -> None:
        self.text = read("scan-queue.yml")
        self.steps = steps(self.text)

    def test_one_job(self) -> None:
        jobs = self.text.split("\njobs:\n", 1)[1]
        self.assertEqual(re.findall(r"(?m)^  ([A-Za-z0-9_-]+):\s*$", jobs), ["scan"])

    def test_no_workflow_or_job_wide_env(self) -> None:
        # Every variable is set on the one step that needs it.
        self.assertNotRegex(self.text, r"(?m)^env:")
        self.assertNotRegex(self.text, r"(?m)^    env:")

    def test_the_token_reaches_the_claim_and_post_steps_only(self) -> None:
        self.assertEqual(holders(self.text, "SKILLS_SCAN_TOKEN"), ["Claim the queue", "Post the results"])
        for name in ("Claim the queue", "Post the results"):
            self.assertEqual(step_env(self.steps[name]).get("SKILLS_SCAN_TOKEN"), "${{ secrets.SKILLS_SCAN_TOKEN }}")
        self.assertEqual(self.text.count("secrets.SKILLS_SCAN_TOKEN"), 2)
        self.assertIn("scan_queue.py claim --out", self.steps["Claim the queue"])
        self.assertIn("scan_queue.py post --dir", self.steps["Post the results"])

    def test_the_key_reaches_the_ai_review_step_only_and_alone(self) -> None:
        self.assertEqual(holders(self.text, "ANTHROPIC_API_KEY"), ["AI security review"])
        self.assertEqual(step_env(self.steps["AI security review"]),
                         {"ANTHROPIC_API_KEY": "${{ secrets.SKILLS_REVIEW_ANTHROPIC_KEY }}"})
        self.assertEqual(self.text.count("secrets.SKILLS_REVIEW_ANTHROPIC_KEY"), 1)
        self.assertIn("scan_queue.py ai-review --items", self.steps["AI security review"])

    def test_the_scanners_run_as_the_unprivileged_user(self) -> None:
        expected = {
            "Install the scanners": "bash scripts/sandbox.sh install",
            "Make the scanner user": "bash scripts/sandbox.sh setup",
            "Run the scanners as the scanner user": "bash scripts/sandbox.sh scan ",
        }
        for name, command in expected.items():
            self.assertIn(f"run: {command}", self.steps[name], name)
            self.assertEqual(step_env(self.steps[name]), {}, name)
        # The scanners never run as the runner user: only sandbox.sh starts the scan subcommand.
        self.assertNotIn("scan_queue.py scan", self.text)
        self.assertNotIn("install_scanners.sh", self.text)

    def test_step_order(self) -> None:
        self.assertEqual(list(self.steps), [
            "Check out the scan code", "Set up Python", "Keep the temp folder private", "Claim the queue",
            "Install the scanners", "Make the scanner user", "Run the scanners as the scanner user",
            "AI security review", "Post the results",
        ])

    def test_the_temp_folder_is_closed_first(self) -> None:
        self.assertIn('run: chmod 700 "$HOME" "$RUNNER_TEMP"', self.steps["Keep the temp folder private"])

    def test_review_and_post_still_run_after_a_failed_scanner_step(self) -> None:
        for name in ("AI security review", "Post the results"):
            self.assertIn("!cancelled() && steps.queue.outcome == 'success'", self.steps[name], name)

    def test_review_and_post_never_run_while_the_scanner_user_may_still_run(self) -> None:
        # The key and the token: after a failed scan step, only on sandbox.sh's word that none is left.
        self.assertIn("        id: scan\n", self.steps["Run the scanners as the scanner user"])
        gate = "&& (steps.scan.outcome != 'failure' || steps.scan.outputs.scan_user_stopped == 'true') }}"
        for name in ("AI security review", "Post the results"):
            self.assertTrue(line_with(self.steps[name], "if: ").endswith(gate), name)

    def test_every_python_run_is_roots_python_without_site(self) -> None:
        # Not setup-python's Python, which lives in /opt, and no site module, so no .pth file runs.
        runs = [run for body in self.steps.values() for run in python_runs(body)]
        self.assertEqual(len(runs), 3)
        self.assertEqual(set(runs), {ROOT_PYTHON})
        self.assertEqual(re.findall(r"python3 -m", self.text), [])

    def test_every_step_runs_in_roots_shell_by_its_full_path(self) -> None:
        # A bare "bash" is looked up on PATH, and /opt folders come before /usr/bin there.
        self.assertEqual(job_shell(self.text), ROOT_SHELL)
        self.assertEqual(len(re.findall(r"(?m)^\s*shell:", self.text)), 1, "no step may set its own shell")


class SelfTest(unittest.TestCase):
    def setUp(self) -> None:
        self.text = read("self-test.yml")
        self.steps = steps(self.text)

    def test_the_key_reaches_the_sample_review_only_and_alone(self) -> None:
        self.assertEqual(holders(self.text, "ANTHROPIC_API_KEY"), ["AI security review"])
        self.assertEqual(step_env(self.steps["AI security review"]),
                         {"ANTHROPIC_API_KEY": "${{ secrets.SKILLS_REVIEW_ANTHROPIC_KEY }}"})
        self.assertNotIn("ANTHROPIC", self.steps["Unit tests"])
        self.assertNotIn("SKILLS_SCAN_TOKEN", self.text)

    def test_it_runs_the_scanners_the_way_the_queue_does(self) -> None:
        queue = steps(read("scan-queue.yml"))
        for name in ("Keep the temp folder private", "Install the scanners", "Make the scanner user"):
            self.assertEqual(self.steps[name].split("run: ")[1].strip(), queue[name].split("run: ")[1].strip(), name)
        self.assertIn("run: bash scripts/sandbox.sh scan ", self.steps["Run the scanners as the scanner user"])
        self.assertIn("self_test.py check --dir", self.steps["Check the verdicts"])

    def test_steps_after_the_scanner_user_exists_run_roots_python_and_shell(self) -> None:
        self.assertEqual(job_shell(self.text), ROOT_SHELL)
        self.assertEqual(len(re.findall(r"(?m)^\s*shell:", self.text)), 1, "no step may set its own shell")
        names = list(self.steps)
        after = names[names.index("Make the scanner user"):]
        runs = [run for name in after for run in python_runs(self.steps[name])]
        self.assertEqual(len(runs), 3)
        self.assertEqual(set(runs), {ROOT_PYTHON})
        self.assertIn("python3 -m unittest", self.steps["Unit tests"])
        self.assertLess(names.index("Unit tests"), names.index("Make the scanner user"))

    def test_no_other_workflow_sees_the_key(self) -> None:
        for path in WORKFLOWS.glob("*.yml"):
            if path.name not in ("scan-queue.yml", "self-test.yml"):
                self.assertNotIn("ANTHROPIC", path.read_text(encoding="utf-8"), path.name)


def step_minutes(body: str) -> int:
    """A step's timeout-minutes."""
    found = re.findall(r"(?m)^        timeout-minutes: (\d+)$", body)
    if len(found) != 1:
        raise AssertionError(f"expected one timeout-minutes, found {len(found)}")
    return int(found[0])


class AiReviewTime(unittest.TestCase):
    # The ai-review step stops waiting at scan_queue.AI_RUN_SECONDS, so GitHub never kills it first:
    # an item with no review then gets the did-not-finish row from the script, never a pass.

    def test_the_run_deadline_leaves_room_in_each_ai_review_step(self) -> None:
        for name in ("scan-queue.yml", "self-test.yml"):
            body = steps(read(name))["AI security review"]
            self.assertIn("scan_queue.py ai-review --items", body, name)
            self.assertGreaterEqual(step_minutes(body) * 60 - scan_queue.AI_RUN_SECONDS, 120, name)

    def test_one_full_review_fits_before_the_deadline(self) -> None:
        self.assertGreaterEqual(scan_queue.AI_RUN_SECONDS, ai_review.TIMEOUT_SECONDS + ai_review.START_MIN_SECONDS)

    def test_the_scan_job_has_room_for_every_step(self) -> None:
        text = read("scan-queue.yml")
        job = int(re.findall(r"(?m)^    timeout-minutes: (\d+)$", text)[0])
        self.assertLessEqual(sum(step_minutes(body) for body in steps(text).values()
                                 if "timeout-minutes:" in body), job)


class Sandbox(unittest.TestCase):
    def setUp(self) -> None:
        self.text = SANDBOX.read_text(encoding="utf-8")
        self.code = "\n".join(line for line in self.text.splitlines() if not line.lstrip().startswith("#"))

    def test_the_user_has_no_sudo_no_groups_and_a_closed_home(self) -> None:
        self.assertIn('sudo useradd --create-home --user-group --shell /usr/sbin/nologin \\\n    -K SUB_UID_COUNT=0 -K SUB_GID_COUNT=0 "$SCAN_USER"', self.code)
        self.assertIn('fail "$SCAN_USER has subordinate id ranges"', self.code)
        self.assertIn('sudo tee -a /etc/cron.deny /etc/at.deny', self.code)
        self.assertIn('sudo chmod 700 "$SCAN_HOME"', self.code)
        self.assertIn('[ "$groups" = "$SCAN_USER" ]', self.code)
        self.assertIn('sudo -n -l -U "$SCAN_USER"', self.code)
        self.assertIn('*"may run"*) fail', self.code)
        self.assertNotRegex(self.code, r"usermod|sudoers|visudo|--groups|-G ")

    def test_the_scan_runs_as_the_user_with_an_empty_environment(self) -> None:
        line = line_with(self.code, 'env -i -C "$WORK" PATH=')
        self.assertIn('sudo -n -u "$SCAN_USER" -- env -i -C "$WORK"', line)
        self.assertEqual(re.findall(r"\b([A-Z_]+)=", line), ["PATH", "HOME", "TMPDIR"])
        self.assertIn('HOME="$WORK" TMPDIR="$WORK"', line)
        self.assertIn('scan_queue.py" scan --items "$WORK/items.json" --out "$WORK/scanners.json"', self.code)
        for secret in ("SKILLS_SCAN_TOKEN", "ANTHROPIC_API_KEY", "secrets."):
            self.assertNotIn(secret, self.text)

    def test_results_are_copied_out_only_after_the_user_is_stopped(self) -> None:
        scan = self.code[self.code.index("scan_items() {"):]
        self.assertLess(scan.index("env -i"), scan.index("stop_scan_user"))
        self.assertLess(scan.index("stop_scan_user"), scan.index('sudo head -c "$MAX_RESULT_BYTES"'))
        self.assertIn('sudo test ! -L "$src"', scan)

    def test_the_stop_kills_with_kill_minus_one_as_the_user_until_none_is_left(self) -> None:
        # pkill signals a list it read first, so a process forked after that read survived it.
        stop = self.code[self.code.index("stop_scan_user() {"):self.code.index("scan_user_live=0")]
        loop = stop[stop.index("while scan_user_running; do"):stop.index("done")]
        self.assertIn("as_scan_user kill -s KILL -- -1", loop)
        self.assertNotIn("pkill", self.code)

    def test_the_later_steps_are_told_only_when_no_process_of_the_user_is_left(self) -> None:
        scan = self.code[self.code.index("scan_items() {"):self.code.index('case "${1:-}" in')]
        live, run = scan.index("scan_user_live=1"), scan.index('timeout --kill-after=10 "$BATCH_SECONDS"')
        self.assertLess(scan.index("walls.py\" check"), live)
        self.assertLess(live, run)
        self.assertIn("  stop_scan_user\n  scan_user_live=0\n", scan)
        self.assertLess(scan.index("scan_user_live=0"), scan.index('sudo head -c "$MAX_RESULT_BYTES"'))
        dispatch = self.code[self.code.index('case "${1:-}" in'):]
        self.assertLess(dispatch.index("trap report_scan_user_stopped EXIT"), dispatch.index('scan_items "$2" "$3"'))
        self.assertIn('echo "scan_user_stopped=true" >> "$GITHUB_OUTPUT"', self.code)

    def test_the_walls_are_checked_before_any_skill_is_handed_over(self) -> None:
        scan = self.code[self.code.index("scan_items() {"):]
        self.assertLess(scan.index('as_scan_user test -x "$HOME"'), scan.index("sudo install -o"))

    def test_what_later_steps_run_is_closed_before_the_user_exists(self) -> None:
        setup = self.code[self.code.index("setup_user() {"):self.code.index("as_scan_user() {")]
        lock = line_with(setup, "walls.py")
        self.assertIn('sudo "$SYSTEM_PYTHON" -I -S "$repo_root/scripts/walls.py" lock', lock)
        self.assertIn('--path "$PATH" --ld-path "${LD_LIBRARY_PATH:-}"', lock)
        self.assertLess(setup.index("walls.py"), setup.index("sudo useradd"))

    def test_the_user_is_proven_unable_to_change_later_steps_before_any_skill(self) -> None:
        scan = self.code[self.code.index("scan_items() {"):]
        start = scan.index('as_scan_user env -i -C "$WORK" "$SYSTEM_PYTHON" -I -S "$OPT/scripts/walls.py" check')
        call = scan[start:scan.index("sudo install -o")]
        self.assertIn('--path "$job_path" --ld-path "$job_ld" --run "$STEP_SHELL" --run "$SYSTEM_PYTHON"', call)
        self.assertIn("|| fail ", call)
        # The job's PATH and LD_LIBRARY_PATH are what is checked; this script then uses root's folders only.
        self.assertIn('job_path="$PATH" job_ld="${LD_LIBRARY_PATH:-}"', scan)
        self.assertLess(scan.index('PATH="$SAFE_PATH"'), start)
        self.assertLess(scan.index("unset LD_LIBRARY_PATH"), start)
        self.assertIn('[ -z "${LD_PRELOAD:-}${LD_AUDIT:-}" ] || fail', scan)

    def test_the_shell_and_python_it_proves_are_the_ones_the_workflows_run(self) -> None:
        self.assertIn('STEP_SHELL="/usr/bin/bash"', self.code)
        self.assertIn('SYSTEM_PYTHON="/usr/bin/python3"', self.code)
        self.assertIn('SAFE_PATH="/usr/sbin:/usr/bin:/sbin:/bin"', self.code)
        self.assertTrue(ROOT_SHELL.startswith("/usr/bin/bash "))
        self.assertTrue(ROOT_PYTHON.startswith("/usr/bin/python3 "))
        self.assertIn('"$SYSTEM_PYTHON" -I "$OPT/scripts/scan_queue.py" scan', self.code)

    def test_scan_code_and_scanners_are_owned_by_root(self) -> None:
        self.assertIn('sudo chown -R root:root "$OPT"', self.code)
        self.assertIn('sudo chmod -R go-w "$OPT"', self.code)
        self.assertIn('GITHUB_ENV="" bash', self.code)


if __name__ == "__main__":
    unittest.main()
