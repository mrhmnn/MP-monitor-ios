"""Make the MP Agent modules importable from the tests directory.

The project deliberately has no package structure (flat scripts in a
folder with a space in its name), so the straightforward fix is putting
that folder on sys.path before the tests import anything.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
