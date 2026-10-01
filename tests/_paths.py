"""Path bootstrap for the test scripts.

Every test inserts the repo's `src/` onto sys.path and runs with the repo root
as the working directory, so `python tests/test_llm.py` behaves identically
whether it is launched from the repo root, from inside `tests/`, or from an
editor. Import this module before importing anything from `accelerateddevops`.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# tests/_paths.py -> tests/ -> repo root
REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = REPO_ROOT / "src"

if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

# Several tests write scratch state under `data/`, so anchor relative paths to
# the repo root rather than to whatever directory the script was launched from.
os.chdir(REPO_ROOT)
