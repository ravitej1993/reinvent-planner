"""Plan your AWS re:Invent schedule with the official AWS Events API."""

__version__ = "0.1.0"

import re

DEFAULT_EVENT_ID = "reinvent2026"
# What an event ID may look like. It ends up in file names and a command line, so no path
# separators and no leading dot.
EVENT_ID_PATTERN = re.compile(r"[A-Za-z0-9_-][A-Za-z0-9._-]{0,127}")
