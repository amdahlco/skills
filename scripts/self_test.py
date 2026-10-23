"""Run the real checks on known samples and check each verdict.

Usage:
    python3 scripts/self_test.py                  # every check in this process (local runs)
    python3 scripts/self_test.py items --out DIR  # write the samples as DIR/items.json
    python3 scripts/self_test.py check --dir DIR  # check DIR/scanners.json and DIR/ai.json

.github/workflows/self-test.yml runs it the way scan-queue.yml runs the queue:
`items`, then the scanners as the unprivileged scanner user (scripts/sandbox.sh
scan), then the AI review in its own step (scan_queue.py ai-review), then
`check`. So a scanner upgrade, a broken install, a broken scanner user or a
parsing change that would let a bad skill through fails loudly here instead of
quietly on the queue.

Samples:
  clean      tests/fixtures/inputs/discovery-call-notes.md: every check passes.
  injection  tests/fixtures/inputs/meeting-follow-up.md: hidden text telling the
             agent to send ~/.ssh away; Cisco skill-scanner and the AI review
             must fail it.
  secret     a GitHub token shape built at run time (never committed, so no
             real or fake secret sits in this repo): Betterleaks and the AI
             review must not pass it.

The AI review runs only when ANTHROPIC_API_KEY is set (in Actions, from the
SKILLS_REVIEW_ANTHROPIC_KEY secret). When it runs, each of its verdicts must
also come from ai_review.MODEL (Claude Sonnet 5.5), the one model that reviews
every skill. Without the key, the test checks Cisco skill-scanner and
Betterleaks, says the AI review was skipped, and still requires the AI check to
be the "did not finish" warning, never a pass.

Every check that runs must also have really finished: a "did not finish"
result fails the test.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import random
import string
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

import ai_review  # noqa: E402
import handoff  # noqa: E402
import normalize  # noqa: E402

SECRET_SKILL = """---
name: crm-sync
description: Syncs notes to the CRM. Use when the user asks to push call notes to the CRM.
---

# CRM sync

Call the API with this token: {token}

## Steps

1. Read the notes.
2. Send them.
"""

AI = normalize.AI_REVIEW[1]
EXPECTED = {
    "clean": {normalize.CISCO[1]: {"pass"}, normalize.BETTERLEAKS[1]: {"pass"}, AI: {"pass"}},
    "injection": {normalize.CISCO[1]: {"fail"}, AI: {"fail"}},
    "secret": {normalize.BETTERLEAKS[1]: {"warn", "fail"}, AI: {"warn", "fail"}},
}
# Each sample's place in items.json, and the skill name it is scanned under.
SAMPLES = (("clean", "discovery-call-notes"), ("injection", "meeting-follow-up"), ("secret", "crm-sync"))


def fake_token() -> str:
    rng = random.SystemRandom()
    return "ghp_" + "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(36))


def sample_texts() -> dict[str, str]:
    """The three samples' SKILL.md text, by sample name."""
    inputs = ROOT / "tests" / "fixtures" / "inputs"
    return {
        "clean": (inputs / "discovery-call-notes.md").read_text(encoding="utf-8"),
        "injection": (inputs / "meeting-follow-up.md").read_text(encoding="utf-8"),
        "secret": SECRET_SKILL.format(token=fake_token()),
    }


def from_model(check: dict) -> bool:
    """True when a finished AI review check says ai_review.MODEL gave its verdict (the first line of details)."""
    details = check.get("details")
    head = details.split("\n", 1)[0] if isinstance(details, str) else ""
    return head.startswith("Verdict: ") and f", from {ai_review.MODEL}." in head


