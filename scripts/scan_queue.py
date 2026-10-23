"""Pull skills waiting for a scan from amdahl.ai, scan them, post the results back.

Usage:
    python3 scripts/scan_queue.py fetch --out queue.json
    python3 scripts/scan_queue.py run --queue queue.json

Environment:
    AMDAHL_SITE        base URL, default https://amdahl.ai (https only; http
                       is accepted for localhost so the flow can be tested)
    SKILLS_SCAN_TOKEN  bearer token that may only read the queue and post results

This repo is public, so its Actions logs are public. Nothing here prints the
token, a skill's text, its name, its id or any finding. Logs carry the item
kind, its place in the queue and the three results only. The id stays out
because a submission's id is the only key to its status on amdahl.ai.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # works under `python3 -I` too

from skillmd import DIGEST_RE, UUID_RE, is_valid_name, sha256_hex  # noqa: E402

DEFAULT_SITE = "https://amdahl.ai"
QUEUE_PATH = "/api/skills/scan/queue"
RESULTS_PATH = "/api/skills/scan/results"
MAX_ITEMS = 20
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
HTTP_TIMEOUT_SECONDS = 30
USER_AGENT = "amdahl-skills-scanner (+https://github.com/amdahlco/skills)"


class QueueError(RuntimeError):
    """The site could not be reached or answered with something we cannot use."""


def site_base(raw: str | None) -> str:
    """Validate AMDAHL_SITE and return it without a trailing slash."""
    value = (raw or DEFAULT_SITE).strip().rstrip("/")
    parts = urllib.parse.urlsplit(value)
    local = parts.hostname in ("localhost", "127.0.0.1")
    if parts.scheme != "https" and not (parts.scheme == "http" and local):
        raise QueueError("AMDAHL_SITE must be an https:// URL")
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise QueueError("AMDAHL_SITE must be a plain origin such as https://amdahl.ai")
    if parts.path not in ("", "/"):
        raise QueueError("AMDAHL_SITE must not include a path")
    return value


def _token() -> str:
    token = os.environ.get("SKILLS_SCAN_TOKEN", "").strip()
    if not token:
        raise QueueError("SKILLS_SCAN_TOKEN is not set")
    return token


def _request(method: str, url: str, token: str, body: dict | None = None) -> tuple[int, bytes]:
    data = json.dumps(body, ensure_ascii=True).encode("utf-8") if body is not None else None
    request = urllib.request.Request(url, data=data, method=method)
    request.add_header("Authorization", f"Bearer {token}")
    request.add_header("Accept", "application/json")
    request.add_header("User-Agent", USER_AGENT)
    if data is not None:
        request.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return response.status, response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        return error.code, b""
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        reason = getattr(error, "reason", error)
        raise QueueError(f"{method} {urllib.parse.urlsplit(url).path} failed: {reason}") from None


def validate_item(item: object) -> dict[str, str] | None:
    """Return the fields we use from one queue item, or None when it is not valid.

    `publisher` may be present; it is ignored here.
    """
    if not isinstance(item, dict):
        return None
    kind, item_id, name = item.get("kind"), item.get("id"), item.get("name")
    digest, markdown = item.get("digest"), item.get("markdown")
    if kind not in ("submission", "skill"):
        return None
    if not isinstance(item_id, str) or not UUID_RE.fullmatch(item_id):
        return None
    if not is_valid_name(name):
        return None
    if not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest.lower()):
        return None
    if not isinstance(markdown, str):
        return None
    return {"kind": kind, "id": item_id, "name": name, "digest": digest.lower(), "markdown": markdown}


def fetch(base: str, token: str) -> tuple[list[dict[str, object]], int]:
    """GET the queue. Returns (valid items, number of invalid items skipped).

    Each valid item also carries `position`, its place in the site's queue, so
    the `run` step can name it in the log without its id.
    """
    status, raw = _request("GET", base + QUEUE_PATH, token)
    if status != 200:
        raise QueueError(f"GET {QUEUE_PATH} answered HTTP {status}")
    if len(raw) > MAX_RESPONSE_BYTES:
        raise QueueError(f"GET {QUEUE_PATH} answered more than {MAX_RESPONSE_BYTES} bytes")
    try:
        payload = json.loads(raw)
    except ValueError:
        raise QueueError(f"GET {QUEUE_PATH} did not answer JSON") from None
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise QueueError(f"GET {QUEUE_PATH} answered without an 'items' list")
    items, invalid = [], 0
    for index, raw_item in enumerate(payload["items"][:MAX_ITEMS]):
        item = validate_item(raw_item)
        if item is None:
            print(f"Skipped queue item {index}: it does not match the contract")
            invalid += 1
        elif sha256_hex(item["markdown"]) != item["digest"]:
            print(f"Skipped queue item {index} ({item['kind']}): digest does not match the markdown")
            invalid += 1
        else:
            items.append({**item, "position": index})
    return items, invalid


def post_result(base: str, token: str, item: dict[str, str], checks: list[dict[str, str]]) -> str:
    """POST one result. Returns 'posted', 'stale' (409), 'unknown' (404) or 'error'."""
    body = {"kind": item["kind"], "id": item["id"], "digest": item["digest"], "checks": checks}
    for attempt in (1, 2):
        try:
            status, _ = _request("POST", base + RESULTS_PATH, token, body)
        except QueueError as error:
            status = 0
            print(f"  attempt {attempt}: {error}")
        if status == 200:
            return "posted"
        if status == 409:
            return "stale"
        if status == 404:
            return "unknown"
        if attempt == 1 and (status == 0 or status >= 500 or status == 429):
            time.sleep(5)
            continue
        print(f"  POST {RESULTS_PATH} answered HTTP {status}")
        return "error"
    return "error"


def _write_private(path: Path, text: str) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(text)


def _append(env_var: str, text: str) -> None:
    target = os.environ.get(env_var)
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(text)


def cmd_fetch(args: argparse.Namespace) -> int:
    base = site_base(os.environ.get("AMDAHL_SITE"))
    items, invalid = fetch(base, _token())
    _write_private(args.out, json.dumps({"items": items, "invalid": invalid}))
    print(f"Queue: {len(items)} item(s) to scan, {invalid} skipped")
    _append("GITHUB_OUTPUT", f"count={len(items)}\ninvalid={invalid}\n")
    # Skipped items fail the `run` step (it reads `invalid`), so valid ones still get scanned.
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    import scan  # imported here so `fetch` works before the scanners are installed

    base = site_base(os.environ.get("AMDAHL_SITE"))
    token = _token()
    queue = json.loads(args.queue.read_text(encoding="utf-8"))
    tally = {"posted": 0, "stale": 0, "unknown": 0, "error": int(queue.get("invalid", 0))}
    for index, raw_item in enumerate(queue.get("items", [])):
        item = validate_item(raw_item)
        if item is None or sha256_hex(item["markdown"]) != item["digest"]:
            tally["error"] += 1
            continue
        position = raw_item.get("position")
        if not isinstance(position, int) or isinstance(position, bool):
            position = index
        checks = scan.scan_markdown(item["name"], item["markdown"])
        outcome = post_result(base, token, item, checks)
        tally[outcome] += 1
        results = "/".join(check["result"] for check in checks)
        print(f"Queue item {position} ({item['kind']}): {results} -> {outcome}")
    print("Done: " + ", ".join(f"{key} {value}" for key, value in tally.items()))
    _append(
        "GITHUB_STEP_SUMMARY",
        "### Scan queue\n\n| Posted | Stale (409) | Unknown id (404) | Errors |\n|---|---|---|---|\n"
        f"| {tally['posted']} | {tally['stale']} | {tally['unknown']} | {tally['error']} |\n",
    )
    return 1 if tally["error"] else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    fetch_parser = sub.add_parser("fetch", help="download the queue to a private file")
    fetch_parser.add_argument("--out", type=Path, required=True)
    run_parser = sub.add_parser("run", help="scan every queued item and post the results")
    run_parser.add_argument("--queue", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        return cmd_fetch(args) if args.command == "fetch" else cmd_run(args)
    except QueueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
