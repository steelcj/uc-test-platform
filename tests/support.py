"""Shared scaffolding for the uc-test-platform tests: lib/ first on the
import path, so the tests run from a checkout without installing."""

import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
LIB = REPO / "lib"
if str(LIB) not in sys.path:
    sys.path.insert(0, str(LIB))
