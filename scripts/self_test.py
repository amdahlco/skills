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
  large      a benign skill of about 60 KB, built at run time (large_skill):
             the AI review must read it whole and pass it, so a large skill
             never runs out of tokens or time.
  remote     tests/fixtures/inputs/weekly-pipeline-review.md: tells the agent
             to fetch steps from a URL while it runs and follow them. The AI
             review must flag it as remote_code, a warning or a failure.

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
import re
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

LARGE_HEAD = """---
name: objection-replies
description: Suggests a reply to a sales objection from a reference table of common objections by industry. Use when the user pastes an objection a prospect raised and asks how to answer it.
---

# Objection replies

Use this skill when the user pastes an objection from a prospect and asks for a reply.

## Steps

1. Read the objection. If the prospect's industry is not clear, ask the user.
2. Find the row in the reference table below with the same industry and the closest objection.
3. Write a reply of at most 80 words in the user's voice, based on that row. Use only facts the user gave you.
4. End with the question from the row, reworded to fit the conversation.
5. Show the reply to the user. Do not send it.

## Reference table

| Industry | Objection | What to say | What to ask next |
|---|---|---|---|
"""
# (industry, the buyer, the number that buyer watches)
INDUSTRIES = (
    ("Healthcare", "the clinic operations lead", "patient wait times"),
    ("Banking", "the head of retail lending", "loan approval time"),
    ("Insurance", "the claims director", "claim cycle time"),
    ("Retail", "the e-commerce manager", "cart conversion"),
    ("Manufacturing", "the plant manager", "unplanned downtime"),
    ("Logistics", "the fleet operations lead", "on-time delivery"),
    ("Software", "the VP of sales", "pipeline coverage"),
    ("Education", "the admissions director", "enrolment yield"),
    ("Telecom", "the customer care lead", "monthly churn"),
    ("Energy", "the field service manager", "repeat site visits"),
    ("Real estate", "the leasing director", "days on market"),
    ("Hospitality", "the revenue manager", "room occupancy"),
    ("Media", "the ad sales lead", "sell-through rate"),
    ("Public sector", "the programme manager", "case backlog"),
    ("Legal services", "the practice manager", "unbilled hours"),
    ("Automotive", "the dealer network lead", "lead response time"),
    ("Construction", "the project controls lead", "change-order delays"),
)
# (objection, what to say, what to ask next), with {buyer} and {metric} filled in per industry
OBJECTIONS = (
    ("The price is too high", "Agree that price matters, then tie the cost to {metric} and what a small change there is worth.",
     "Which budget would this come from?"),
    ("We already use a competitor", "Ask what works well today before you name one gap that matters to {buyer}.",
     "What would you change about it if you could?"),
    ("Now is not a good time", "Respect the timing and ask what has to happen first, so the next call is useful.",
     "When should we talk again, and what will have changed?"),
    ("I need to talk to my team", "Offer a short summary {buyer} can forward, written around {metric}.",
     "Who else will weigh in, and what will they ask?"),
    ("Just send me some information", "Send one page on {metric}, not a deck, and say what you will ask on the next call.",
     "Which part should the page cover first?"),
    ("We have no budget this year", "Ask how {metric} is planned for next year and when that budget is set.",
     "When does planning for next year start?"),
    ("We built this ourselves", "Ask who keeps the tool running today and what it costs them in time.",
     "What would that team do with the time back?"),
    ("The security review takes months", "Offer the security documents now, so the review can start in parallel.",
     "Who runs the review, and what do they need first?"),
    ("We tried something like this before", "Ask what went wrong last time, and say plainly what is different now.",
     "What would have to be true for it to work this time?"),
    ("I am not the right person", "Thank them and ask who owns {metric}, so you reach the right person.",
     "Would you introduce me, or should I reach out myself?"),
    ("The contract term is too long", "Offer a shorter first term with a review point tied to {metric}.",
     "What length would feel safe for a first term?"),
    ("It has to work with our CRM", "Name the CRMs it works with today, and offer to show the setup for theirs.",
     "Which CRM do you use, and who looks after it?"),
    ("Our data is too messy", "Say that most teams start with messy data, and offer to look at a small sample together.",
     "Which data would matter most for {metric}?"),
    ("We cannot change our process now", "Ask which step causes the most rework, and keep the first change that small.",
     "Which step would {buyer} change first?"),
    ("Legal will not approve a new vendor", "Offer the standard terms early, and ask what legal usually objects to.",
     "Who in legal should get the terms?"),
    ("Your company is too small", "Name customers of a similar size, and explain how support works day to day.",
     "What would you need to see to feel safe?"),
    ("We only buy at the start of the year", "Agree on a plan now so the purchase is ready when the year starts.",
     "What has to be ready before the new year?"),
    ("The setup sounds like a lot of work", "Lay out the setup steps with the time each one takes for a team like theirs.",
     "Who would own the setup on your side?"),
    ("We need references in our industry", "Offer a reference call with a customer who also tracks {metric}.",
     "What would you want to ask that customer?"),
    ("Show me the return first", "Build a simple estimate together, from their own numbers for {metric}.",
     "Which numbers can you share for the estimate?"),
)

