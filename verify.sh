#!/usr/bin/env bash
# Context Circulator verifier.
#
# Runs the checks that prove the package is intact and runnable on this
# machine, without needing a live model server:
#   1. launcher present and executable
#   2. launcher interpreter present, >= Python 3.10, PyYAML importable
#   3. package imports cleanly (engine, relay, cli, common, pages)
#   4. shipped file hashes match the manifest
#   5. full unit test suite passes (one run)
#
# With --doctor it additionally runs `doctor --profile <p>` (needs a
# prepared profile and, for a full pass, the matching main server up).
#
# Usage:
#   ./verify.sh
#   ./verify.sh --doctor            # also doctor fast + large
#   ./verify.sh --doctor --profile fast
#   ./verify.sh --state-dir DIR
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
LAUNCHER="$SCRIPT_DIR/hermes-circulator"
STATE_DIR="$HOME/.local/state/local-ai-control-centre/rolling-context"
DOCTOR=0
PROFILES=(fast large)

usage() { sed -n '2,17p' "$0"; exit 0; }
while [[ $# -gt 0 ]]; do
  case "$1" in
    --doctor) DOCTOR=1; shift ;;
    --profile) PROFILES=("$2"); shift 2 ;;
    --state-dir) STATE_DIR="$2"; shift 2 ;;
    -h|--help) usage ;;
    *) echo "Unknown argument: $1" >&2; usage ;;
  esac
done

pass=0; fail=0
ok()   { echo "  PASS  $1"; pass=$((pass+1)); }
bad()  { echo "  FAIL  $1"; fail=$((fail+1)); }

echo "== 1. launcher =="
if [[ -x "$LAUNCHER" ]]; then ok "launcher executable: $LAUNCHER"; else bad "launcher missing/not executable: $LAUNCHER"; fi

echo "== 2. interpreter =="
PYBIN="$(head -1 "$LAUNCHER" | sed 's/^#!//')"
if [[ -x "$PYBIN" ]]; then ok "interpreter present: $PYBIN"; else bad "interpreter missing: $PYBIN"; fi
if "$PYBIN" -c 'import sys; sys.exit(0 if sys.version_info >= (3,10) else 1)' 2>/dev/null; then
  ok "interpreter >= 3.10 ($("$PYBIN" --version 2>&1))"
else
  bad "interpreter older than 3.10"
fi
if "$PYBIN" -c 'import yaml' 2>/dev/null; then
  ok "PyYAML importable by $PYBIN"
else
  bad "PyYAML not importable by $PYBIN (prepare/doctor need it)"
fi

echo "== 3. package imports =="
# engine.py imports `agent.context_engine` from the Hermes source tree, so the
# import check must add ~/.hermes/hermes-agent to sys.path (as the tests do).
HERMES_SRC="$HOME/.hermes/hermes-agent"
if [[ -d "$HERMES_SRC" ]]; then
  if (cd "$SCRIPT_DIR" && "$PYBIN" -c \
    "import sys; sys.path.insert(0, '$HERMES_SRC'); \
     import rolling_context.common, rolling_context.pages, rolling_context.engine, rolling_context.relay, rolling_context.cli" \
    2>/dev/null); then
    ok "all modules import (with $HERMES_SRC on path)"
  else
    bad "module import failed"
  fi
else
  echo "  SKIP  engine import (Hermes source tree not found at $HERMES_SRC)"
fi

echo "== 4. package manifest =="
if "$PYBIN" - "$SCRIPT_DIR" <<'PY'
import hashlib
import json
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve()
try:
    files = json.loads((root / "install-manifest.json").read_text())["files"]
    if not isinstance(files, dict) or not files:
        raise ValueError("empty or invalid file manifest")
    for name, expected in files.items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError(f"missing or invalid manifest path: {name}")
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError(f"file hash mismatch: {name}")
except (OSError, ValueError, KeyError, TypeError) as exc:
    print(f"Manifest verification failed: {exc}", file=sys.stderr)
    sys.exit(1)
PY
then
  ok "package file hashes match manifest"
else
  bad "package manifest verification failed"
fi

echo "== 5. unit tests =="
if (cd "$SCRIPT_DIR" && "$PYBIN" -m unittest discover -s tests -q); then
  ok "test suite passed"
else
  bad "test suite failed"
fi

if [[ "$DOCTOR" -eq 1 ]]; then
  echo "== 6. doctor =="
  for p in "${PROFILES[@]}"; do
    if [[ -f "$STATE_DIR/profiles/$p/.rolling-context-profile.json" ]]; then
      if "$LAUNCHER" doctor --profile "$p" --state-dir "$STATE_DIR" >/dev/null 2>&1; then
        ok "doctor $p"
      else
        bad "doctor $p (profile prepared; a live main server may be required)"
      fi
    else
      echo "  SKIP  doctor $p (profile not prepared; run install.sh first)"
    fi
  done
fi

echo
echo "verify: $pass passed, $fail failed"
[[ "$fail" -eq 0 ]] || exit 1
exit 0
