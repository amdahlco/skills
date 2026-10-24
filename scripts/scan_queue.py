"""Claim skills waiting for a scan on amdahl.ai, check them, post the results back.

Usage, one subcommand per step of .github/workflows/scan-queue.yml, in this order:
    python3 scripts/scan_queue.py claim --out DIR                    # SKILLS_SCAN_TOKEN
    python3 scripts/scan_queue.py scan --items FILE --out FILE       # the scanner user, no secrets
    python3 scripts/scan_queue.py ai-review --items FILE --out FILE  # ANTHROPIC_API_KEY only
    python3 scripts/scan_queue.py post --dir DIR                     # SKILLS_SCAN_TOKEN

`scan` runs as the unprivileged scanner user (scripts/sandbox.sh scan). The
steps pass files to each other in one private folder, never as workflow
artifacts; scripts/handoff.py names the files and reads them back strictly.

`ai-review` runs at most AI_WORKERS reviews at a time, and stops waiting
AI_RUN_SECONDS after it starts, inside the step's timeout-minutes. An item with
no finished review by then gets the "did not finish" row, never a pass.

Environment:
    AMDAHL_SITE        base URL, default https://amdahl.ai (https only; http
                       is accepted for localhost so the flow can be tested)
    SKILLS_SCAN_TOKEN  bearer token that may only read the queue and post
                       results. For claim and post; scan and ai-review refuse
                       to run while it is set.
    ANTHROPIC_API_KEY  the AI review's key. For ai-review only; claim, scan and
                       post refuse to run while it is set.

This repo is public, so its Actions logs are public. Nothing here prints the
token, the key, a skill's text, its name, its id or any finding. Logs carry the
item kind, its place in the queue, the two scanners' results and whether the AI
review finished, never the AI review's verdict. Ids stay out of the logs too: a
submission made before status links had their own key still answers its status
by id on amdahl.ai.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import queue
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # works under `python3 -I` too

import handoff  # noqa: E402
import normalize  # noqa: E402
from skillmd import DIGEST_RE, UUID_RE, is_valid_name, sha256_hex  # noqa: E402

DEFAULT_SITE = "https://amdahl.ai"
QUEUE_PATH = "/api/skills/scan/queue"
RESULTS_PATH = "/api/skills/scan/results"
MAX_ITEMS = 20
MAX_RESPONSE_BYTES = 32 * 1024 * 1024
HTTP_TIMEOUT_SECONDS = 30
SCAN_TIMEOUT_SECONDS = 180  # per scanner run, in the queue
AI_WORKERS = 4  # at most this many AI reviews at a time
# The AI review step stops waiting this long after it starts. The "AI security review" step has
# timeout-minutes 15 in scan-queue.yml and self-test.yml, so 2 minutes are left to write ai.json.
AI_RUN_SECONDS = 13 * 60
USER_AGENT = "amdahl-skills-scanner (+https://github.com/amdahlco/skills)"

TOKEN, MODEL_KEY = "SKILLS_SCAN_TOKEN", "ANTHROPIC_API_KEY"
# The secrets each step must NOT hold. Each step refuses to start with one of them set.
FORBIDDEN = {
    "claim": (MODEL_KEY,),
    "scan": (TOKEN, MODEL_KEY),
    "ai-review": (TOKEN,),
    "post": (MODEL_KEY,),
}


class QueueError(RuntimeError):
    """The site could not be reached, a step was started wrong, or a file it needs is missing."""


def refuse_secrets(step: str) -> None:
    """Stop when this step's environment holds a secret it must never have."""
    for name in FORBIDDEN[step]:
        if os.environ.get(name, "").strip():
            raise QueueError(f"{name} is set, and the {step} step must never hold it")


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
    token = os.environ.get(TOKEN, "").strip()
    if not token:
        raise QueueError(f"{TOKEN} is not set")
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

    Each valid item also carries `position`, its place in the site's queue: the
    later steps name an item by it, never by its id.
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


def post_result(base: str, token: str, item: dict[str, str], checks: list[dict]) -> str:
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


def _append(env_var: str, text: str) -> None:
    target = os.environ.get(env_var)
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(text)


def _items(path: Path) -> list[dict]:
    try:
        items, invalid = handoff.read_items(path)
    except ValueError as error:
        raise QueueError(str(error)) from None
    if invalid:
        print(f"Left out {invalid} entry(ies) of {path.name} that are not valid")
    return items


