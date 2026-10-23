"""Run the three real scanners on known samples and check each verdict.

Usage:
    python3 scripts/self_test.py

Runs in .github/workflows/self-test.yml on every push, so a scanner upgrade,
a broken install or a parsing change that would let a bad skill through
fails loudly here instead of quietly on the queue.

Samples:
  clean      tests/fixtures/inputs/discovery-call-notes.md: every scanner passes.
  injection  tests/fixtures/inputs/meeting-follow-up.md: hidden text telling the
             agent to send ~/.ssh away; both content scanners must fail it.
  secret     a GitHub token shape built at run time (never committed, so no
             real or fake secret sits in this repo): Betterleaks must not pass it.

Every check must also have really run: a "did not finish" result fails the test.

Standard library only.
"""

from __future__ import annotations

import json
import os
import random
import shutil
import string
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "scripts"))

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

EXPECTED = {
    "clean": {normalize.SKILLSPECTOR[1]: {"pass"}, normalize.CISCO[1]: {"pass"}, normalize.BETTERLEAKS[1]: {"pass"}},
    "injection": {normalize.SKILLSPECTOR[1]: {"fail"}, normalize.CISCO[1]: {"fail"}},
    "secret": {normalize.BETTERLEAKS[1]: {"warn", "fail"}},
}


def fake_token() -> str:
    rng = random.SystemRandom()
    return "ghp_" + "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(36))


def scan(markdown: str) -> list[dict]:
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


def main() -> int:
    inputs = ROOT / "tests" / "fixtures" / "inputs"
    samples = {
        "clean": (inputs / "discovery-call-notes.md").read_text(encoding="utf-8"),
        "injection": (inputs / "meeting-follow-up.md").read_text(encoding="utf-8"),
        "secret": SECRET_SKILL.format(token=fake_token()),
    }
    failures: list[str] = []
    for name, markdown in samples.items():
        checks = scan(markdown)
        by = {c["by"]: c for c in checks}
        print(f"{name}: " + ", ".join(f"{c['by']}={c['result']}" for c in checks))
        for check in checks:
            if check["summary"] == normalize.DID_NOT_FINISH:
                failures.append(f"{name}: {check['by']} did not finish")
        for scanner, allowed in EXPECTED[name].items():
            got = by.get(scanner, {}).get("result")
            if got not in allowed:
                failures.append(f"{name}: {scanner} gave {got}, expected one of {sorted(allowed)}")
    if failures:
        print("Self-test FAILED:")
        for line in failures:
            print(f"  - {line}")
        return 1
    print("Self-test passed: every scanner ran and gave the expected verdict.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
