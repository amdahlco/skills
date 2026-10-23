"""Shared helpers for the tests: import path, fixtures and fake scanner binaries."""

from __future__ import annotations

import json
import os
import stat
import sys
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
    """Context manager that points the *_BIN variables at given paths and restores them."""

    KEYS = ("SKILLSPECTOR_BIN", "SKILL_SCANNER_BIN", "BETTERLEAKS_BIN")

    def __init__(self, skillspector: Path, cisco: Path, betterleaks: Path):
        self.values = dict(zip(self.KEYS, (str(skillspector), str(cisco), str(betterleaks))))
        self.saved: dict[str, str | None] = {}

    def __enter__(self) -> "ScannerEnv":
        for key, value in self.values.items():
            self.saved[key] = os.environ.get(key)
            os.environ[key] = value
        return self

    def __exit__(self, *exc: object) -> None:
        for key, value in self.saved.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
