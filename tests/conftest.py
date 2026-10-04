"""Pytest bootstrap for the kitebase backend test suite.

kitebase is used as a source checkout (not pip-installed), so the package is
importable only when the repo root is on sys.path. The repo root is the parent
of this tests/ directory and contains the `kitebase/` package.
"""
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
