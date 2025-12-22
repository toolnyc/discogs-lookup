#!/usr/bin/env bash
#
# Discogs Enrichment Script Runner
# Creates/activates venv, installs dependencies, and runs the script.
#

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
PYTHON="${VENV_DIR}/bin/python"
PIP="${VENV_DIR}/bin/pip"

# Create venv if it doesn't exist
if [ ! -d "$VENV_DIR" ]; then
    echo "Creating virtual environment..."
    python3 -m venv "$VENV_DIR"

    echo "Installing dependencies..."
    "$PIP" install --upgrade pip
    "$PIP" install python3-discogs-client mutagen pyyaml

    echo "Setup complete."
    echo ""
fi

# Run the script with all passed arguments
exec "$PYTHON" "$SCRIPT_DIR/discogs_enrich.py" "$@"
