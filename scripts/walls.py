"""Prove that the scanner user cannot change anything the job runs after the scan.

    sudo /usr/bin/python3 -I -S scripts/walls.py lock --path "$PATH" --ld-path "$LD_LIBRARY_PATH"
    /usr/bin/python3 -I -S walls.py check --path ... --ld-path ... --run /usr/bin/bash --run /usr/bin/python3

scripts/sandbox.sh runs `lock` as root before the scanner user exists, and
`check` AS the scanner user before that user reads a skill.

Why: code that breaks out of a scanner runs as the scanner user. After the
scan, the same machine runs steps that hold secrets: the AI review
(ANTHROPIC_API_KEY), the post step (SKILLS_SCAN_TOKEN), and the runner's own
clean-up steps (checkout's clean-up runs `git`, and the runner process keeps
the job's secrets in memory). If the scanner user can write a file that one
of those steps runs or loads, it gets those secrets. As far as we know,
GitHub's Ubuntu image leaves /opt writable by every user (its build runs
`chmod -R 777 /opt`; `check` proves the real state on every run). /opt holds
the Python that actions/setup-python puts first on PATH and on
LD_LIBRARY_PATH (the loader reads that for every program, bash included),
and /opt/pipx_bin, which is on PATH before /usr/bin.

So after the scan, the workflows run only /usr/bin/bash (the steps' shell,
by its full path) and /usr/bin/python3 -I -S (root's Python, with no site
module, so no .pth file or sitecustomize runs), plus whatever the runner
finds on PATH. `check` proves, as the scanner user, that it can write none of:
  - each --run file, the file it points to, or any folder on the way to it;
  - the Python that runs `check` (run it with /usr/bin/python3 -I -S, the way
    the later steps run), and everything on that Python's import path;
  - each folder on PATH or LD_LIBRARY_PATH, or any folder above one, so no
    command and no library can be put ahead of the real one;
  - each command in a PATH folder, the file it points to, or the folders on the way;
  - anything inside a LD_LIBRARY_PATH folder.
`lock` removes group and other write from the same PATH and LD_LIBRARY_PATH
paths (plus the folders sudo finds commands in). It never changes a sticky
folder such as /tmp: `check` reports one instead.

It fails closed: a relative entry, a symlink loop or a folder it cannot list
is a problem too.

Standard library only. Runs under `python3 -I -S`.
"""

from __future__ import annotations

import argparse
import os
import stat
import sys
from typing import Callable

# sudo finds commands in its secure_path (Ubuntu's default list), and sandbox.sh
# runs its own commands from these after the scan. Checked even when PATH lacks one.
SYSTEM_BIN_DIRS = ("/usr/local/sbin", "/usr/local/bin", "/usr/sbin", "/usr/bin", "/sbin", "/bin", "/snap/bin")
MAX_LINKS = 40
SHOW = 25
OPEN_BITS = stat.S_IWGRP | stat.S_IWOTH


class WallError(ValueError):
    """A path whose walls cannot be proven: a relative entry or a symlink loop."""


def can_write(path: str) -> bool:
    """Whether the user running this may write path (access(2), so ACLs count)."""
    return os.access(path, os.W_OK)


def split_dirs(value: str) -> list[str]:
    """The folders in a PATH-style value, in order, without repeats.

    An empty or relative entry means "the current folder" to a lookup, which
    no wall covers, so it raises WallError.
    """
    found: list[str] = []
    if not value:
        return found
    for entry in value.split(os.pathsep):
        if not entry or not os.path.isabs(entry):
            raise WallError(f"relative entry {entry!r} in a search path")
        if entry not in found:
            found.append(entry)
    return found


def lookup(path: str) -> tuple[list[str], str | None]:
    """Resolve an absolute path one name at a time, the way the kernel does.

    Returns (every folder a name was read from, through every symlink; the
    final path, or None when part of it does not exist or cannot be reached).
    Whoever can write one of those folders can send the lookup somewhere else.
    """
    if not os.path.isabs(path):
        raise WallError(f"relative path {path!r}")
    folders: list[str] = []
    current = "/"
    pending = [part for part in path.split("/") if part]
    links = 0
    while pending:
        name = pending.pop(0)
        if name == ".":
            continue
        if name == "..":
            current = os.path.dirname(current)
            continue
        if current not in folders:
            folders.append(current)
        candidate = os.path.join(current, name)
        try:
            info = os.lstat(candidate)
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            return folders, None
        if stat.S_ISLNK(info.st_mode):
            links += 1
            if links > MAX_LINKS:
                raise WallError(f"too many symlinks under {path}")
            target = os.readlink(candidate)
            if target.startswith("/"):
                current = "/"
            pending = [part for part in target.split("/") if part] + pending
            continue
        current = candidate
    return folders, current


def path_problems(path: str, writable: Callable[[str], bool] = can_write) -> list[str]:
    """The folders on the way to path, and path itself, that are writable."""
    try:
        folders, final = lookup(path)
    except WallError as error:
        return [f"{path} ({error})"]
    bad = [folder for folder in folders if writable(folder)]
    if final is not None and writable(final):
        bad.append(final)
    return bad


def entries(folder: str) -> list[os.DirEntry] | None:
    """The folder's entries; [] when it does not exist; None when this user cannot enter it.

    A user who cannot enter a folder cannot write anything inside it. Any other
    failure to list it raises OSError, which the callers count as a problem.
    """
    try:
        with os.scandir(folder) as found:
            return list(found)
    except (FileNotFoundError, NotADirectoryError):
        return []
    except PermissionError:
        if os.access(folder, os.X_OK):
            raise
        return None


