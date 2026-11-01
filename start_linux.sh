#!/usr/bin/env bash
# Startet das CAN-Tool unter Linux (legt beim ersten Start eine virtuelle Umgebung an).
cd "$(dirname "$0")" || exit 1
if [ ! -d .venv ]; then
    python3 -m venv .venv || exit 1
    . .venv/bin/activate
    python -m pip install --upgrade pip
    pip install -r requirements.txt
else
    . .venv/bin/activate
fi
exec python main.py "$@"