def cmd_claim(args: argparse.Namespace) -> int:
    """Step 1: claim the queue. Writes claimed.json (with ids) and items.json (without)."""
    refuse_secrets("claim")
    base = site_base(os.environ.get("AMDAHL_SITE"))
    items, invalid = fetch(base, _token())
    args.out.mkdir(mode=0o700, parents=True, exist_ok=True)
    handoff.write_private(args.out / handoff.CLAIMED, {"items": items, "invalid": invalid})
    handoff.write_private(args.out / handoff.ITEMS, {"items": [handoff.item_for_checks(item) for item in items]})
    print(f"Queue: {len(items)} item(s) to scan, {invalid} skipped")
    _append("GITHUB_OUTPUT", f"count={len(items)}\ninvalid={invalid}\n")
    # Skipped items fail the post step (it reads `invalid`), so valid ones still get scanned.
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    """Step 2: the two rules-only scanners on every item, as the scanner user with no secrets."""
    refuse_secrets("scan")
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        raise QueueError("the scan step must not run as root")
    import scan  # imported here so claim and post work before the scanners are installed

    results: list[dict] = []
    handoff.write_private(args.out, {"results": results})
    crashed = 0
    for item in _items(args.items):
        try:
            checks = scan.scan_markdown_scanners(item["name"], item["markdown"], timeout=args.timeout)
        except Exception:  # noqa: BLE001 - one bad item must not stop the rest; post marks it did not finish
            crashed += 1
            continue
        results.append({"position": item["position"], "checks": checks})
        handoff.write_private(args.out, {"results": results})
    print(f"Scanners: {len(results)} item(s) scanned, {crashed} could not be scanned")
    return 0


def not_sent_budget(day: str) -> dict:
    """The row of an item not sent because Anthropic refused an earlier review for money."""
    import ai_review

    return ai_review.budget_check(
        day, "Not sent: Anthropic refused an earlier review in this run for the spend limit, "
             "the credit balance or billing")


def out_of_time(day: str, sent: bool) -> dict:
    """The row of an item with no finished review when the run's time for AI reviews ran out."""
    minutes = AI_RUN_SECONDS // 60
    text = (f"No answer before this run's {minutes} minutes for AI reviews ran out" if sent else
            f"Not sent: too little was left of this run's {minutes} minutes for AI reviews")
    return normalize.did_not_finish(normalize.AI_REVIEW, day, text + ", so this skill was not reviewed")


def review_all(items: list[dict], out: Path, keyed: bool, deadline: float) -> tuple[list[dict], int]:
    """The AI review check of every item, in order, and how many ran out of time.

    AI_WORKERS threads take items in turn; ai.json is rewritten after each
    review. After a budget refusal no further item is sent (reviews already
    sent finish as they will). At the deadline this stops waiting: an item
    with no finished review gets the out_of_time row, and a review that ends
    later is dropped. The threads are daemons, so none holds the step open.
    """
    import ai_review
    import scan

    checks: list[dict | None] = [None] * len(items)
    started = [False] * len(items)
    todo: queue.SimpleQueue = queue.SimpleQueue()
    for index in range(len(items)):
        todo.put(index)
    lock, all_done = threading.Lock(), threading.Event()
    state = {"budget": False, "closed": False, "late": 0}

    def save() -> None:  # with lock held
        results = [{"position": item["position"], "checks": [check]}
                   for item, check in zip(items, checks) if check is not None]
        handoff.write_private(out, {"keyed": keyed, "results": results})

    def finish(index: int, check: dict, late: bool = False) -> None:
        with lock:
            if state["closed"]:
                return
            checks[index] = check
            state["budget"] = state["budget"] or handoff.ai_state(check) == handoff.BUDGET_STOP
            state["late"] += late
            save()
            if all(c is not None for c in checks):
                all_done.set()

    def work() -> None:
        while True:
            try:
                index = todo.get_nowait()
            except queue.Empty:
                return
            day = handoff.today_utc()
            with lock:
                budget = state["budget"]
            if budget:
                finish(index, not_sent_budget(day))
            elif ai_review.seconds_left(deadline) < ai_review.START_MIN_SECONDS:
                finish(index, out_of_time(day, sent=False), late=True)
            else:
                started[index] = True
                finish(index, scan.review_markdown(items[index]["markdown"], day, deadline))

    with lock:
        save()
    if not items:
        all_done.set()
    for number in range(min(AI_WORKERS, len(items))):
        threading.Thread(target=work, name=f"ai-review-{number + 1}", daemon=True).start()
    all_done.wait(max(0.0, ai_review.seconds_left(deadline)))
    with lock:
        state["closed"] = True
        for index, check in enumerate(checks):
            if check is None:
                checks[index] = out_of_time(handoff.today_utc(), sent=started[index])
                state["late"] += 1
        save()
        return [check for check in checks if check is not None], state["late"]


