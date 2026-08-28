#!/usr/bin/env python3
"""Run one Python script with the deterministic eval network guard installed."""

from __future__ import annotations

import runpy
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT_DIR = Path(sys.argv[1]).resolve()
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(SCRIPT_DIR.parent))

from tools.weex_eval_offline_guard import install_network_block  # noqa: E402


install_network_block()
sys.argv = [str(SCRIPT_DIR), *sys.argv[2:]]
runpy.run_path(str(SCRIPT_DIR), run_name="__main__")
