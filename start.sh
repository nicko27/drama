#!/bin/sh
set -eu
cd "$(dirname "$0")"
if ! command -v ffmpeg >/dev/null 2>&1 || ! command -v ffprobe >/dev/null 2>&1; then
    echo "FFmpeg requis : brew install ffmpeg" >&2
    exit 1
fi
if [ ! -d .venv ]; then
    if command -v uv >/dev/null 2>&1; then
        uv venv --python 3.12 .venv
    else
        python3 -m venv .venv
    fi
fi
if ! .venv/bin/python -c 'import PySide6,yt_dlp' >/dev/null 2>&1; then
    .venv/bin/python -m pip install -r requirements.txt 2>/dev/null || {
        if command -v uv >/dev/null 2>&1; then uv pip install --python .venv/bin/python -r requirements.txt; else exit 1; fi
    }
fi
PATH="$PWD/.venv/bin:$PATH"
export PATH
exec .venv/bin/python app.py
