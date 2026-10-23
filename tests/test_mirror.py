"""mirror.py: catalog validation, the sync itself and the removal guards."""

from __future__ import annotations

import io
import tempfile
import unittest
from contextlib import redirect_stdout
from pathlib import Path

from support import FIXTURES

import mirror
from skillmd import MARKDOWN_MAX_BYTES, sha256_hex

CLEAN = (FIXTURES / "inputs" / "discovery-call-notes.md").read_text(encoding="utf-8")


def entry(publisher: str = "amdahl", name: str = "discovery-call-notes", markdown: str = CLEAN) -> dict:
    return {"publisher": publisher, "name": name, "digest": sha256_hex(markdown), "markdown": markdown}


def renamed(name: str) -> str:
    return CLEAN.replace("name: discovery-call-notes", f"name: {name}")


def skill_text(front: str, body_lines: int = 3) -> str:
    """A SKILL.md whose body (after the closing ---) is exactly body_lines lines."""
    return f"---\n{front}\n---\n" + "Write the notes.\n" * body_lines


class Catalog(unittest.TestCase):
    def quiet(self, payload: object) -> dict:
        with redirect_stdout(io.StringIO()):
            return mirror.expected_skills(payload)

    def test_valid(self) -> None:
        self.assertEqual(list(self.quiet({"skills": [entry()]})), [("amdahl", "discovery-call-notes")])

    def test_skips_bad_entries(self) -> None:
        bad = [
            entry(publisher="../x"),
            entry(name="Bad"),
            dict(entry(), digest="0" * 64),
            dict(entry(), digest=None),
            entry(name="too-big", markdown="x" * (MARKDOWN_MAX_BYTES + 1)),
            "not an object",
        ]
        self.assertEqual(self.quiet({"skills": bad}), {})

    def test_listed_twice_keeps_the_first(self) -> None:
        second = CLEAN + "\nA later copy.\n"
        got = self.quiet({"skills": [entry(), entry(markdown=second)]})
        self.assertEqual(got, {("amdahl", "discovery-call-notes"): CLEAN})

    def test_keeps_every_file_the_site_gate_accepts(self) -> None:
        # The site lists these: its gate refuses a body only past 1000 lines and reads
        # the front matter as YAML. A mirror that re-checked them dropped the skill, and
        # the next sync deleted its folder while the site still served it.
        accepted = {
            "long-body": skill_text("name: long-body\ndescription: Writes notes. Use after a call.", 601),
            "longest-body": skill_text("name: longest-body\ndescription: Writes notes. Use after a call.", 1000),
            "quoted-name": skill_text('name: "quoted-name" # the folder name\ndescription: Writes notes.'),
            "flow-map": skill_text("{name: flow-map, description: Writes notes. Use after a call.}"),
        }
        catalog = {"skills": [entry(publisher="acme", name=name, markdown=text) for name, text in accepted.items()]}
        got = self.quiet(catalog)
        self.assertEqual(got, {("acme", name): text for name, text in accepted.items()})

    def test_a_long_new_version_replaces_the_old_one(self) -> None:
        old = skill_text("name: call-prep\ndescription: Preps a call.")
        new = skill_text("name: call-prep\ndescription: Preps a call.", 601)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "skills"
            mirror.sync(root, self.quiet({"skills": [entry(publisher="acme", name="call-prep", markdown=old)]}))
            changes = mirror.sync(root, self.quiet({"skills": [entry(publisher="acme", name="call-prep", markdown=new)]}))
            self.assertEqual(changes, {"added": [], "updated": ["skills/acme/call-prep"], "removed": []})
            self.assertEqual((root / "acme" / "call-prep" / "SKILL.md").read_text(encoding="utf-8"), new)

    def test_skips_folder_skills(self) -> None:
        # A folder skill's SKILL.md is valid on its own, but it is not the skill.
        folder = dict(entry(name="system-diagram", markdown=renamed("system-diagram")), kind="folder",
                      archiveUrl="https://amdahl.ai/skills/amdahl/system-diagram.tar.gz")
        single = dict(entry(), kind="file")
        unmarked = entry(publisher="jane", name="a", markdown=renamed("a"))
        self.assertEqual(sorted(self.quiet({"skills": [folder, single, unmarked]})),
                         [("amdahl", "discovery-call-notes"), ("jane", "a")])

    def test_shape(self) -> None:
        with self.assertRaises(mirror.MirrorError):
            self.quiet({"items": []})


