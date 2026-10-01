"""The "rip-neon" look: a dark terminal with phosphor-green and cyan, and magenta for warnings.

One palette feeds both the Textual theme and the rich styles the visuals (Strip map, day
timeline) use, so they always match. Colour is never the only signal: every state the visuals
colour also has a glyph (✓ ★ #N ◆ ⚠ ✗ ░), and the colours below keep at least WCAG AA contrast
(4.5:1) against the background.
"""

from __future__ import annotations

from textual.app import App
from textual.theme import Theme

BACKGROUND = "#0b0f14"
SURFACE = "#111821"
PANEL = "#17202b"
FOREGROUND = "#d8f3e6"
GREEN = "#39ff88"  # phosphor: the accent, and "reserved"
CYAN = "#3fd8ff"  # favorites, labels, the monorail
MAGENTA = "#ff5ad8"  # warnings
RED = "#ff5c5c"  # errors, too-tight transfers, overlaps
AMBER = "#ffc94d"  # ranked picks
VIOLET = "#b48cff"  # personal time
MUTED = "#8a9bb0"  # secondary text, the road, ticks

# Rich styles for the visuals, by role. Blocks are dark text on a bright fill.
PALETTE: dict[str, str] = {
    "reserved": f"bold {BACKGROUND} on {GREEN}",
    "ranked": f"bold {BACKGROUND} on {AMBER}",
    "favorite": f"bold {BACKGROUND} on {CYAN}",
    "backup": f"{BACKGROUND} on {MUTED}",
    "personal": f"bold {BACKGROUND} on {VIOLET}",
    "other": f"{BACKGROUND} on {MUTED}",
    "title": f"bold {GREEN}",
    "label": f"bold {CYAN}",
    "muted": MUTED,
    "text": FOREGROUND,
    "ok": GREEN,
    "warning": f"bold {MAGENTA}",
    "tight": f"bold {RED}",
    "road": AMBER,
    "monorail": CYAN,
    "landmark": VIOLET,
    "venue": FOREGROUND,
    "venue.stop": f"bold {GREEN}",
    "venue.idle": MUTED,
    "stop": f"bold {GREEN}",
    "now": f"bold {MAGENTA}",
    "travel": MUTED,
    "highlight": "reverse",
}

RIP_NEON = Theme(
    name="rip-neon",
    primary=GREEN,
    secondary=CYAN,
    accent=MAGENTA,
    warning=MAGENTA,
    error=RED,
    success=GREEN,
    foreground=FOREGROUND,
    background=BACKGROUND,
    surface=SURFACE,
    panel=PANEL,
    dark=True,
    variables={
        "footer-key-foreground": GREEN,
        "block-cursor-foreground": BACKGROUND,
        "block-cursor-background": GREEN,
        "input-selection-background": f"{CYAN} 35%",
    },
)


def register(app: App) -> Theme:
    """Make "rip-neon" available to ``app`` (switch to it with ``app.theme = "rip-neon"``)."""
    app.register_theme(RIP_NEON)
    return RIP_NEON


def contrast_ratio(first: str, second: str) -> float:
    """WCAG 2 contrast ratio between two "#rrggbb" colours (1.0 to 21.0)."""
    lighter, darker = sorted((_luminance(first), _luminance(second)), reverse=True)
    return (lighter + 0.05) / (darker + 0.05)


def _luminance(colour: str) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    hex_digits = colour.lstrip("#")
    r, g, b = (int(hex_digits[i : i + 2], 16) for i in (0, 2, 4))
    return 0.2126 * channel(r) + 0.7152 * channel(g) + 0.0722 * channel(b)
