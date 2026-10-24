#!/usr/bin/env bash
# Run the two rules-only scanners as a dedicated unprivileged OS user.
#
#   bash scripts/sandbox.sh install          # scanners and scan code into /opt/skill-scan, owned by root
#   bash scripts/sandbox.sh setup            # create the scanner user
#   bash scripts/sandbox.sh scan ITEMS OUT   # scan ITEMS (items.json) as that user, write OUT
#
# For a GitHub Actions Linux runner, whose runner user has passwordless sudo.
# scan-queue.yml and self-test.yml call it; the workflow first closes the
# runner user's home and $RUNNER_TEMP to other users (chmod 700).
#
# Why: the scanners read untrusted skill text. If a skill could break out of a
# scanner, it runs as SCAN_USER, which:
#   - is created in the job, in no group but its own, with no sudo rights;
#   - has a home folder no other user can read (700);
#   - runs with an empty environment apart from PATH and its work folder
#     (HOME and TMPDIR), so no token and no key reaches it;
#   - cannot open the runner user's home or $RUNNER_TEMP, where the claimed
#     ids, the AI review's results and the runner's own step files live;
#   - reads the claimed skills from one file and writes its results to one file.
# After the scan every process of SCAN_USER is killed, and only then are the
# results copied out, once, as plain bytes (never through a symlink). The post
# step reads them as untrusted input (scripts/handoff.py). If a process of
# SCAN_USER is still left, the scan step fails without the output
# scan_user_stopped, and scan-queue.yml then skips the AI review and the post.
#
# The walls must also hold AFTER the scan: the steps that follow hold the
# token and the model key, and the runner keeps every secret of the job in
# memory. So SCAN_USER must not be able to write anything those steps run or
# load. As far as we know, GitHub's image leaves /opt writable by every user,
# and /opt is on PATH and LD_LIBRARY_PATH (setup-python's Python, /opt/pipx_bin). So:
#   - setup closes every folder on PATH and LD_LIBRARY_PATH to other users
#     before SCAN_USER exists (scripts/walls.py lock);
#   - scan proves, AS SCAN_USER and before it reads a skill, that it can write
#     none of: the steps' shell (STEP_SHELL), root's Python (SYSTEM_PYTHON) and
#     its import path, any folder on PATH or LD_LIBRARY_PATH or above one, any
#     command on PATH, any library in a LD_LIBRARY_PATH folder (walls.py check).
#     If one fails, the scan stops before any skill is handed over;
#   - the workflows run every step by STEP_SHELL's full path, and every Python
#     step after setup with SYSTEM_PYTHON -I -S (no site module, so no .pth
#     file runs); this script finds its own commands only in SAFE_PATH once
#     the scan starts.
#
# The scanner user can still reach the network, and it can read every skill in
# the same run. SECURITY.md states this.

set -euo pipefail

SCAN_USER="skillscan"
SCAN_HOME="/home/$SCAN_USER"
WORK="$SCAN_HOME/work"
OPT="/opt/skill-scan"
# Root's own shell and Python, which every step after the scan runs by these
# full paths (scan-queue.yml and self-test.yml). walls.py proves them.
STEP_SHELL="/usr/bin/bash"
SYSTEM_PYTHON="/usr/bin/python3"
# Where this script finds commands once the scanner user has run.
SAFE_PATH="/usr/sbin:/usr/bin:/sbin:/bin"
# Must equal MAX_FILE_BYTES in scripts/handoff.py.
MAX_RESULT_BYTES=8388608
# The whole batch must end, and the cleanup run, inside the workflow step's own time limit.
BATCH_SECONDS=540

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

fail() {
  echo "sandbox: $*" >&2
  exit 1
}

install_scanners() {
  sudo install -d -o "$(id -u)" -g "$(id -g)" -m 755 "$OPT"
  # An empty GITHUB_ENV keeps the binary paths out of later steps' environments.
  GITHUB_ENV="" bash "$repo_root/scripts/install_scanners.sh" "$OPT/scanners"
  cp -R "$repo_root/scripts" "$OPT/scripts"
  # Root owns everything here, so the scanner user can run it but not change it.
  sudo chown -R root:root "$OPT"
  sudo chmod -R go-w "$OPT"
  echo "Scanners and scan code installed in $OPT, owned by root"
}

setup_user() {
  if id -u "$SCAN_USER" >/dev/null 2>&1; then
    fail "the user $SCAN_USER already exists"
  fi
  # Before the user exists: close what later steps run or load to other users.
  sudo "$SYSTEM_PYTHON" -I -S "$repo_root/scripts/walls.py" lock --path "$PATH" --ld-path "${LD_LIBRARY_PATH:-}"
  # No subordinate uid or gid ranges (so no user namespace can hold other
  # uids that pgrep would miss), and no cron or at jobs that could start
  # after the scan is stopped.
  sudo useradd --create-home --user-group --shell /usr/sbin/nologin \
    -K SUB_UID_COUNT=0 -K SUB_GID_COUNT=0 "$SCAN_USER"
  sudo chmod 700 "$SCAN_HOME"
  if grep -qs "^$SCAN_USER:" /etc/subuid /etc/subgid; then
    fail "$SCAN_USER has subordinate id ranges"
  fi
  echo "$SCAN_USER" | sudo tee -a /etc/cron.deny /etc/at.deny >/dev/null
  local groups listing
  groups="$(id -nG "$SCAN_USER")"
  [ "$groups" = "$SCAN_USER" ] || fail "$SCAN_USER is in more groups than its own: $groups"
  listing="$(sudo -n -l -U "$SCAN_USER" 2>&1 || true)"
  case "$listing" in
    *"may run"*) fail "$SCAN_USER has sudo rights" ;;
  esac
  sudo install -d -o "$SCAN_USER" -g "$SCAN_USER" -m 700 "$WORK"
  echo "Scanner user $SCAN_USER is ready: no sudo rights, no other groups, home closed to other users"
}

