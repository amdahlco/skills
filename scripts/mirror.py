"""Mirror every published skill from amdahl.ai into skills/<publisher>/<name>/SKILL.md.

Usage:
    python3 scripts/mirror.py [--root skills] [--allow-mass-removal]

amdahl.ai is the source of truth. After a run, the skills/ folder holds
exactly the catalog: new and changed skills are written, and anything that
is not in the catalog is removed. Two guards stop an outage from wiping the
mirror: an empty catalog never removes existing skills, and one run never
removes more than half of them (at least 5 may always go). Pass
--allow-mass-removal to override both.

The mirror holds single-file skills only. A catalog entry with kind 'folder'
(a whole folder that installs from a tar.gz) is skipped: its source is its
own repository, and its SKILL.md alone is not the skill.

The mirror does not check the SKILL.md format again. The site's gate passed
every file before it was listed, and a second reader with its own rules would
skip, and then remove, skills the site still serves. The mirror checks only
what it needs to write a file safely: the names, the size and the digest.

Environment: AMDAHL_SITE (default https://amdahl.ai). No token is needed.

Standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import urllib.error
import urllib.request
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))  # works under `python3 -I` too

from scan_queue import MAX_RESPONSE_BYTES, USER_AGENT, site_base  # noqa: E402
from skillmd import DIGEST_RE, MARKDOWN_MAX_BYTES, is_valid_name, sha256_hex  # noqa: E402

CATALOG_PATH = "/api/skills/catalog"
HTTP_TIMEOUT_SECONDS = 60
MIN_FREE_REMOVALS = 5


class MirrorError(RuntimeError):
    """The catalog could not be read, or a guard refused the change."""


def warn(message: str) -> None:
    """A GitHub Actions warning annotation (plain text outside Actions)."""
    print(f"::warning::{message}")


def fetch_catalog(base: str) -> object:
    """GET the public catalog and parse its JSON."""
    request = urllib.request.Request(base + CATALOG_PATH, headers={"Accept": "application/json", "User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        raise MirrorError(f"GET {CATALOG_PATH} answered HTTP {error.code}") from None
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        raise MirrorError(f"GET {CATALOG_PATH} failed: {getattr(error, 'reason', error)}") from None
    if len(raw) > MAX_RESPONSE_BYTES:
        raise MirrorError(f"GET {CATALOG_PATH} answered more than {MAX_RESPONSE_BYTES} bytes")
    try:
        return json.loads(raw)
    except ValueError:
        raise MirrorError(f"GET {CATALOG_PATH} did not answer JSON") from None


def expected_skills(payload: object) -> dict[tuple[str, str], str]:
    """Validate the catalog and return {(publisher, name): markdown}. Bad entries are skipped with a warning.

    The SKILL.md format is never checked here: the site's gate owns it (see the module docstring).
    """
    if not isinstance(payload, dict) or not isinstance(payload.get("skills"), list):
        raise MirrorError("the catalog has no 'skills' list")
    wanted: dict[tuple[str, str], str] = {}
    for index, entry in enumerate(payload["skills"]):
        if not isinstance(entry, dict):
            warn(f"catalog entry {index} is not an object; skipped")
            continue
        publisher, name = entry.get("publisher"), entry.get("name")
        digest, markdown = entry.get("digest"), entry.get("markdown")
        if not is_valid_name(publisher) or not is_valid_name(name):
            warn(f"catalog entry {index} has an invalid publisher or name; skipped")
            continue
        label = f"{publisher}/{name}"
        if entry.get("kind") == "folder":
            print(f"{label}: a folder skill; its source is its own repository, so it is not mirrored")
            continue
        if not isinstance(markdown, str) or not isinstance(digest, str) or not DIGEST_RE.fullmatch(digest.lower()):
            warn(f"{label}: missing markdown or digest; skipped")
            continue
        if len(markdown.encode("utf-8")) > MARKDOWN_MAX_BYTES:
            warn(f"{label}: larger than 1 MB; skipped")
            continue
        if sha256_hex(markdown) != digest.lower():
            warn(f"{label}: digest does not match the markdown; skipped")
            continue
        if (publisher, name) in wanted:
            warn(f"{label}: listed twice; kept the first")
            continue
        wanted[(publisher, name)] = markdown
    for name, count in Counter(name for _, name in wanted).items():
        if count > 1:
            warn(f"'{name}' is published by {count} publishers; `--skill {name}` will match more than one")
    return wanted


def _remove(path: Path) -> None:
    """Delete a file, symlink or real directory without following links."""
    if path.is_symlink() or not path.is_dir():
        path.unlink()
    else:
        shutil.rmtree(path)


def existing_skills(root: Path) -> set[tuple[str, str]]:
    """Skill folders present now (only real directories with valid names count)."""
    found = set()
    if not root.is_dir() or root.is_symlink():
        return found
    for publisher in os.scandir(root):
        if publisher.is_dir(follow_symlinks=False) and is_valid_name(publisher.name):
            for skill in os.scandir(publisher.path):
                if skill.is_dir(follow_symlinks=False) and is_valid_name(skill.name):
                    found.add((publisher.name, skill.name))
    return found


def check_guards(existing: set, wanted: dict, allow_mass_removal: bool) -> None:
    """Refuse an empty catalog or a large removal unless explicitly allowed."""
    if allow_mass_removal:
        return
    removals = len(existing - set(wanted))
    if existing and not wanted:
        raise MirrorError("the catalog is empty but the mirror is not; refusing to remove every skill")
    limit = max(MIN_FREE_REMOVALS, len(existing) // 2)
    if removals > limit:
        raise MirrorError(f"this run would remove {removals} of {len(existing)} skills (limit {limit}); refusing")


def sync(root: Path, wanted: dict[tuple[str, str], str]) -> dict[str, list[str]]:
    """Make root hold exactly `wanted`. Returns the paths added, updated and removed."""
    changes: dict[str, list[str]] = {"added": [], "updated": [], "removed": []}
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        _remove(root)
    root.mkdir(exist_ok=True)
    keep_publishers = {publisher for publisher, _ in wanted}

    for entry in sorted(os.scandir(root), key=lambda e: e.name):
        path = Path(entry.path)
        if not entry.is_dir(follow_symlinks=False) or entry.name not in keep_publishers:
            _remove(path)
            changes["removed"].append(f"{root.name}/{entry.name}")
            continue
        for skill in sorted(os.scandir(path), key=lambda e: e.name):
            if not skill.is_dir(follow_symlinks=False) or (entry.name, skill.name) not in wanted:
                _remove(Path(skill.path))
                changes["removed"].append(f"{root.name}/{entry.name}/{skill.name}")
                continue
            for inner in os.scandir(skill.path):
                if inner.name != "SKILL.md" or not inner.is_file(follow_symlinks=False):
                    _remove(Path(inner.path))

    for (publisher, name), markdown in sorted(wanted.items()):
        folder = root / publisher / name
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / "SKILL.md"
        data = markdown.encode("utf-8")
        relative = f"{root.name}/{publisher}/{name}"
        if target.is_file() and not target.is_symlink():
            if target.read_bytes() == data:
                continue
            changes["updated"].append(relative)
        else:
            changes["added"].append(relative)
        target.write_bytes(data)
    return changes


def _append(env_var: str, text: str) -> None:
    target = os.environ.get(env_var)
    if target:
        with open(target, "a", encoding="utf-8") as handle:
            handle.write(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mirror the amdahl.ai skills catalog into skills/.")
    parser.add_argument("--root", type=Path, default=Path("skills"))
    parser.add_argument("--allow-mass-removal", action="store_true")
    args = parser.parse_args(argv)
    try:
        base = site_base(os.environ.get("AMDAHL_SITE"))
        wanted = expected_skills(fetch_catalog(base))
        check_guards(existing_skills(args.root), wanted, args.allow_mass_removal)
    except Exception as error:  # noqa: BLE001 - one clear line, never a partial write
        print(f"Error: {error}", file=sys.stderr)
        return 1
    changes = sync(args.root, wanted)
    for kind in ("added", "updated", "removed"):
        for path in changes[kind]:
            print(f"{kind}: {path}")
    counts = {kind: len(paths) for kind, paths in changes.items()}
    print(f"Catalog: {len(wanted)} skill(s). Added {counts['added']}, updated {counts['updated']}, removed {counts['removed']}.")
    _append("GITHUB_OUTPUT", "".join(f"{kind}={count}\n" for kind, count in counts.items()))
    _append(
        "GITHUB_STEP_SUMMARY",
        f"### Mirror\n\n{len(wanted)} skills in the catalog. Added {counts['added']}, "
        f"updated {counts['updated']}, removed {counts['removed']}.\n",
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
