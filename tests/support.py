"""Shared helpers for the tests: import path, fixtures, fake scanner binaries and a fake model API."""

from __future__ import annotations

import io
import json
import os
import stat
import sys
import urllib.error
import urllib.request
from email.message import Message
from pathlib import Path

TESTS = Path(__file__).resolve().parent
FIXTURES = TESTS / "fixtures"
sys.path.insert(0, str(TESTS.parent / "scripts"))


def fixture(*parts: str) -> object:
    """Load a JSON fixture (real scanner output, trimmed)."""
    return json.loads((FIXTURES.joinpath(*parts)).read_text(encoding="utf-8"))


def fake_scanner(folder: Path, name: str, *, report: Path | None = None, exit_code: int = 0,
                 sleep: int = 0, env_dump: Path | None = None) -> Path:
    """Write a fake scanner: it copies `report` to the path after --output or --report-path.

    It never reads the skill. Optional: exit with exit_code, sleep first, or dump its environment.
    """
    lines = [
        "#!/bin/sh",
        "out=''",
        "prev=''",
        'for arg in "$@"; do',
        '  if [ "$prev" = "--output" ] || [ "$prev" = "--report-path" ]; then out="$arg"; fi',
        '  prev="$arg"',
        "done",
    ]
    if sleep:
        lines.append(f"sleep {sleep}")
    if env_dump:
        lines.append(f"env > '{env_dump}'")
    if report:
        lines.append(f'cp \'{report}\' "$out"')
    lines.append(f"exit {exit_code}")
    path = folder / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


class ScannerEnv:
    """Context manager that points the *_BIN variables at given paths and restores them.

    It also removes ANTHROPIC_API_KEY for its duration (or sets the given test
    value), so a scan in a test never calls the real API from a shell that
    happens to hold a key.
    """

    KEYS = ("SKILL_SCANNER_BIN", "BETTERLEAKS_BIN")

    def __init__(self, cisco: Path, betterleaks: Path, *, model_key: str | None = None):
        self.values: dict[str, str | None] = dict(zip(self.KEYS, (str(cisco), str(betterleaks))))
        self.values["ANTHROPIC_API_KEY"] = model_key
        self.saved: dict[str, str | None] = {}

    def __enter__(self) -> "ScannerEnv":
        for key, value in self.values.items():
            self.saved[key] = os.environ.get(key)
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        return self

    def __exit__(self, *exc: object) -> None:
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value


def model_answer(review: dict | None = None, *, stop_reason: str = "end_turn", text: str | None = None) -> dict:
    """A Messages API answer: an (empty) thinking block, then the review as the JSON text block."""
    body = text if text is not None else json.dumps(review)
    return {
        "id": "msg_test", "type": "message", "role": "assistant", "model": "test",
        "content": [{"type": "thinking", "thinking": "", "signature": "sig"}, {"type": "text", "text": body}],
        "stop_reason": stop_reason,
        "usage": {"input_tokens": 1, "output_tokens": 1},
    }


def http_error(code: int, body: bytes = b"") -> urllib.error.HTTPError:
    """The error urlopen raises for an HTTP status other than 2xx."""
    return urllib.error.HTTPError("https://api.anthropic.com/v1/messages", code, "error", Message(), io.BytesIO(body))


class _Response:
    def __init__(self, payload: dict) -> None:
        self.status = 200
        self._raw = json.dumps(payload).encode("utf-8")

    def read(self, limit: int = -1) -> bytes:
        return self._raw if limit < 0 else self._raw[:limit]

    def __enter__(self) -> "_Response":
        return self

    def __exit__(self, *exc: object) -> None:
        pass


class FakeAnthropic:
    """Stands in for urllib.request.urlopen: records each request and answers from a script.

    Each scripted answer is a payload dict (HTTP 200) or an exception to raise
    (an HTTPError, a TimeoutError). Nothing reaches the Anthropic API. A request
    to any other URL (the local fake site in test_scan_queue) goes to the real
    urlopen.
    """

    def __init__(self, *answers: object) -> None:
        self.answers = list(answers)
        self.requests: list[dict] = []
        self._real_urlopen = urllib.request.urlopen

    def __call__(self, request: urllib.request.Request, timeout: float | None = None) -> object:
        if not request.full_url.startswith("https://api.anthropic.com/"):
            return self._real_urlopen(request, timeout=timeout)
        self.requests.append({
            "url": request.full_url,
            "method": request.get_method(),
            "headers": {key.lower(): value for key, value in request.header_items()},
            "body": json.loads(request.data),
            "timeout": timeout,
        })
        if not self.answers:
            raise AssertionError("the fake API got more requests than it has answers")
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return _Response(answer)

    @property
    def models(self) -> list[str]:
        return [request["body"]["model"] for request in self.requests]