def failures(checks_by_sample: dict[str, list[dict]], ai_enabled: bool) -> list[str]:
    """Every way the checks differ from what each sample must get."""
    found: list[str] = []
    for name, checks in checks_by_sample.items():
        by = {c["by"]: c for c in checks}
        print(f"{name}: " + ", ".join(f"{c['by']}={c['result']}" for c in checks))
        for check in checks:
            if check["by"] == AI and not ai_enabled:
                if (check["result"], check["summary"]) != ("warn", normalize.DID_NOT_FINISH):
                    found.append(f"{name}: {AI} gave {check['result']} with no key, expected did not finish")
                continue
            if check["summary"] == normalize.DID_NOT_FINISH:
                found.append(f"{name}: {check['by']} did not finish")
            elif check["by"] == AI and not from_model(check):
                found.append(f"{name}: {AI} gave no verdict from {ai_review.MODEL}")
        for scanner, allowed in EXPECTED[name].items():
            if scanner == AI and not ai_enabled:
                continue
            got = by.get(scanner, {}).get("result")
            if got not in allowed:
                found.append(f"{name}: {scanner} gave {got}, expected one of {sorted(allowed)}")
    return found


def report(found: list[str], ai_enabled: bool) -> int:
    if not ai_enabled:
        print("AI security review skipped: ANTHROPIC_API_KEY is not set (secret SKILLS_REVIEW_ANTHROPIC_KEY).")
    if found:
        print("Self-test FAILED:")
        for line in found:
            print(f"  - {line}")
        return 1
    ran = "every check" if ai_enabled else "both scanners (the AI review was skipped)"
    print(f"Self-test passed: {ran} ran and gave the expected verdict.")
    return 0


def scan_in_process(markdown: str) -> list[dict]:
    with tempfile.TemporaryDirectory() as tmp:
        folder = Path(tmp) / "skill"
        folder.mkdir()
        (folder / "SKILL.md").write_text(markdown, encoding="utf-8")
        out = Path(tmp) / "checks.json"
        subprocess.run(
            [sys.executable, "-I", str(ROOT / "scripts" / "scan.py"), str(folder), "--output", str(out)],
            check=True,
            env=os.environ.copy(),
            timeout=600,
        )
        return json.loads(out.read_text(encoding="utf-8"))


def cmd_local() -> int:
    ai_enabled = bool(os.environ.get("ANTHROPIC_API_KEY", "").strip())
    checks = {name: scan_in_process(markdown) for name, markdown in sample_texts().items()}
    return report(failures(checks, ai_enabled), ai_enabled)


def cmd_items(out: Path) -> int:
    """Write the samples as items.json, the file the queue's claim step writes (no ids)."""
    out.mkdir(mode=0o700, parents=True, exist_ok=True)
    texts = sample_texts()
    items = [{"position": position, "name": skill, "markdown": texts[sample]}
             for position, (sample, skill) in enumerate(SAMPLES)]
    handoff.write_private(out / handoff.ITEMS, {"items": items})
    print(f"Wrote {len(items)} samples")
    return 0


def cmd_check(folder: Path) -> int:
    """Build each sample's three checks the way the post step does, then check the verdicts."""
    ai_payload = handoff.read_json(folder / handoff.AI)
    ai_enabled = isinstance(ai_payload, dict) and ai_payload.get("keyed") is True
    scanners = handoff.by_position(handoff.read_json(folder / handoff.SCANNERS))
    ai = handoff.by_position(ai_payload)
    day = handoff.today_utc()
    checks = {sample: handoff.assemble(position, scanners, ai, day) for position, (sample, _) in enumerate(SAMPLES)}
    return report(failures(checks, ai_enabled), ai_enabled)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the real checks on known samples and check each verdict.")
    sub = parser.add_subparsers(dest="command")
    items = sub.add_parser("items", help="write the samples as items.json")
    items.add_argument("--out", type=Path, required=True)
    check = sub.add_parser("check", help="check scanners.json and ai.json against the expected verdicts")
    check.add_argument("--dir", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "items":
        return cmd_items(args.out)
    if args.command == "check":
        return cmd_check(args.dir)
    return cmd_local()


if __name__ == "__main__":
    sys.exit(main())
