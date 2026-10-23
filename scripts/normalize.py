"""Turn raw scanner output into the site's normalized check shape.

A check is:
    {"name", "by", "result": "pass" | "warn" | "fail", "summary" (one line,
     <= 300 chars), "checkedAt": "YYYY-MM-DD", "details" (<= 4000 chars)}

`details` is text, with one exception: a "did not finish" row that carries a
reason amdahl.ai acts on has details {"reason": <one of REASONS>, "text": ...}.
Today the only reason is BUDGET, and only the AI review's row carries it.

Severity map (Cisco skill-scanner): none, info, low -> pass; medium -> warn;
high, critical -> fail; anything unknown -> warn. Betterleaks gives a
confidence instead (see normalize_betterleaks). The AI review gives a verdict:
pass -> pass, review -> warn, block -> fail. A scanner or a review that errors
or returns output we cannot read becomes `warn` with the summary
"Scanner did not finish". A Cisco report that does not show it covered the
whole skill is at least `warn` (fail closed): a missing field, a failed
analyzer or a shape we do not expect counts as a gap. Nothing is ever passed
silently.

Standard library only.
"""

from __future__ import annotations

import os
import unicodedata
from typing import Any

CISCO = ("Threat analysis", "Cisco AI Defense skill-scanner")
BETTERLEAKS = ("Secrets", "Betterleaks")
AI_REVIEW = ("AI security review", "Amdahl AI review")

# Must equal SCANNER_DID_NOT_FINISH in amdahl.ai's lib/skills/scanners.ts.
DID_NOT_FINISH = "Scanner did not finish"
# Anthropic refused the AI review for money: a spend limit, the monthly spend
# cap, the credit balance or billing (ai_review.is_budget_refusal). amdahl.ai
# queues the item again without counting one of its attempts, and tells the
# team once per incident.
BUDGET = "budget"
REASONS = (BUDGET,)
SUMMARY_MAX = 300
DETAILS_MAX = 4000

SEVERITY_RANK = {"NONE": 0, "INFO": 1, "LOW": 2, "MEDIUM": 3, "HIGH": 4, "CRITICAL": 5}
# Cisco skill-scanner reports a skill with no findings as max_severity "SAFE" (is_safe true).
# It reads as NONE: without it, every clean skill graded as an unknown severity, a warning.
CISCO_SEVERITY_ALIASES = {"SAFE": "NONE"}
RESULT_RANK = {"pass": 0, "warn": 1, "fail": 2}


class ScannerOutputError(ValueError):
    """The scanner ran but its output does not have the shape we expect."""


def result_for_severity(severity: object) -> str:
    """Map one scanner severity to pass / warn / fail."""
    rank = SEVERITY_RANK.get(str(severity).strip().upper())
    if rank is None:
        return "warn"
    if rank <= SEVERITY_RANK["LOW"]:
        return "pass"
    if rank == SEVERITY_RANK["MEDIUM"]:
        return "warn"
    return "fail"


def worst_result(results: list[str]) -> str:
    """The strictest of a list of results ('pass' for an empty list)."""
    return max(results, key=lambda r: RESULT_RANK[r], default="pass")


def clean_text(value: object, limit: int) -> str:
    """One line of text, invisible and control characters shown as \\uXXXX, clipped to limit."""
    out = []
    for char in str(value):
        if char in "\n\r\t":
            out.append(" ")
            continue
        category = unicodedata.category(char)
        if category in ("Cc", "Cf", "Co", "Cs", "Cn", "Zl", "Zp") or (category == "Zs" and char != " "):
            out.append(f"\\u{ord(char):04x}")
        else:
            out.append(char)
    text = " ".join("".join(out).split())
    return text if len(text) <= limit else text[: max(0, limit - 3)] + "..."


def _clip_block(lines: list[str], limit: int) -> str:
    text = "\n".join(lines)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _check(pair: tuple[str, str], result: str, summary: str, checked_at: str, details: str = "") -> dict[str, str]:
    check = {
        "name": pair[0],
        "by": pair[1],
        "result": result,
        "summary": clean_text(summary, SUMMARY_MAX),
        "checkedAt": checked_at,
    }
    if details:
        check["details"] = details[:DETAILS_MAX]
    return check


def did_not_finish(pair: tuple[str, str], checked_at: str, reason: str, *, code: str | None = None) -> dict[str, Any]:
    """The check we report when a scanner errors, times out or prints output we cannot read.

    With `code` (one of REASONS), details is {"reason": code, "text": reason}:
    the one shape amdahl.ai reads a reason from. Without it, details is the text.
    """
    if code is None:
        return _check(pair, "warn", DID_NOT_FINISH, checked_at, clean_text(reason, DETAILS_MAX))
    if code not in REASONS:
        raise ValueError(f"unknown reason code {code!r}")
    check: dict[str, Any] = _check(pair, "warn", DID_NOT_FINISH, checked_at)
    check["details"] = {"reason": code, "text": clean_text(reason, DETAILS_MAX - 100)}
    return check


