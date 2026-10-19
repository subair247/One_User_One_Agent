#!/usr/bin/env python3
"""Convenience launcher: `python run.py all` == `PYTHONPATH=src python -m oua all`."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))
from oua.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
