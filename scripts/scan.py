"""Run the three checks on one skill folder and print them as normalized checks.

Usage:
    python3 scripts/scan.py <skill-folder> [--output checks.json]

The checks READ the files. Nothing in the skill is executed. The two scanners
run with a scrubbed environment (no tokens, no model keys), a private HOME
and a timeout. Scanner binaries come from SKILL_SCANNER_BIN and
BETTERLEAKS_BIN, or from PATH. After them, the AI security review
(ai_review.py) sends the SKILL.md text, and nothing else, to the Anthropic API.
It needs ANTHROPIC_API_KEY; without it, that check did not finish.

The scan queue runs the two halves in separate steps (scan_queue.py): the
scanners as an unprivileged user with no secrets (`scanner_checks`), and the
AI review in a step that holds only the model key (`review_markdown`). This
command runs both in one process, for local use.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

sys.path.insert(0, str(Path(__file__).resolve().parent))  # works under `python3 -I` too

import ai_review  # noqa: E402
import normalize  # noqa: E402
from skillmd import MARKDOWN_MAX_BYTES, is_valid_name  # noqa: E402

DEFAULT_TIMEOUT_SECONDS = 300
SCANNERS = (normalize.CISCO, normalize.BETTERLEAKS)


@dataclass
class RunResult:
    """What one scanner run produced: its parsed JSON, or the reason it did not finish."""

    report: object = None
    error: str = ""


def today_utc() -> str:
    """Today's date in UTC as YYYY-MM-DD (the checkedAt format)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def scanner_env(home: Path) -> dict[str, str]:
    """A minimal environment: PATH plus a private HOME. Secrets and model keys never reach a scanner."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "TMPDIR": str(home),
        "LANG": "C.UTF-8",
        "LC_ALL": "C.UTF-8",
        "PYTHONUTF8": "1",
        "PYTHONDONTWRITEBYTECODE": "1",
        "NO_COLOR": "1",
    }


def _binary(env_var: str, default: str) -> str | None:
    configured = os.environ.get(env_var)
    if configured:
        return configured if os.path.isfile(configured) and os.access(configured, os.X_OK) else None
    return shutil.which(default)


def cisco_command(binary: str, skill_dir: Path, out: Path) -> list[str]:
    """Cisco skill-scanner, static analyzers only: no --use-llm, --use-aidefense or --use-virustotal."""
    return [
        binary, "scan", str(skill_dir), "--use-behavioral", "--policy", "balanced",
        "--format", "json", "--output", str(out),
    ]


def betterleaks_command(binary: str, skill_dir: Path, out: Path, empty_dir: Path) -> list[str]:
    """Betterleaks over the folder. Values are redacted, no live validation, and in-file
    allow comments or ignore files cannot hide a finding."""
    return [
        binary, "dir", str(skill_dir), "--no-banner", "--no-color", "--redact",
        "--ignore-gitleaks-allow", "--gitleaks-ignore-path", str(empty_dir),
        "--report-format", "json", "--report-path", str(out),
        "--exit-code", "0", "--log-level", "warn",
    ]


def _tail(text: str, limit: int = 600) -> str:
    text = text.strip()
    return text[-limit:] if len(text) > limit else text


def run_scanner(command: list[str], out: Path, ok_codes: set[int], env: dict[str, str], cwd: Path, timeout: int) -> RunResult:
    """Run one scanner and read its JSON report. Never raises."""
    try:
        proc = subprocess.run(
            command, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            capture_output=True, text=True, errors="replace", timeout=timeout, check=False,
        )
    except subprocess.TimeoutExpired:
        return RunResult(error=f"Timed out after {timeout} seconds")
    except OSError as error:
        return RunResult(error=f"Could not start the scanner: {error.strerror or error}")
    if proc.returncode not in ok_codes:
        return RunResult(error=f"Exit code {proc.returncode}. {_tail(proc.stderr or proc.stdout)}")
    try:
        with open(out, encoding="utf-8") as handle:
            return RunResult(report=json.load(handle))
    except FileNotFoundError:
        return RunResult(error="The scanner wrote no report")
    except (OSError, ValueError) as error:
        return RunResult(error=f"Could not read the report: {error}")


def review_markdown(markdown: str, checked_at: str) -> dict[str, Any]:
    """The AI security review of one SKILL.md text. Never raises: a crash is a check that did not finish."""
    if len(markdown.encode("utf-8")) > MARKDOWN_MAX_BYTES:
        return normalize.did_not_finish(normalize.AI_REVIEW, checked_at, "SKILL.md is larger than 1 MB, so it was not reviewed")
    try:
        return ai_review.review(markdown, checked_at)
    except Exception as error:  # noqa: BLE001 - a bug here must cost one warning, never a pass or the whole queue
        return normalize.did_not_finish(normalize.AI_REVIEW, checked_at, f"The AI review crashed: {type(error).__name__}")


def ai_review_check(skill_dir: Path, checked_at: str) -> dict[str, Any]:
    """The AI security review of skill_dir/SKILL.md. Never raises: a crash is a check that did not finish."""
    try:
        raw = (skill_dir / "SKILL.md").read_bytes()
    except OSError as error:
        return normalize.did_not_finish(normalize.AI_REVIEW, checked_at, f"Could not read SKILL.md: {error.strerror or error}")
    if len(raw) > MARKDOWN_MAX_BYTES:
        return normalize.did_not_finish(normalize.AI_REVIEW, checked_at, "SKILL.md is larger than 1 MB, so it was not reviewed")
    try:
        markdown = raw.decode("utf-8")
    except UnicodeDecodeError:
        return normalize.did_not_finish(normalize.AI_REVIEW, checked_at, "SKILL.md is not UTF-8, so it was not reviewed")
    return review_markdown(markdown, checked_at)


def scanner_checks(skill_dir: Path, checked_at: str | None = None, timeout: int | None = None) -> list[dict[str, Any]]:
    """Run Cisco skill-scanner, then Betterleaks, on skill_dir; return their two checks.

    Needs no secret and no network: the rules-only half of the checks.
    """
    checked_at = checked_at or today_utc()
    timeout = timeout or int(os.environ.get("SCAN_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS))
    skill_dir = skill_dir.resolve()
    with tempfile.TemporaryDirectory(prefix="skill-scan-out-") as work_name:
        work = Path(work_name)
        home, empty = work / "home", work / "empty"
        home.mkdir()
        empty.mkdir()
        env = scanner_env(home)
        checks = []

        binary = _binary("SKILL_SCANNER_BIN", "skill-scanner")
        if binary is None:
            checks.append(normalize.did_not_finish(normalize.CISCO, checked_at, "skill-scanner is not installed"))
        else:
            out = work / "cisco.json"
            run = run_scanner(cisco_command(binary, skill_dir, out), out, {0}, env, work, timeout)
            checks.append(
                normalize.did_not_finish(normalize.CISCO, checked_at, run.error)
                if run.error else normalize.normalize_cisco(run.report, checked_at)
            )

        binary = _binary("BETTERLEAKS_BIN", "betterleaks")
        if binary is None:
            checks.append(normalize.did_not_finish(normalize.BETTERLEAKS, checked_at, "Betterleaks is not installed"))
        else:
            out = work / "betterleaks.json"
            run = run_scanner(betterleaks_command(binary, skill_dir, out, empty), out, {0}, env, work, timeout)
            checks.append(
                normalize.did_not_finish(normalize.BETTERLEAKS, checked_at, run.error)
                if run.error else normalize.normalize_betterleaks(run.report, checked_at)
            )
    return checks


def scan_skill_dir(skill_dir: Path, checked_at: str | None = None, timeout: int | None = None) -> list[dict[str, Any]]:
    """Run Cisco skill-scanner, Betterleaks, then the AI review on skill_dir; return three checks."""
    checked_at = checked_at or today_utc()
    return scanner_checks(skill_dir, checked_at, timeout) + [ai_review_check(skill_dir.resolve(), checked_at)]


def _in_skill_folder(name: str, markdown: str, run: Callable[[Path], list[dict[str, Any]]]) -> list[dict[str, Any]]:
    """Write markdown to a fresh temp folder as <name>/SKILL.md and call run on that folder.

    The name is checked against the skill-name pattern before it touches a path.
    """
    if not is_valid_name(name):
        raise ValueError("invalid skill name")
    with tempfile.TemporaryDirectory(prefix="skill-scan-in-") as root_name:
        skill_dir = Path(root_name) / name
        skill_dir.mkdir()
        with open(skill_dir / "SKILL.md", "w", encoding="utf-8", newline="") as handle:
            handle.write(markdown)
        return run(skill_dir)


def _too_large(pairs: tuple[tuple[str, str], ...], checked_at: str) -> list[dict[str, Any]]:
    reason = "SKILL.md is larger than 1 MB, so it was not scanned"
    return [normalize.did_not_finish(pair, checked_at, reason) for pair in pairs]


def scan_markdown_scanners(name: str, markdown: str, checked_at: str | None = None,
                           timeout: int | None = None) -> list[dict[str, Any]]:
    """The two scanner checks on one SKILL.md text. Never calls the AI review."""
    checked_at = checked_at or today_utc()
    if not is_valid_name(name):
        raise ValueError("invalid skill name")
    if len(markdown.encode("utf-8")) > MARKDOWN_MAX_BYTES:
        return _too_large(SCANNERS, checked_at)
    return _in_skill_folder(name, markdown, lambda folder: scanner_checks(folder, checked_at, timeout))


def scan_markdown(name: str, markdown: str, checked_at: str | None = None) -> list[dict[str, Any]]:
    """All three checks on one SKILL.md text, in one process."""
    checked_at = checked_at or today_utc()
    if not is_valid_name(name):
        raise ValueError("invalid skill name")
    if len(markdown.encode("utf-8")) > MARKDOWN_MAX_BYTES:
        return _too_large(SCANNERS + (normalize.AI_REVIEW,), checked_at)
    return _in_skill_folder(name, markdown, lambda folder: scan_skill_dir(folder, checked_at))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan one skill folder and print normalized checks as JSON.")
    parser.add_argument("skill_dir", type=Path)
    parser.add_argument("--output", type=Path, help="write the checks here instead of stdout")
    args = parser.parse_args(argv)
    if not (args.skill_dir / "SKILL.md").is_file():
        parser.error(f"{args.skill_dir} has no SKILL.md")
    checks = scan_skill_dir(args.skill_dir)
    text = json.dumps(checks, indent=2, ensure_ascii=True)
    if args.output:
        args.output.write_text(text + "\n", encoding="utf-8")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
