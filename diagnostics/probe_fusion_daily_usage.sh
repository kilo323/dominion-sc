#!/usr/bin/env bash
# User-facing launcher for the Fusion daily-usage diagnostic.
set -euo pipefail

script_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
repo_root="$(cd -- "$script_dir/.." && pwd)"
python_script="$script_dir/probe_fusion_daily_usage.py"
venv_path="$repo_root/.venv-fusion-probe"
venv_python="$venv_path/bin/python"

if [[ ! -x "$venv_python" ]]; then
    python_command="${PYTHON_BIN:-}"
    if [[ -z "$python_command" ]]; then
        if command -v python3 >/dev/null 2>&1; then
            python_command="$(command -v python3)"
        elif command -v python >/dev/null 2>&1; then
            python_command="$(command -v python)"
        else
            echo "Python 3 is required. Install Python 3 and try again." >&2
            exit 1
        fi
    fi
    "$python_command" -m venv "$venv_path"
fi

if ! "$venv_python" -c "import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('bs4') and importlib.util.find_spec('requests') else 1)" >/dev/null 2>&1; then
    echo "Setting up the probe's dependencies (only needed the first time)..."
    "$venv_python" -m pip install 'beautifulsoup4>=4.12.3' requests
fi

read -r -p "Dominion username: " dominion_username
read -r -s -p "Dominion password: " dominion_password
printf '\n'

cd "$repo_root"
DOMINIONSC_TEST_USERNAME="$dominion_username" \
DOMINIONSC_TEST_PASSWORD="$dominion_password" \
    "$venv_python" "$python_script" "$@"
