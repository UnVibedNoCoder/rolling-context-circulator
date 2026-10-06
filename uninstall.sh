#!/usr/bin/env bash
# Context Circulator uninstaller.
#
# Removes the launcher symlink (only if it points at this package) and,
# with --remove-state, the isolated state root. It NEVER touches a
# llama-server, model file, or the original ~/.hermes configuration.
#
# Usage:
#   ./uninstall.sh                 # remove launcher symlink only
#   ./uninstall.sh --remove-state  # also remove the state root (data!)
#   ./uninstall.sh --state-dir DIR # target a non-default state root
#
# The state root holds the durable memory database, telemetry, and the
# prepared profiles. Removing it deletes that data. Back it up first if
# you want to keep it.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER_DEST="$HOME/.local/bin/hermes-circulator"
STATE_DIR="$HOME/.local/state/local-ai-control-centre/rolling-context"
REMOVE_STATE=0

usage() { sed -n '2,15p' "$0"; exit 0; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --remove-state) REMOVE_STATE=1; shift ;;
    --state-dir) STATE_DIR="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done

# --- launcher --------------------------------------------------------------
if [[ -L "$LAUNCHER_DEST" ]]; then
  target="$(readlink "$LAUNCHER_DEST")"
  if [[ "$target" == "$SCRIPT_DIR/hermes-circulator" ]]; then
    rm -f "$LAUNCHER_DEST"
    echo "removed launcher symlink: $LAUNCHER_DEST"
  else
    echo "launcher symlink points elsewhere; left untouched: $target"
  fi
elif [[ -e "$LAUNCHER_DEST" ]]; then
  echo "launcher is a regular file, not this package's symlink; left untouched: $LAUNCHER_DEST"
else
  echo "no launcher symlink at $LAUNCHER_DEST"
fi

# --- state root ------------------------------------------------------------
if [[ "$REMOVE_STATE" -eq 1 ]]; then
  if [[ -e "$STATE_DIR" ]]; then
    echo
    echo "About to remove the state root (durable memory + telemetry + profiles):"
    echo "  $STATE_DIR"
    read -r -p "Type 'yes' to confirm deletion: " confirm
    if [[ "$confirm" == "yes" ]]; then
      rm -rf "$STATE_DIR"
      echo "removed state root: $STATE_DIR"
    else
      echo "aborted; state root kept."
    fi
  else
    echo "state root not present: $STATE_DIR"
  fi
fi

echo
echo "Uninstall complete. No llama-server, model file, or ~/.hermes config was touched."
