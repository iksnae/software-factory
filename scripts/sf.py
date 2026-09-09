#!/usr/bin/env -S uv run
# /// script
# dependencies = ["pydantic", "python-dotenv", "pyyaml", "rich"]
# ///
"""`sf` entrypoint. A script, so `uv run` supplies the deps with no venv to manage.

The engine is a package (`factory/`), not a script, because its modules import
each other relatively. So this file's only job is to put the factory checkout on
sys.path and hand over — which also means `sf` works from any cwd, against any
repo, with no install step.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from factory.cli import main  # noqa: E402  (path must be set first)

if __name__ == "__main__":
    sys.exit(main())
