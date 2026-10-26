"""Thin delegator so ``python -m moat_install`` works from a source checkout.

Uses an absolute import (not ``from .cli import …``) so the file also works
as a PyInstaller entry script: PyInstaller executes it as a top-level script
with no package context, under which a relative import would fail.
"""

from __future__ import annotations

import sys

from moat_install.cli import main

if __name__ == "__main__":
    sys.exit(main())
