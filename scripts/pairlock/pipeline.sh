#!/bin/sh
# calibrated execution: exec calibration (features + model fit reused), comparison, item 19, validation grid
cd /root/troll-poly-bot
S=/tmp/claude-0/-root-troll-poly-bot/0b5739ea-ef03-4944-92af-cebb397ef559/scratchpad
export PAIRLOCK_SCENARIO=calibrated
.venv/bin/python scripts/pairlock/replay.py prepare > $S/pairlock_prepare_cal.txt 2>&1 || exit 1
.venv/bin/python scripts/pairlock/run_compare.py > $S/compare_cal.txt 2>&1
.venv/bin/python scripts/pairlock/adverse_study.py > $S/adverse_cal.txt 2>&1
.venv/bin/python scripts/pairlock/grid.py > $S/grid_cal.txt 2>&1
echo DONE >> $S/grid_cal.txt
