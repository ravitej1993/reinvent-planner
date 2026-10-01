#!/bin/bash
# Double-click in Finder to open re:Invent Planner in your browser (this runs
# `reinvent-planner ui`, the same as `rip ui`).
# Stop it with Ctrl+C, or by closing this Terminal window.
#
# This deliberately calls `reinvent-planner`, not the short `rip`: other tools install a `rip`
# too (rm-improved's `rip` deletes files), and a double-click must never run the wrong one.

# Add the usual install locations (uv tool, Homebrew) in case the login shell doesn't.
export PATH="$PATH:$HOME/.local/bin:/opt/homebrew/bin:/usr/local/bin"

if ! command -v reinvent-planner >/dev/null 2>&1; then
  echo "re:Invent Planner isn't installed: the 'reinvent-planner' command wasn't found." >&2
  echo "Install it with uv (see the README), for example:" >&2
  echo "  uv tool install reinvent-planner" >&2
  echo >&2
  read -r -p "Press Return to close this window. " _
  exit 1
fi

exec reinvent-planner ui "$@"