as_scan_user() {
  sudo -n -u "$SCAN_USER" -- "$@"
}

# True unless pgrep counts no process with SCAN_USER as its effective or its real
# user id (a set-user-id program it started keeps SCAN_USER as the real one).
# A pgrep that could not run prints no count, which counts as running.
scan_user_running() {
  local by_effective by_real
  by_effective="$(sudo pgrep -c -u "$SCAN_USER" 2>/dev/null || true)"
  by_real="$(sudo pgrep -c -U "$SCAN_USER" 2>/dev/null || true)"
  [ "$by_effective" != "0" ] || [ "$by_real" != "0" ]
}

# Kill every process of SCAN_USER, and fail unless none is left.
# kill -1, sent AS SCAN_USER, signals every process that user may signal while
# the kernel holds its process list, so no process it forks can slip past.
# (pkill reads the list first and signals each process after, so a child forked
# in between survived it.) It is sent again on every try.
stop_scan_user() {
  local tries=0
  while scan_user_running; do
    tries=$((tries + 1))
    [ "$tries" -le 50 ] || fail "processes of $SCAN_USER are still running, so the steps that hold the token and the key will not run"
    as_scan_user kill -s KILL -- -1 >/dev/null 2>&1 || true
    sleep 0.1
  done
}

# 1 from the moment SCAN_USER starts on the skills until stop_scan_user proves
# that none of its processes is left.
scan_user_live=0

# On exit from scan: tell the later steps that no process of SCAN_USER is left.
# The AI review and post steps in scan-queue.yml hold the key and the token, and
# after a failed scan step they run only on this output. It is not written when
# the stop did not finish or the step ended in the middle of the scan.
report_scan_user_stopped() {
  [ "$scan_user_live" -eq 0 ] || return 0
  [ -z "${GITHUB_OUTPUT:-}" ] || echo "scan_user_stopped=true" >> "$GITHUB_OUTPUT"
}

scan_items() {
  local items="$1" out="$2" job_path="$PATH" job_ld="${LD_LIBRARY_PATH:-}" scan_path status=0
  [ -f "$items" ] || fail "no items file at $items"
  [ -z "${LD_PRELOAD:-}${LD_AUDIT:-}" ] || fail "LD_PRELOAD or LD_AUDIT is set, so the walls cannot be proven"
  # From here on, commands come only from root's folders (walls.py proves them).
  PATH="$SAFE_PATH"
  unset LD_LIBRARY_PATH
  scan_path="$OPT/scanners/skill-scanner/bin:$OPT/scanners/betterleaks:/usr/local/bin:/usr/bin:/bin"

  # Prove the walls before the scanner user touches a skill.
  if as_scan_user test -x "$HOME" || as_scan_user test -x "${RUNNER_TEMP:-$HOME}"; then
    fail "$SCAN_USER can open the runner user's home or RUNNER_TEMP; close them with chmod 700 first"
  fi
  as_scan_user test -x "$SYSTEM_PYTHON" || fail "$SCAN_USER cannot run $SYSTEM_PYTHON"
  if sudo test -L "$WORK" || ! sudo test -d "$WORK"; then
    fail "$WORK is not a plain folder"
  fi
  as_scan_user env -i -C "$WORK" "$SYSTEM_PYTHON" -I -S "$OPT/scripts/walls.py" check \
    --path "$job_path" --ld-path "$job_ld" --run "$STEP_SHELL" --run "$SYSTEM_PYTHON" \
    || fail "$SCAN_USER can write something a later step runs or loads (listed above); no skill was handed over"

  sudo install -o "$SCAN_USER" -g "$SCAN_USER" -m 600 -- "$items" "$WORK/items.json"
  scan_user_live=1
  timeout --kill-after=10 "$BATCH_SECONDS" \
    sudo -n -u "$SCAN_USER" -- env -i -C "$WORK" PATH="$scan_path" HOME="$WORK" TMPDIR="$WORK" \
    "$SYSTEM_PYTHON" -I "$OPT/scripts/scan_queue.py" scan --items "$WORK/items.json" --out "$WORK/scanners.json" \
    || status=$?
  # Nothing the scanner user started may outlive the scan or touch the results after this point.
  stop_scan_user
  scan_user_live=0

  umask 077
  rm -f -- "$out"
  local src="$WORK/scanners.json"
  if sudo test ! -L "$WORK" && sudo test ! -L "$src" && sudo test -f "$src"; then
    sudo head -c "$MAX_RESULT_BYTES" -- "$src" > "$out"
  else
    echo "The scanner user left no results file. The post step marks both scanners as did not finish."
  fi
  if [ "$status" -ne 0 ]; then
    echo "The scan ended with status $status. The post step marks what is missing as did not finish."
  fi
}

case "${1:-}" in
  install) install_scanners ;;
  setup) setup_user ;;
  scan)
    [ "$#" -eq 3 ] || fail "usage: sandbox.sh scan ITEMS OUT"
    trap report_scan_user_stopped EXIT
    scan_items "$2" "$3"
    ;;
  *) fail "usage: sandbox.sh install | setup | scan ITEMS OUT" ;;
esac
