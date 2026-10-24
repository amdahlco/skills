"""walls.py: what the scanner user must not be able to write, and the lock that closes it.

The real proof runs on GitHub's runner, as the scanner user (scripts/sandbox.sh
scan, in self-test.yml and scan-queue.yml). Here the test user owns every file
in its temp folder, so "the scanner user can write this" is a stand-in
predicate: a path in self.open, or (after lock) a path with group or other write.
"""

from __future__ import annotations

import errno
import os
import stat
import subprocess
import sys
import tempfile
import unittest

from support import TESTS

import walls

SCRIPTS = TESTS.parent / "scripts"
OPEN_BITS = stat.S_IWGRP | stat.S_IWOTH


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._tmp.name)  # macOS: /var is a symlink to /private/var
        self.open: set[str] = set()

    def tearDown(self) -> None:
        for parent, dirs, _ in os.walk(self.root):
            for name in dirs:
                path = os.path.join(parent, name)
                if not os.path.islink(path):
                    os.chmod(path, 0o755)
        self._tmp.cleanup()

    def path(self, *parts: str) -> str:
        return os.path.join(self.root, *parts)

    def folder(self, *parts: str, mode: int = 0o755) -> str:
        path = self.path(*parts)
        os.makedirs(path, exist_ok=True)
        os.chmod(path, mode)
        return path

    def file(self, *parts: str, mode: int = 0o755) -> str:
        path = self.path(*parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n")
        os.chmod(path, mode)
        return path

    def link(self, target: str, *parts: str) -> str:
        path = self.path(*parts)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        os.symlink(target, path)
        return path

    def writable(self, path: str) -> bool:
        return path in self.open

    def open_bits(self, path: str) -> bool:
        # After lock: could anyone but the owner write it? Only inside the temp folder.
        return path.startswith(self.root) and bool(os.stat(path).st_mode & OPEN_BITS)

    def local(self, notes: list[str]) -> list[str]:
        # On Linux the temp folder sits in /tmp, a sticky folder lock rightly leaves alone.
        return [note for note in notes if note.startswith(self.root)]


class SearchPaths(unittest.TestCase):
    def test_folders_in_order_without_repeats(self) -> None:
        self.assertEqual(walls.split_dirs("/a:/b:/a"), ["/a", "/b"])
        self.assertEqual(walls.split_dirs(""), [])

    def test_an_empty_or_relative_entry_is_refused(self) -> None:
        # Both mean "the current folder" to a lookup, which no wall covers.
        for value in ("/a::/b", "/a:", "/a:bin", "."):
            with self.assertRaises(walls.WallError, msg=value):
                walls.split_dirs(value)


class Lookup(Base):
    def test_every_folder_a_name_is_read_from_through_symlinks(self) -> None:
        tool = self.file("real", "bin", "tool")
        self.link("real", "link")  # relative
        self.link(self.path("link", "bin"), "abs")  # absolute, through the first link
        folders, final = walls.lookup(self.path("abs", "tool"))
        self.assertEqual(final, tool)
        for folder in ("/", self.root, self.path("real"), self.path("real", "bin")):
            self.assertIn(folder, folders)

    def test_a_missing_part_stops_at_the_folder_that_could_hold_it(self) -> None:
        self.folder("real")
        folders, final = walls.lookup(self.path("real", "nope", "tool"))
        self.assertIsNone(final)
        self.assertEqual(folders[-1], self.path("real"))

    def test_a_symlink_loop_or_a_relative_path_fails(self) -> None:
        self.link("two", "one")
        self.link("one", "two")
        with self.assertRaises(walls.WallError):
            walls.lookup(self.path("one"))
        with self.assertRaises(walls.WallError):
            walls.lookup("relative/path")


class Check(Base):
    def setUp(self) -> None:
        super().setUp()
        self.bin = self.folder("opt", "bin")
        self.git = self.file("opt", "bin", "git")
        self.target = self.file("opt", "venv", "bin", "tool")
        self.link(self.target, "opt", "bin", "tool")
        self.lib = self.folder("opt", "lib")
        self.deep = self.file("opt", "lib", "python3", "site", "x.pth", mode=0o644)
        self.shell = self.file("usr", "bin", "bash")
        self.stdlib = self.folder("usr", "lib", "python3")
        self.module = self.file("usr", "lib", "python3", "os.py", mode=0o644)

    def run_check(self) -> list[str]:
        interpreter = (self.shell, [self.path("usr", "lib", "python.zip"), self.stdlib])
        return walls.check([self.bin], [self.lib], [self.shell], interpreter, self.writable)

    def test_nothing_writable_means_the_walls_hold(self) -> None:
        self.assertEqual(self.run_check(), [])

    def test_each_place_a_skill_could_hide_code_is_found(self) -> None:
        cases = {
            "a new command ahead of the real one": self.bin,
            "a parent of a PATH folder (rename it, put your own in place)": self.path("opt"),
            "a command on PATH": self.git,
            "the file a command on PATH points to": self.target,
            "a folder on the way to that file": self.path("opt", "venv", "bin"),
            "a library or .pth file in a LD_LIBRARY_PATH folder": self.deep,
            "the shell": self.shell,
            "a module on the import path": self.module,
            "the folder that would hold a missing import path entry": self.path("usr", "lib"),
        }
        for label, path in cases.items():
            self.open = {path}
            self.assertEqual(self.run_check(), [path], label)

    def test_a_folder_it_can_search_but_not_list_fails_closed(self) -> None:
        hidden = self.folder("opt", "lib", "hidden", mode=0o300)
        if os.access(hidden, os.R_OK):
            self.skipTest("running as root")
        self.assertEqual(self.run_check(), [f"{hidden} (cannot list it)"])

    def test_a_folder_it_cannot_enter_is_not_a_problem(self) -> None:
        # The runner user's home is 700: the scanner user cannot write anything inside it.
        closed = self.folder("home", mode=0o000)
        if os.access(closed, os.X_OK):
            self.skipTest("running as root")
        self.assertEqual(walls.check([os.path.join(closed, "bin")], [], [], None, self.writable), [])

    def test_a_relative_or_looping_path_is_a_problem(self) -> None:
        self.link("loop", "opt", "lib", "loop")
        self.assertEqual(self.run_check(), [f"{self.path('opt', 'lib', 'loop')} (too many symlinks under "
                                            f"{self.path('opt', 'lib', 'loop')})"])


class Lock(Base):
    def test_it_closes_what_check_covers_to_group_and_others(self) -> None:
        parent = self.folder("opt", mode=0o777)
        path_dir = self.folder("opt", "pipx_bin", mode=0o777)
        command = self.file("opt", "pipx_bin", "python3", mode=0o777)
        venv_bin = self.folder("opt", "pipx", "venvs", "x", "bin", mode=0o777)
        target = self.file("opt", "pipx", "venvs", "x", "bin", "x", mode=0o777)
        self.link(target, "opt", "pipx_bin", "x")
        ld_dir = self.folder("opt", "py", "lib", mode=0o777)
        nested = self.folder("opt", "py", "lib", "python3.12", "site-packages", mode=0o777)
        library = self.file("opt", "py", "lib", "libc.so.6", mode=0o666)
        pth = self.file("opt", "py", "lib", "python3.12", "site-packages", "evil.pth", mode=0o666)

        self.assertNotEqual(walls.check([path_dir], [ld_dir], [], None, self.open_bits), [])
        self.assertEqual(self.local(walls.lock([path_dir], [ld_dir])), [])
        self.assertEqual(walls.check([path_dir], [ld_dir], [], None, self.open_bits), [])
        for path in (parent, path_dir, command, venv_bin, target, ld_dir, nested, library, pth):
            self.assertFalse(os.stat(path).st_mode & OPEN_BITS, path)
        # The owner keeps its own rights.
        self.assertEqual(stat.S_IMODE(os.stat(library).st_mode), 0o644)
        self.assertEqual(stat.S_IMODE(os.stat(path_dir).st_mode), 0o755)

    def test_it_never_changes_a_sticky_folder_and_check_reports_it(self) -> None:
        shared = self.folder("tmp", mode=0o1777)
        path_dir = self.folder("tmp", "bin")
        notes = self.local(walls.lock([path_dir], []))
        self.assertEqual(notes, [f"{shared} is a shared sticky folder; left as it is"])
        self.assertEqual(stat.S_IMODE(os.stat(shared).st_mode), 0o1777)
        self.assertEqual(walls.check([path_dir], [], [], None, self.open_bits), [shared])

    def test_a_file_it_cannot_change_is_a_note_and_check_decides(self) -> None:
        path_dir = self.folder("bin", mode=0o777)

        def read_only(path: str, mode: int) -> None:
            raise OSError(errno.EROFS, "Read-only file system", path)

        self.assertEqual(self.local(walls.lock([path_dir], [], read_only)),
                         [f"{path_dir} could not be closed: Read-only file system"])


class Command(unittest.TestCase):
    def run_script(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([sys.executable, *args], capture_output=True, text=True, timeout=60)

    def test_check_refuses_to_run_without_isolation_and_no_site(self) -> None:
        # The import path it proves must be the one the later steps get.
        done = self.run_script("-I", str(SCRIPTS / "walls.py"), "check", "--path", "/usr/bin")
        self.assertEqual(done.returncode, 1)
        self.assertIn("run check with python3 -I -S", done.stderr)

    def test_a_relative_search_path_fails_closed(self) -> None:
        for command in ("lock", "check"):
            done = self.run_script("-I", "-S", str(SCRIPTS / "walls.py"), command, "--path", "/usr/bin:bin")
            self.assertEqual(done.returncode, 1, command)
            self.assertIn("relative entry", done.stderr, command)

    def test_the_later_steps_scripts_run_without_the_site_module(self) -> None:
        # scan-queue.yml and self-test.yml run them as /usr/bin/python3 -I -S.
        for script in ("scan_queue.py", "self_test.py", "walls.py"):
            done = self.run_script("-I", "-S", str(SCRIPTS / script), "--help")
            self.assertEqual(done.returncode, 0, f"{script}: {done.stderr}")


if __name__ == "__main__":
    unittest.main()
