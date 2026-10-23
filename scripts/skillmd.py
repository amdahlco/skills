"""Shared rules for skill names, ids, digests and the SKILL.md size cap.

Standard library only. Every name that becomes part of a path goes through
`is_valid_name` first, so a name can never climb out of its folder.

The SKILL.md format (frontmatter, description, body length) is checked by
amdahl.ai's gate, not here: a second reader with its own rules would refuse
files the site accepts.
"""

from __future__ import annotations

import hashlib
import re

NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
NAME_MAX = 64
MARKDOWN_MAX_BYTES = 1024 * 1024  # a larger SKILL.md is not scanned or reviewed
UUID_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
DIGEST_RE = re.compile(r"^[0-9a-f]{64}$")


def is_valid_name(value: object) -> bool:
    """True when value is a safe skill or publisher name (1-64 chars, a-z, 0-9, single hyphens)."""
    return isinstance(value, str) and 0 < len(value) <= NAME_MAX and NAME_RE.fullmatch(value) is not None


def sha256_hex(markdown: str) -> str:
    """The digest the site uses: sha256 of the markdown as UTF-8 bytes, lowercase hex."""
    return hashlib.sha256(markdown.encode("utf-8")).hexdigest()