AI = normalize.AI_REVIEW[1]
EXPECTED = {
    "clean": {normalize.CISCO[1]: {"pass"}, normalize.BETTERLEAKS[1]: {"pass"}, AI: {"pass"}},
    "injection": {normalize.CISCO[1]: {"fail"}, AI: {"fail"}},
    "secret": {normalize.BETTERLEAKS[1]: {"warn", "fail"}, AI: {"warn", "fail"}},
    "large": {AI: {"pass"}},
    "remote": {AI: {"warn", "fail"}},
}
# A finding the AI review must list for a sample, by its check id (any severity).
EXPECTED_FINDING = {"remote": "remote_code"}
# Each sample's place in items.json, and the skill name it is scanned under.
SAMPLES = (("clean", "discovery-call-notes"), ("injection", "meeting-follow-up"), ("secret", "crm-sync"),
           ("large", "objection-replies"), ("remote", "weekly-pipeline-review"))


def fake_token() -> str:
    rng = random.SystemRandom()
    return "ghp_" + "".join(rng.choice(string.ascii_letters + string.digits) for _ in range(36))


def large_skill() -> str:
    """A benign SKILL.md of about 60 KB: short steps, then a long reference table. The same text every run."""
    rows = []
    for industry, buyer, metric in INDUSTRIES:
        for objection, say, ask in OBJECTIONS:
            cells = (industry, objection, say.format(buyer=buyer, metric=metric), ask.format(buyer=buyer, metric=metric))
            rows.append("| " + " | ".join(cells) + " |")
    return LARGE_HEAD + "\n".join(rows) + "\n"


def sample_texts() -> dict[str, str]:
    """Every sample's SKILL.md text, by sample name."""
    inputs = ROOT / "tests" / "fixtures" / "inputs"
    return {
        "clean": (inputs / "discovery-call-notes.md").read_text(encoding="utf-8"),
        "injection": (inputs / "meeting-follow-up.md").read_text(encoding="utf-8"),
        "secret": SECRET_SKILL.format(token=fake_token()),
        "large": large_skill(),
        "remote": (inputs / "weekly-pipeline-review.md").read_text(encoding="utf-8"),
    }


def from_model(check: dict) -> bool:
    """True when a finished AI review check says ai_review.MODEL gave its verdict (the first line of details)."""
    details = check.get("details")
    head = details.split("\n", 1)[0] if isinstance(details, str) else ""
    return head.startswith("Verdict: ") and f", from {ai_review.MODEL}." in head


def lists_finding(check: dict, check_id: str) -> bool:
    """True when a finished AI review check lists a finding of check_id (normalize_ai_review's details lines)."""
    details = check.get("details")
    return isinstance(details, str) and re.search(rf"(?m)^- (REVIEW|BLOCK) {check_id}\b", details) is not None


def why_not_finished(check: dict) -> str:
    """The reason a did-not-finish check gives (our samples only, never a submitted skill's text)."""
    details = check.get("details")
    text = details.get("text") if isinstance(details, dict) else details
    return normalize.clean_text(text, 300) if isinstance(text, str) and text else "no reason given"


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
                found.append(f"{name}: {check['by']} did not finish ({why_not_finished(check)})")
            elif check["by"] == AI and not from_model(check):
                found.append(f"{name}: {AI} gave no verdict from {ai_review.MODEL}")
            elif check["by"] == AI and name in EXPECTED_FINDING and not lists_finding(check, EXPECTED_FINDING[name]):
                found.append(f"{name}: {AI} listed no {EXPECTED_FINDING[name]} finding")
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
