"""`python -m reinvent_planner.tui [EVENT_ID]`: what `rip tui` runs, and what `rip ui` serves."""

import sys

from .. import DEFAULT_EVENT_ID
from .app import run

if __name__ == "__main__":
    run(sys.argv[1] if len(sys.argv) > 1 else DEFAULT_EVENT_ID)
