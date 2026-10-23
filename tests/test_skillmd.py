"""Name rules and the digest. The SKILL.md format is amdahl.ai's gate's job, not this module's."""

from __future__ import annotations

import unittest

import support  # noqa: F401 - puts scripts/ on the import path

import skillmd


class Names(unittest.TestCase):
    def test_valid(self) -> None:
        for name in ("a", "cold-email", "q3-pipeline-review", "x" * 64, "a1-b2"):
            self.assertTrue(skillmd.is_valid_name(name), name)

    def test_invalid_including_path_tricks(self) -> None:
        for name in ("", "-a", "a-", "a--b", "A", "a_b", "a.b", "..", "../etc", "a/b", "a\\b",
                     "x" * 65, " a", "a\n", "caf\u00e9", None, 7):
            self.assertFalse(skillmd.is_valid_name(name), repr(name))


class Digest(unittest.TestCase):
    def test_digest(self) -> None:
        self.assertEqual(
            skillmd.sha256_hex("abc"),
            "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad",
        )

    def test_no_format_reader(self) -> None:
        # A second SKILL.md reader here would refuse files the site's gate accepts
        # (a 501-1000 line body, a quoted name with a comment, flow-style YAML).
        for gone in ("validate_skill_md", "parse_frontmatter", "split_frontmatter", "BODY_MAX_LINES"):
            self.assertFalse(hasattr(skillmd, gone), gone)


if __name__ == "__main__":
    unittest.main()