def reason_of(check: dict[str, Any]) -> str | None:
    """The reason code in a check's details, or None when it carries none."""
    details = check.get("details")
    reason = details.get("reason") if isinstance(details, dict) else None
    return reason if reason in REASONS else None


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _summary(findings: list[tuple[int, str]], top: str, where: str = "") -> str:
    """'N findings, highest TOP: first title[, where]; other titles'. where locates the first one."""
    titles = []
    for _, title in findings:
        if title not in titles:
            titles.append(title)
    if where:
        titles[0] = f"{titles[0]}, {where}"
    return f"{_plural(len(findings), 'finding')}, highest {top}: " + "; ".join(titles)


def _cisco_gaps(report: dict) -> list[str]:
    """Why a Cisco skill-scanner report does not show a whole scan."""
    gaps = []
    used = report.get("analyzers_used")
    if not isinstance(used, list) or not used:
        gaps.append("no analyzers listed")
    # Real 2.3.1 output for a skill it could not load: analyzers_failed
    # [{"analyzer": "skill_loader", "error": "SkillLoadError:..."}], is_safe true, max INFO.
    for key, value in report.items():
        if value and ("error" in str(key).lower() or "failed" in str(key).lower()):
            gaps.append(f"{clean_text(key, 40)}: {clean_text(value, 200)}")
    if not isinstance(report.get("is_safe"), bool):
        gaps.append("is_safe is not true or false")
    if "max_severity" not in report:
        gaps.append("no max_severity")
    if report.get("findings_count") != len(report["findings"]):
        gaps.append("findings_count does not match the findings")
    return gaps


def line_number(value: object) -> int | None:
    """A scanner's line number as a positive int, or None. amdahl.ai reads at most 6 digits."""
    if isinstance(value, bool):
        return None
    if isinstance(value, str) and value.strip().isascii() and value.strip().isdigit():
        value = int(value.strip())
    return value if isinstance(value, int) and 0 < value < 1_000_000 else None


def _cisco_severity(severity: object) -> str:
    upper = str(severity).strip().upper()
    return CISCO_SEVERITY_ALIASES.get(upper, upper)


def normalize_cisco(report: Any, checked_at: str) -> dict[str, str]:
    """Cisco AI Defense skill-scanner `scan --format json` report -> one check.

    The summary leads with the most severe finding and, when the scanner gives
    one, its file and line ("..., highest HIGH: <title>, SKILL.md line 12; ..."):
    amdahl.ai shows the submitter the first "line N" of a summary, and it never
    stores details. Among findings of the same severity, one with a line comes
    first. The summary never holds the matched text.
    """
    try:
        if not isinstance(report, dict) or not isinstance(report.get("findings"), list):
            raise ScannerOutputError("report has no 'findings' list")
        rows: list[tuple[int, str, str, str]] = []
        severities = [_cisco_severity(report.get("max_severity", ""))]
        for finding in report["findings"]:
            if not isinstance(finding, dict):
                raise ScannerOutputError("a finding is not an object")
            severity = _cisco_severity(finding.get("severity", ""))
            severities.append(severity)
            rule = clean_text(finding.get("rule_id", "?"), 60)
            title = clean_text(finding.get("title") or rule, 80)
            where = clean_text(finding.get("file_path") or "", 80)
            if finding.get("line_number"):
                where = f"{where} line {finding['line_number']}"
            metadata = finding.get("metadata") if isinstance(finding.get("metadata"), dict) else {}
            match = clean_text(metadata.get("matched_text") or finding.get("snippet") or "", 120)
            text = f"- {severity} {rule}: {title}"
            if where:
                text += f", {where}"
            if match:
                text += f': "{match}"'
            line = line_number(finding.get("line_number"))
            # The file's own name only: a scanner may report the temporary folder it read.
            name = clean_text(os.path.basename(str(finding.get("file_path") or "")), 80)
            located = f"{name} line {line}".strip() if line else ""
            rows.append((SEVERITY_RANK.get(severity, 3), title, text, located))
    except ScannerOutputError as error:
        return did_not_finish(CISCO, checked_at, f"Unexpected skill-scanner output: {error}")

    rows.sort(key=lambda row: (-row[0], not row[3]))
    worst = max(severities, key=lambda s: SEVERITY_RANK.get(s, 3))
    result = worst_result([result_for_severity(s) for s in severities])
    gaps = _cisco_gaps(report)
    # is_safe false is the scanner's own verdict, not a gap: never a pass, never "partial".
    if gaps or report.get("is_safe") is False:
        result = worst_result([result, "warn"])
    summary = _summary([(r[0], r[1]) for r in rows], worst, rows[0][3]) if rows else "No findings"
    if gaps:
        summary = "Partial coverage. " + summary
    head = "Static analyzers only (no LLM judge, no cloud services)."
    used = report.get("analyzers_used")
    if isinstance(used, list) and used:
        head += " Analyzers: " + clean_text(", ".join(str(u) for u in used), 200) + "."
    if gaps:
        head += " Coverage gaps: " + clean_text("; ".join(gaps), 1000) + "."
    details = _clip_block([head] + [r[2] for r in rows], DETAILS_MAX)
    return _check(CISCO, result, summary, checked_at, details)