def tree_problems(root: str, writable: Callable[[str], bool] = can_write) -> list[str]:
    """path_problems(root) plus every writable thing below it (a symlink inside is followed, not walked)."""
    bad = path_problems(root, writable)
    if bad:
        return bad
    _, final = lookup(root)
    if final is None or not os.path.isdir(final):
        return bad
    stack = [final]
    while stack:
        folder = stack.pop()
        try:
            listed = entries(folder)
        except OSError:
            bad.append(f"{folder} (cannot list it)")
            continue
        for entry in listed or []:
            if entry.is_symlink():
                bad += path_problems(entry.path, writable)
                continue
            if writable(entry.path):
                bad.append(entry.path)
            if entry.is_dir(follow_symlinks=False):
                stack.append(entry.path)
    return bad


def check(path_dirs: list[str], ld_dirs: list[str], runs: list[str],
          interpreter: tuple[str, list[str]] | None = None,
          writable: Callable[[str], bool] = can_write) -> list[str]:
    """Every path the scanner user could write that a later step runs or loads (empty: the walls hold).

    interpreter is (executable, import path) of the Python the later steps
    use; main() passes the one running this check.
    """
    bad: list[str] = []
    for run in runs:
        bad += path_problems(run, writable)
    if interpreter is not None:
        executable, import_path = interpreter
        bad += path_problems(executable, writable)
        for entry in import_path:
            bad += tree_problems(entry, writable)
    for folder in path_dirs:
        bad += path_problems(folder, writable)
        try:
            listed = entries(folder)
        except OSError:
            bad.append(f"{folder} (cannot list it)")
            continue
        for entry in listed or []:
            bad += path_problems(entry.path, writable)
    for folder in ld_dirs:
        bad += tree_problems(folder, writable)
    return list(dict.fromkeys(bad))


def _close(path: str, chmod: Callable[[str, int], None]) -> str | None:
    """Remove group and other write from one file or folder. Returns a note on anything it leaves open."""
    try:
        info = os.stat(path)
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)) or not info.st_mode & OPEN_BITS:
            return None
        if stat.S_ISDIR(info.st_mode) and info.st_mode & stat.S_ISVTX:
            return f"{path} is a shared sticky folder; left as it is"
        chmod(path, stat.S_IMODE(info.st_mode) & ~OPEN_BITS)
    except OSError as error:  # a read-only file system, say: check() decides whether it matters
        return f"{path} could not be closed: {error.strerror}"
    return None


def _close_path(path: str, chmod: Callable[[str, int], None], tree: bool = False) -> list[str]:
    try:
        folders, final = lookup(path)
    except WallError as error:  # check() reports it and fails
        return [f"{path} ({error})"]
    notes = [_close(folder, chmod) for folder in folders]
    if final is not None:
        notes.append(_close(final, chmod))
        if tree and os.path.isdir(final):
            for parent, dirs, files in os.walk(final):
                for name in dirs + files:
                    child = os.path.join(parent, name)
                    if os.path.islink(child):
                        notes += _close_path(child, chmod)
                    else:
                        notes.append(_close(child, chmod))
    return [note for note in notes if note]


def lock(path_dirs: list[str], ld_dirs: list[str], chmod: Callable[[str, int], None] = os.chmod) -> list[str]:
    """Make what check() covers unwritable for group and others. Returns notes on what it left."""
    notes: list[str] = []
    for folder in path_dirs:
        notes += _close_path(folder, chmod)
        try:
            listed = entries(folder) or []
        except OSError as error:  # check() reports it and fails
            notes.append(f"{folder} could not be listed: {error.strerror}")
            continue
        for entry in listed:
            notes += _close_path(entry.path, chmod)
    for folder in ld_dirs:
        notes += _close_path(folder, chmod, tree=True)
    return list(dict.fromkeys(notes))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Lock or check what the scanner user must not be able to write.")
    parser.add_argument("command", choices=("lock", "check"))
    parser.add_argument("--path", default="", help="the job's PATH")
    parser.add_argument("--ld-path", default="", help="the job's LD_LIBRARY_PATH, if any")
    parser.add_argument("--run", action="append", default=[], help="a file a later step runs by its full path")
    args = parser.parse_args(argv)
    try:
        path_dirs = list(dict.fromkeys(split_dirs(args.path) + list(SYSTEM_BIN_DIRS)))
        ld_dirs = split_dirs(args.ld_path)
    except WallError as error:
        print(f"walls: {error}", file=sys.stderr)
        return 1
    if args.command == "lock":
        for note in lock(path_dirs, ld_dirs):
            print(f"walls: {note}")
        print(f"walls: closed {len(path_dirs)} command folders and {len(ld_dirs)} library folders to other users")
        return 0
    if not (sys.flags.isolated and sys.flags.no_site):
        # The import path checked must be the one the later steps get.
        print("walls: run check with python3 -I -S, the way the later steps run", file=sys.stderr)
        return 1
    bad = check(path_dirs, ld_dirs, args.run, (sys.executable, [p for p in sys.path if p]))
    if bad:
        print(f"walls: the scanner user can write {len(bad)} path(s) that a later step runs or loads:", file=sys.stderr)
        for path in bad[:SHOW]:
            print(f"  {path}", file=sys.stderr)
        return 1
    print(f"walls: hold for {len(args.run)} programs, the Python import path, "
          f"{len(path_dirs)} command folders and {len(ld_dirs)} library folders")
    return 0


if __name__ == "__main__":
    sys.exit(main())
