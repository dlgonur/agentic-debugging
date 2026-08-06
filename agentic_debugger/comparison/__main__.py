"""``python -m agentic_debugger.comparison`` entry point."""

from __future__ import annotations

import sys

from agentic_debugger.comparison.cli import main

if __name__ == "__main__":
    sys.exit(main())
