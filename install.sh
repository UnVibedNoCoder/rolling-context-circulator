#!/usr/bin/env bash
# Context Circulator installer.
#
# Installs the launcher into ~/.local/bin and (optionally) prepares the
# isolated FAST/LARGE Hermes profiles. It never touches a llama-server,
# model file, or the original Hermes configuration.
#
# Usage:
#   ./install.sh                 # launcher + prepare fast + prepare large
#   ./install.sh --no-prepare    # launcher only
#   ./install.sh --profile fast  # launcher + prepare fast only
#   ./install.sh --profile large # launcher + prepare large only
#   ./install.sh --state-dir DIR # use a non-default state root
#   ./install.sh --copy-secrets  # also copy ~/.hermes/.env into each profile
#
# Idempotent: re-running replaces the launcher symlink and re-prepares
# profiles (existing config.yaml is backed up by `prepare --refresh`).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER_SRC="$SCRIPT_DIR/hermes-circulator"
LAUNCHER_DEST="$HOME/.local/bin/hermes-circulator"
STATE_DIR="$HOME/.local/state/local-ai-control-centre/rolling-context"
PREPARE=1
PROFILES=(fast large)
COPY_SECRETS=0

usage() { sed -n '2,16p' "$0"; exit 0; }

while [[ $# -gt 0 ]]; do
  case "$1" in
    --no-prepare) PREPARE=0; shift ;;
    --profile) PROFILES=("$2"); shift 2 ;;
    --state-dir) STATE_DIR="$2"; shift 2 ;;
    --copy-secrets) COPY_SECRETS=1; shift ;;
    -h|--help) usage ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done

# --- preflight -------------------------------------------------------------
[[ -f "$LAUNCHER_SRC" ]] || { echo "error: launcher not found at $LAUNCHER_SRC" >&2; exit 1; }
[[ -x "$LAUNCHER_SRC" ]] || { echo "error: launcher is not executable: $LAUNCHER_SRC" >&2; exit 1; }

# The launcher shebang is /usr/bin/python3; confirm it exists and is new enough.
PYBIN="$(head -1 "$LAUNCHER_SRC" | sed 's/^#!//')"
[[ -x "$PYBIN" ]] || { echo "error: launcher interpreter not found: $PYBIN" >&2; exit 1; }
"$PYBIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' \
  || { echo "error: $PYBIN is older than Python 3.10" >&2; exit 1; }
"$PYBIN" -c 'import yaml' 2>/dev/null \
  || { echo "error: PyYAML is not importable by $PYBIN (needed for prepare/doctor)." >&2;
       echo "       Install it for that interpreter, e.g.: $PYBIN -m pip install --user pyyaml" >&2;
       exit 1; }

# --- install launcher ------------------------------------------------------
mkdir -p "$(dirname "$LAUNCHER_DEST")"
if [[ -L "$LAUNCHER_DEST" ]]; then
  current="$(readlink "$LAUNCHER_DEST")"
  if [[ "$current" != "$LAUNCHER_SRC" ]]; then
    echo "replacing existing launcher symlink: $current"
  fi
fi
ln -sfn "$LAUNCHER_SRC" "$LAUNCHER_DEST"
echo "launcher: $LAUNCHER_DEST -> $LAUNCHER_SRC"

# --- prepare profiles ------------------------------------------------------
if [[ "$PREPARE" -eq 1 ]]; then
  for p in "${PROFILES[@]}"; do
    echo "preparing profile: $p"
    args=(prepare --profile "$p" --state-dir "$STATE_DIR")
    [[ "$COPY_SECRETS" -eq 1 ]] && args+=(--copy-secrets)
    "$LAUNCHER_DEST" "${args[@]}"
  done
fi

echo
echo "Installed. Next steps:"
echo "  $LAUNCHER_DEST doctor --profile fast"
echo "  $LAUNCHER_DEST run --profile fast"
echo "State root: $STATE_DIR"