class Sync(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "skills"

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def test_add_update_remove(self) -> None:
        first = {("amdahl", "discovery-call-notes"): CLEAN, ("jane", "a"): renamed("a")}
        changes = mirror.sync(self.root, first)
        self.assertEqual(len(changes["added"]), 2)
        self.assertEqual((self.root / "amdahl" / "discovery-call-notes" / "SKILL.md").read_text(), CLEAN)

        again = mirror.sync(self.root, first)
        self.assertEqual(again, {"added": [], "updated": [], "removed": []})

        second = {("amdahl", "discovery-call-notes"): CLEAN + "\nMore.\n"}
        changes = mirror.sync(self.root, second)
        self.assertEqual(changes["updated"], ["skills/amdahl/discovery-call-notes"])
        self.assertEqual(changes["removed"], ["skills/jane"])
        self.assertFalse((self.root / "jane").exists())

    def test_removes_strays_and_links_without_following_them(self) -> None:
        outside = Path(self.tmp.name) / "outside"
        outside.mkdir()
        (outside / "keep.txt").write_text("must survive")
        wanted = {("amdahl", "discovery-call-notes"): CLEAN}
        mirror.sync(self.root, wanted)
        (self.root / "stray.txt").write_text("x")
        (self.root / "amdahl" / "discovery-call-notes" / "script.sh").write_text("x")
        (self.root / "evil").symlink_to(outside, target_is_directory=True)
        skill_file = self.root / "amdahl" / "discovery-call-notes" / "SKILL.md"
        skill_file.unlink()
        skill_file.symlink_to(outside / "keep.txt")

        mirror.sync(self.root, wanted)
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), ["amdahl"])
        self.assertEqual(sorted(p.name for p in skill_file.parent.iterdir()), ["README.md", "SKILL.md"])
        self.assertFalse(skill_file.is_symlink())
        self.assertEqual(skill_file.read_text(), CLEAN)
        self.assertEqual((outside / "keep.txt").read_text(), "must survive")


class InstallNote(unittest.TestCase):
    """Each mirrored folder says how to install the skill from amdahl.ai, where installs are counted."""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "skills"
        self.wanted = {("amdahl", "discovery-call-notes"): CLEAN}

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def note(self) -> Path:
        return self.root / "amdahl" / "discovery-call-notes" / "README.md"

    def test_gives_the_amdahl_command_and_page(self) -> None:
        mirror.sync(self.root, self.wanted)
        text = self.note().read_text(encoding="utf-8")
        self.assertIn("npx amdahl skills add amdahl/discovery-call-notes\n", text)
        self.assertIn("https://amdahl.ai/skills/amdahl/discovery-call-notes", text)
        self.assertNotIn("--site", text)
        self.assertNotIn("npx skills", text)
        self.assertTrue(text.isascii())

    def test_names_another_site(self) -> None:
        mirror.sync(self.root, self.wanted, "http://localhost:6777")
        text = self.note().read_text(encoding="utf-8")
        self.assertIn("npx amdahl skills add amdahl/discovery-call-notes --site http://localhost:6777\n", text)
        self.assertIn("http://localhost:6777/skills/amdahl/discovery-call-notes", text)

    def test_skill_md_keeps_the_served_bytes(self) -> None:
        mirror.sync(self.root, self.wanted)
        skill = self.root / "amdahl" / "discovery-call-notes" / "SKILL.md"
        self.assertEqual(skill.read_bytes(), CLEAN.encode("utf-8"))

    def test_a_missing_or_edited_note_is_written_back(self) -> None:
        mirror.sync(self.root, self.wanted)
        expected = self.note().read_bytes()
        self.note().write_text("edited by hand")
        self.assertEqual(mirror.sync(self.root, self.wanted)["updated"], ["skills/amdahl/discovery-call-notes"])
        self.assertEqual(self.note().read_bytes(), expected)
        self.note().unlink()
        self.assertEqual(mirror.sync(self.root, self.wanted)["updated"], ["skills/amdahl/discovery-call-notes"])
        self.assertEqual(mirror.sync(self.root, self.wanted), {"added": [], "updated": [], "removed": []})

    def test_a_linked_note_is_replaced_without_following_it(self) -> None:
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("must survive")
        mirror.sync(self.root, self.wanted)
        self.note().unlink()
        self.note().symlink_to(outside)
        mirror.sync(self.root, self.wanted)
        self.assertFalse(self.note().is_symlink())
        self.assertIn("npx amdahl skills add", self.note().read_text(encoding="utf-8"))
        self.assertEqual(outside.read_text(), "must survive")


class Guards(unittest.TestCase):
    def test_empty_catalog_never_wipes(self) -> None:
        with self.assertRaises(mirror.MirrorError):
            mirror.check_guards({("a", "b")}, {}, allow_mass_removal=False)
        mirror.check_guards({("a", "b")}, {}, allow_mass_removal=True)

    def test_half_limit(self) -> None:
        existing = {("p", f"s{i}") for i in range(20)}
        keep_nine = {key: "" for key in list(sorted(existing))[:9]}
        with self.assertRaises(mirror.MirrorError):
            mirror.check_guards(existing, keep_nine, allow_mass_removal=False)
        keep_ten = {key: "" for key in list(sorted(existing))[:10]}
        mirror.check_guards(existing, keep_ten, allow_mass_removal=False)

    def test_small_mirror_may_lose_five(self) -> None:
        existing = {("p", f"s{i}") for i in range(6)}
        mirror.check_guards(existing, {("p", "s0"): ""}, allow_mass_removal=False)


if __name__ == "__main__":
    unittest.main()