def cmd_ai_review(args: argparse.Namespace) -> int:
    """Step 3: the AI security review of every item. Holds the model key, never the token.

    At most AI_WORKERS reviews run at a time, and the step stops waiting
    AI_RUN_SECONDS after it starts (review_all). After a budget refusal no
    further item is sent: each one gets the same did-not-finish row with
    details.reason 'budget', and amdahl.ai queues them all again.
    """
    refuse_secrets("ai-review")
    deadline = time.monotonic() + AI_RUN_SECONDS
    keyed = bool(os.environ.get(MODEL_KEY, "").strip())
    checks, late = review_all(_items(args.items), args.out, keyed, deadline)
    tally = {handoff.FINISHED: 0, handoff.NOT_FINISHED: 0, handoff.BUDGET_STOP: 0}
    for check in checks:
        tally[handoff.ai_state(check)] += 1
    if not keyed:
        print("AI review: ANTHROPIC_API_KEY is not set, so no item was reviewed")
    print("AI review: " + ", ".join(f"{key} {value}" for key, value in tally.items()))
    if tally[handoff.BUDGET_STOP]:
        print("AI review: stopped sending reviews. Anthropic refused for the spend limit, the credit balance or billing.")
    if late:
        print(f"AI review: {late} item(s) had no finished review when the run's {AI_RUN_SECONDS // 60} minutes ran out")
    return 0


def cmd_post(args: argparse.Namespace) -> int:
    """Step 4: build each item's three checks from the result files and post them."""
    refuse_secrets("post")
    base = site_base(os.environ.get("AMDAHL_SITE"))
    token = _token()
    claimed = handoff.read_json(args.dir / handoff.CLAIMED)
    if not isinstance(claimed, dict) or not isinstance(claimed.get("items"), list):
        raise QueueError(f"{handoff.CLAIMED} is missing or not valid")
    scanners = handoff.by_position(handoff.read_json(args.dir / handoff.SCANNERS))
    ai = handoff.by_position(handoff.read_json(args.dir / handoff.AI))
    invalid = claimed.get("invalid", 0)
    tally = {"posted": 0, "stale": 0, "unknown": 0, "error": invalid if isinstance(invalid, int) else 1}
    for index, raw_item in enumerate(claimed["items"]):
        item = validate_item(raw_item)
        if item is None or sha256_hex(item["markdown"]) != item["digest"]:
            tally["error"] += 1
            continue
        position = raw_item.get("position")
        if not isinstance(position, int) or isinstance(position, bool):
            position = index
        checks = handoff.assemble(position, scanners, ai, handoff.today_utc())
        outcome = post_result(base, token, item, checks)
        tally[outcome] += 1
        print(f"Queue item {position} ({item['kind']}): {checks[0]['result']}/{checks[1]['result']}, "
              f"AI review {handoff.ai_state(checks[2])} -> {outcome}")
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
    claim = sub.add_parser("claim", help="claim the queue; write claimed.json and items.json to a private folder")
    claim.add_argument("--out", type=Path, required=True)
    scan = sub.add_parser("scan", help="run the two scanners on items.json (as the scanner user)")
    scan.add_argument("--items", type=Path, required=True)
    scan.add_argument("--out", type=Path, required=True)
    scan.add_argument("--timeout", type=int, default=SCAN_TIMEOUT_SECONDS, help="seconds per scanner run")
    review = sub.add_parser("ai-review", help="run the AI security review on items.json")
    review.add_argument("--items", type=Path, required=True)
    review.add_argument("--out", type=Path, required=True)
    post = sub.add_parser("post", help="post the three checks for every claimed item")
    post.add_argument("--dir", type=Path, required=True)
    args = parser.parse_args(argv)
    commands = {"claim": cmd_claim, "scan": cmd_scan, "ai-review": cmd_ai_review, "post": cmd_post}
    try:
        return commands[args.command](args)
    except QueueError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
