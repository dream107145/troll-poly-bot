"""Session-wide guards: tests can never touch the real data files.

The real earnings file is append-only and never reset; a test that wrote a
fixture settlement into it once made the Earnings page show 265 fake rows
(slug ``btc-updown-5m-1``). Two layers make that impossible now:

1. **The whole session runs in a temporary working directory.** Every data
   path in the bot is relative (``data/earnings.jsonl``, ``data/KILL``,
   ``data/controls.json``, the tagged ``data/*.momentum.jsonl`` ...), so a
   test that forgets to pass a ``tmp_path`` writes into the temporary tree,
   never into the repository's ``data/``. It also means an operator's real
   ``data/KILL`` cannot change what a test sees.
2. **The real files are checked anyway**, by absolute path: every
   ``data/*.jsonl`` (tagged ones included) is scanned for fixture-shaped
   rows (a slug without a real 10-digit epoch) before and after the session.
   Counting rows rather than bytes keeps the check honest while live bots
   append to the same files in the background.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
REAL_DIR = REPO / "data"
REAL_SLUG = re.compile(r"-\d{10}$")


def _real_files() -> list[Path]:
    return sorted(REAL_DIR.glob("*.jsonl")) if REAL_DIR.exists() else []


def _fixture_rows(p: Path) -> int:
    if not p.exists():
        return 0
    n = 0
    try:
        # streamed: data/decisions.jsonl is 1.5 GB, and reading it whole (plus splitlines) peaked above 3 GB --
        # the OOM killer took the test session on 2026-10-02 when the box had less free memory
        with p.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if '"slug"' not in line and '"market"' not in line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                slug = row.get("slug") or row.get("market")
                if slug and not REAL_SLUG.search(str(slug)):
                    n += 1
    except OSError:
        return 0
    return n


@pytest.fixture(scope="session", autouse=True)
def real_data_files_are_never_touched(tmp_path_factory):
    before = {p: _fixture_rows(p) for p in _real_files()}
    workdir = tmp_path_factory.mktemp("cwd")
    old = os.getcwd()
    os.chdir(workdir)
    try:
        yield
    finally:
        os.chdir(old)
    after = {p: _fixture_rows(p) for p in _real_files()}
    touched = [f"{p} (+{after[p] - before.get(p, 0)} fixture rows)" for p in after if after[p] > before.get(p, 0)]
    assert not touched, f"tests wrote fixture data into real files: {touched}"
    assert not (REAL_DIR / "KILL").exists() or (REAL_DIR / "KILL").stat().st_mtime < _session_start, \
        "a test created the real kill file"


_session_start = __import__("time").time()

