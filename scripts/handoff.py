"""The files the scan-queue steps hand to each other, and the checks the post step builds from them.

scan-queue.yml is ONE job. Its steps share files in $RUNNER_TEMP, never workflow
artifacts: this repo is public, so anyone can download its artifacts, and they
would carry skills that are not published and the AI review's private findings.

  claimed.json   claim step -> post step. Every claimed item with its id and digest.
  items.json     claim step -> scanner user and AI review step. Position, name and
                 markdown only: no ids, no digests.
  scanners.json  scanner user -> post step. UNTRUSTED: the scanner user reads skill
                 text, so code that broke out of a scanner could write anything here.
  ai.json        AI review step -> post step.

Every file is written 0600 and replaced whole (write, then rename), so a step that
is stopped part way leaves the last complete file. A result file holds
{"results": [{"position": <int>, "checks": [<check>, ...]}, ...]}.

The post step reads both result files strictly (assemble). A missing or
malformed file, a position listed twice, a check with the wrong name or by, or
a malformed field each make that check "Scanner did not finish". Only the AI
review's row may carry a reason (normalize.REASONS), and only on a did-not-finish
row, so nothing in scanners.json can claim the budget signal or stand in for the
AI review.

Standard library only.
"""

from __future__ import annotations

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent))  # works under `python3 -I` too

import normalize  # noqa: E402
from skillmd import is_valid_name  # noqa: E402

CLAIMED, ITEMS, SCANNERS, AI = "claimed.json", "items.json", "scanners.json", "ai.json"
# The largest result file the post step reads. scripts/sandbox.sh copies at most
# this many bytes out of the scanner user's home (MAX_RESULT_BYTES there).
MAX_FILE_BYTES = 8 * 1024 * 1024

CHECK_KEYS = {"name", "by", "result", "summary", "checkedAt", "details"}
RESULTS = ("pass", "warn", "fail")
DAY_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

FINISHED, NOT_FINISHED, BUDGET_STOP = "finished", "did not finish", "did not finish (budget)"


def today_utc() -> str:
    """Today's date in UTC as YYYY-MM-DD (the checkedAt format)."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def write_private(path: Path, data: object) -> None:
    """Write data as JSON to path, readable by its owner only, replacing the file in one step."""
    temp = path.with_name(path.name + ".tmp")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=True)
    os.replace(temp, path)


def read_json(path: Path) -> object | None:
    """The JSON in path, or None when it is missing, larger than MAX_FILE_BYTES or not JSON."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read(MAX_FILE_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_FILE_BYTES:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return None


def item_for_checks(item: dict[str, Any]) -> dict[str, Any]:
    """What the scanners and the AI review get of one claimed item: no id, no digest."""
    return {"position": item["position"], "name": item["name"], "markdown": item["markdown"]}


def read_items(path: Path) -> tuple[list[dict[str, Any]], int]:
    """The valid items in items.json, and how many entries were not valid."""
    payload = read_json(path)
    entries = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise ValueError(f"{path.name} has no items list")
    items, invalid = [], 0
    for entry in entries:
        position = entry.get("position") if isinstance(entry, dict) else None
        if (isinstance(position, int) and not isinstance(position, bool) and is_valid_name(entry.get("name"))
                and isinstance(entry.get("markdown"), str)):
            items.append(item_for_checks(entry))
        else:
            invalid += 1
    return items, invalid


def by_position(payload: object) -> dict[int, object]:
    """A result file's checks by position. A position listed twice is dropped: it has no one answer."""
    results = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(results, list):
        return {}
    found: dict[int, object] = {}
    twice: set[int] = set()
    for entry in results:
        position = entry.get("position") if isinstance(entry, dict) else None
        if not isinstance(position, int) or isinstance(position, bool):
            continue
        if position in found:
            twice.add(position)
        found[position] = entry.get("checks")
    for position in twice:
        del found[position]
    return found


def clean_check(raw: object, pair: tuple[str, str], *, allow_reason: bool = False) -> dict[str, Any] | None:
    """A copy of one posted check rebuilt from its known fields, or None when anything is off."""
    if not isinstance(raw, dict) or not set(raw) <= CHECK_KEYS or (raw.get("name"), raw.get("by")) != pair:
        return None
    result, summary, day = raw.get("result"), raw.get("summary"), raw.get("checkedAt")
    if result not in RESULTS or not isinstance(summary, str) or not isinstance(day, str) or not DAY_RE.fullmatch(day):
        return None
    summary = normalize.clean_text(summary, normalize.SUMMARY_MAX)
    if not summary:
        return None
    check: dict[str, Any] = {"name": pair[0], "by": pair[1], "result": result, "summary": summary, "checkedAt": day}
    details = raw.get("details")
    if isinstance(details, str):
        if details:
            check["details"] = details[: normalize.DETAILS_MAX]
    elif isinstance(details, dict):
        # The one object a check may carry: a reason amdahl.ai acts on, on a did-not-finish row.
        if not allow_reason or set(details) != {"reason", "text"} or details["reason"] not in normalize.REASONS:
            return None
        if (result, summary) != ("warn", normalize.DID_NOT_FINISH) or not isinstance(details["text"], str):
            return None
        check["details"] = {"reason": details["reason"],
                            "text": normalize.clean_text(details["text"], normalize.DETAILS_MAX - 100)}
    elif details is not None:
        return None
    return check


def pick(checks: object, pair: tuple[str, str], *, allow_reason: bool = False) -> dict[str, Any] | None:
    """The one check for pair in a list, cleaned; None when it is absent, listed twice or malformed."""
    if not isinstance(checks, list):
        return None
    found = [check for check in checks if isinstance(check, dict) and (check.get("name"), check.get("by")) == pair]
    return clean_check(found[0], pair, allow_reason=allow_reason) if len(found) == 1 else None


def assemble(position: int, scanners: dict[int, object], ai: dict[int, object], checked_at: str) -> list[dict[str, Any]]:
    """The three checks to post for one item: Cisco and Betterleaks from scanners.json, the AI review from ai.json."""
    checks = []
    for pair in (normalize.CISCO, normalize.BETTERLEAKS):
        checks.append(pick(scanners.get(position), pair) or normalize.did_not_finish(
            pair, checked_at, "The scanner step gave no usable result for this skill"))
    checks.append(pick(ai.get(position), normalize.AI_REVIEW, allow_reason=True) or normalize.did_not_finish(
        normalize.AI_REVIEW, checked_at, "The AI review step gave no usable result for this skill"))
    return checks


def ai_state(check: dict[str, Any]) -> str:
    """How the AI review ended, for the public log: never its verdict or a finding."""
    if normalize.reason_of(check) == normalize.BUDGET:
        return BUDGET_STOP
    return NOT_FINISHED if check.get("summary") == normalize.DID_NOT_FINISH else FINISHED
