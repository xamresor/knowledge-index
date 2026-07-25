"""Shared test bootstrap: import a `bin/` script as a module.

The scripts in `bin/` are executables, not an installed package: at runtime Python puts the script's
own directory on `sys.path[0]`, which is why `import graph` works there. Tests run from elsewhere, so
they need the same directory on the path — done once here instead of re-deriving importlib
boilerplate in every test file.
"""
from __future__ import annotations

import importlib.util
import os
import sys
from types import ModuleType

HERE = os.path.dirname(os.path.abspath(__file__))
BIN = os.path.abspath(os.path.join(HERE, "..", "bin"))

if BIN not in sys.path:
    sys.path.insert(0, BIN)


def load_script(filename: str, module_name: str | None = None) -> ModuleType:
    """Load `bin/<filename>` (with or without a .py suffix) as an importable module."""
    path = os.path.join(BIN, filename)
    name = module_name or os.path.splitext(os.path.basename(filename))[0].replace("-", "_")
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module
