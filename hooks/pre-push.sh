#!/bin/sh
# ACENLY Bench — pre-push git hook
# Installed by: python3 bench.py --install-hooks
# Removed by:   python3 bench.py --uninstall-hooks

SCRIPT_DIR="$(cd "$(dirname "$0")/../../.." && pwd)"
python3 "$SCRIPT_DIR/bench.py" --hook-mode
