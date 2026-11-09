"""Scripted scenario evals. Run `python -m evals.run_evals` from the repo root.

Source modules use flat imports (`from config import Settings`), so `src/` must be
on `sys.path` before anything under `evals` imports them.
"""

from __future__ import annotations

import sys
from pathlib import Path

_SRC = str(Path(__file__).resolve().parent.parent / "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