CONFIDENCE_TO_RESULT = {"high": "fail", "medium": "warn", "low": "warn"}


def normalize_betterleaks(findings: Any, checked_at: str) -> dict[str, str]:
    """Betterleaks `--report-format json` output (a list) -> one check.

    Betterleaks has no severity field, only a confidence (low / medium /
    high). A possible secret is never a pass: high confidence -> fail,
    anything else -> warn. Secret values are never copied into the check,
    even redacted ones.
    """
    try:
        if not isinstance(findings, list):
            raise ScannerOutputError("report is not a JSON list")
        rows: list[tuple[str, str]] = []
        for finding in findings:
            if not isinstance(finding, dict):
                raise ScannerOutputError("a finding is not an object")
            attributes = finding.get("Attributes") if isinstance(finding.get("Attributes"), dict) else {}
            confidence = str(attributes.get("confidence", "")).lower()
            rule = clean_text(finding.get("RuleID", "?"), 60)
            where = clean_text(os.path.basename(str(finding.get("File", ""))), 80)
            line = finding.get("StartLine")
            if line:
                where = f"{where} line {line}"
            label = confidence or "unknown"
            rows.append((CONFIDENCE_TO_RESULT.get(confidence, "warn"), f"- {rule}, {where}, confidence {label}"))
    except ScannerOutputError as error:
        return did_not_finish(BETTERLEAKS, checked_at, f"Unexpected Betterleaks output: {error}")

    if not rows:
        return _check(BETTERLEAKS, "pass", "No secrets found", checked_at)
    result = worst_result([row[0] for row in rows])
    first = rows[0][1][2:]
    summary = f"{_plural(len(rows), 'possible secret')}: {first}"
    details = _clip_block(["Values are redacted and never stored."] + [row[1] for row in rows], DETAILS_MAX)
    return _check(BETTERLEAKS, result, summary, checked_at, details)


# The AI review (scripts/ai_review.py). Its verdict maps to a result, like a severity.
VERDICT_RANK = {"pass": 0, "review": 1, "block": 2}
VERDICT_TO_RESULT = {"pass": "pass", "review": "warn", "block": "fail"}
AI_CHECK_WORDS = {
    "data_leaving": "Data leaving",
    "hidden_actions": "Hidden actions",
    "overrides": "Overrides",
    "persistence": "Persistence",
    "remote_code": "Remote code",
    "destructive": "Destructive actions",
    "credentials": "Credentials",
    "purpose_mismatch": "Purpose mismatch",
}
# At most this many characters of the skill's own text (the findings' quotes) go into details.
QUOTE_BUDGET = 120
QUOTE_MAX = 60


def effective_verdict(review: dict[str, Any]) -> str:
    """The stricter of the review's verdict and its worst finding.

    A 'pass' that lists a finding is not a pass: the finding wins.
    """
    severities = [finding["severity"] for finding in review["findings"]]
    return max([review["verdict"], *severities], key=lambda v: VERDICT_RANK[v])


def normalize_ai_review(review: dict[str, Any], model: str, checked_at: str, note: str = "") -> dict[str, str]:
    """A validated AI review (ai_review.parse_review) -> one check.

    The summary names the most severe finding by its check, line and why; it
    never quotes the skill. Details list every finding and say which model gave
    the final verdict. Quotes of the skill text share a budget of QUOTE_BUDGET
    characters across all of details.
    """
    verdict = effective_verdict(review)
    result = VERDICT_TO_RESULT[verdict]
    ranked = sorted(review["findings"], key=lambda f: -VERDICT_RANK[f["severity"]])  # stable: ties keep order
    if result == "pass":
        summary = "No findings"
    elif not ranked:
        summary = "Flagged, but the review named no finding"
    else:
        top = ranked[0]
        label = AI_CHECK_WORDS[top["check"]]
        where = f"{label} on line {top['line']}" if top["line"] else label
        summary = f"{where}: {clean_text(top['why'], SUMMARY_MAX)}"

    head = f"Final verdict: {verdict}, from {model}."
    if note:
        head += " " + clean_text(note, 400)
    head += " The model saw only the SKILL.md text, with no tools."
    lines = [head]
    budget = QUOTE_BUDGET
    for finding in ranked:
        where = f", line {finding['line']}" if finding["line"] else ""
        text = f"- {finding['severity'].upper()} {finding['check']}{where}: {clean_text(finding['why'], 300)}"
        if budget >= 10 and finding["quote"].strip():
            quote = clean_text(finding["quote"], min(QUOTE_MAX, budget))
            budget -= len(quote)
            text += f' Quote: "{quote}"'
        lines.append(text)
    return _check(AI_REVIEW, result, summary, checked_at, _clip_block(lines, DETAILS_MAX))
