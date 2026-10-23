"""Turn raw scanner output into the site's normalized check shape.

A check is:
    {"name", "by", "result": "pass" | "warn" | "fail", "summary" (one line,
     <= 300 chars), "checkedAt": "YYYY-MM-DD", "details" (<= 4000 chars)}

Severity map (every scanner): none, info, low -> pass; medium -> warn;
high, critical -> fail; anything unknown -> warn. A scanner that errors or
returns output we cannot read becomes `warn` with the summary
"Scanner did not finish". Nothing is ever passed silently.

Standard library only.
"""

from __future__ import annotations

import os
import unicodedata
from typing import Any

SKILLSPECTOR = ("Prompt injection and malicious instructions", "NVIDIA SkillSpector")
CISCO = ("Threat analysis", "Cisco AI Defense skill-scanner")
BETTERLEAKS = ("Secrets", "Betterleaks")

DID_NOT_FINISH = "Scanner did not finish"
SUMMARY_MAX = 300
DETAILS_MAX = 4000

SEVERITY_RANK = {"NONE": 0, "INFO": 1, "LOW": 2, "MEDIUM": 3, "HIGH": 4, "CRITICAL": 5}
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


def did_not_finish(pair: tuple[str, str], checked_at: str, reason: str) -> dict[str, str]:
    """The check we report when a scanner errors, times out or prints output we cannot read."""
    return _check(pair, "warn", DID_NOT_FINISH, checked_at, clean_text(reason, DETAILS_MAX))


def _plural(count: int, word: str) -> str:
    return f"{count} {word}" if count == 1 else f"{count} {word}s"


def _summary(findings: list[tuple[int, str]], top: str) -> str:
    titles = []
    for _, title in findings:
        if title not in titles:
            titles.append(title)
    return f"{_plural(len(findings), 'finding')}, highest {top}: " + "; ".join(titles)


def normalize_skillspector(report: Any, checked_at: str) -> dict[str, str]:
    """NVIDIA SkillSpector `scan --format json` report -> one check."""
    try:
        if not isinstance(report, dict) or not isinstance(report.get("issues"), list):
            raise ScannerOutputError("report has no 'issues' list")
        if report.get("execution_successful") is False:
            return did_not_finish(SKILLSPECTOR, checked_at, "SkillSpector reported execution_successful=false")
        risk = report.get("risk_assessment") or {}
        if not isinstance(risk, dict):
            raise ScannerOutputError("'risk_assessment' is not an object")
        rows: list[tuple[int, str, str]] = []
        for issue in report["issues"]:
            if not isinstance(issue, dict):
                raise ScannerOutputError("an issue is not an object")
            severity = str(issue.get("severity", "")).upper()
            rule = clean_text(issue.get("id", "?"), 20)
            title = clean_text(issue.get("pattern") or issue.get("category") or rule, 80)
            location = issue.get("location") or {}
            where = clean_text(location.get("file", ""), 80) if isinstance(location, dict) else ""
            line = location.get("start_line") if isinstance(location, dict) else None
            if line:
                where = f"{where} line {line}"
            match = clean_text(issue.get("finding", ""), 120)
            text = f"- {severity} {rule} {title}, {where}"
            if match:
                text += f': "{match}"'
            rows.append((SEVERITY_RANK.get(severity, 3), f"{title} ({rule})", text))
    except ScannerOutputError as error:
        return did_not_finish(SKILLSPECTOR, checked_at, f"Unexpected SkillSpector output: {error}")

    rows.sort(key=lambda row: -row[0])
    severities = [str(issue.get("severity", "")).upper() for issue in report["issues"]]
    # The stricter of the worst single issue and the scanner's own score band.
    severities.append(str(risk.get("severity", "NONE")).upper())
    worst = max(severities, key=lambda s: SEVERITY_RANK.get(s, 3))
    result = worst_result([result_for_severity(s) for s in severities])

    completeness = report.get("analysis_completeness") or {}
    partial = isinstance(completeness, dict) and completeness.get("is_complete") is False
    if partial:
        result = worst_result([result, "warn"])

    if rows:
        summary = _summary([(r[0], r[1]) for r in rows], worst)
    else:
        summary = "No findings"
    if partial:
        summary = "Partial coverage. " + summary
    head = (
        f"Risk score {risk.get('score', '?')}/100, {risk.get('severity', '?')}, "
        f"{risk.get('recommendation', '?')}. Rules only, no model."
    )
    details = _clip_block([head] + [r[2] for r in rows], DETAILS_MAX)
    return _check(SKILLSPECTOR, result, summary, checked_at, details)


def normalize_cisco(report: Any, checked_at: str) -> dict[str, str]:
    """Cisco AI Defense skill-scanner `scan --format json` report -> one check."""
    try:
        if not isinstance(report, dict) or not isinstance(report.get("findings"), list):
            raise ScannerOutputError("report has no 'findings' list")
        rows: list[tuple[int, str, str]] = []
        severities = [str(report.get("max_severity", "NONE")).upper()]
        for finding in report["findings"]:
            if not isinstance(finding, dict):
                raise ScannerOutputError("a finding is not an object")
            severity = str(finding.get("severity", "")).upper()
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
            rows.append((SEVERITY_RANK.get(severity, 3), title, text))
    except ScannerOutputError as error:
        return did_not_finish(CISCO, checked_at, f"Unexpected skill-scanner output: {error}")

    rows.sort(key=lambda row: -row[0])
    worst = max(severities, key=lambda s: SEVERITY_RANK.get(s, 3))
    result = worst_result([result_for_severity(s) for s in severities])
    summary = _summary([(r[0], r[1]) for r in rows], worst) if rows else "No findings"
    head = "Static analyzers only (no LLM judge, no cloud services)."
    used = report.get("analyzers_used")
    if isinstance(used, list) and used:
        head += " Analyzers: " + clean_text(", ".join(str(u) for u in used), 200) + "."
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
