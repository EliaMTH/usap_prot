"""
Make ``import usap`` work when these scripts run from a checkout.

The package lives in ``src/usap``, so ``src`` has to be on ``sys.path``.
Installing the project (``pip install -e .``) puts it there permanently, and
``pyproject.toml``'s ``pythonpath = ["src"]`` puts it there for pytest -- but
neither covers ``python examples/whatever.py`` from a fresh clone, which is
the first thing anyone tries.

Importing this module first fixes that. It is a no-op when usap is already
importable, so an installed environment is unaffected and the installed copy
always wins.

    import _bootstrap  # noqa: F401

``examples/`` is sys.path[0] while one of its scripts is running, so the bare
import resolves without any path juggling of its own.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

if importlib.util.find_spec("usap") is None:
    _src = Path(__file__).resolve().parent.parent / "src"

    if (_src / "usap").is_dir():
        sys.path.insert(0, str(_src))
