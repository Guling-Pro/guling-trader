"""Entry point for PyInstaller-bundled guling-trader.exe.

PyInstaller bundles a script as the top-level `__main__`, so `from .main import run`
inside `src/trader/__main__.py` raises "attempted relative import with no known parent
package". This file lives at the repo root and uses an absolute import that survives
the bundle.

Dev mode (editable install via `pip install -e .`) keeps using `python -m trader` →
hits `src/trader/__main__.py` → relative import works because Python's -m flag sets up
the package context. So we don't change __main__.py.
"""
from trader.main import run

if __name__ == "__main__":
    run()
