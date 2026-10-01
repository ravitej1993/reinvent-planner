# The README's demo GIF, recorded with vhs (https://github.com/charmbracelet/vhs):
#
#     vhs scripts/demo/demo.tape
#
# It uses a made-up week in fresh temp folders (scripts/demo/seed_demo.py), never your real data,
# and it makes no network calls: the Launch tab is shown in website-copilot mode.

Output docs/demo.gif

Set Shell "bash"
Set FontSize 15
Set Width 1400
Set Height 820
Set Padding 12
Set TypingSpeed 60ms

Hide
# Fresh, empty scratch folders each time: never your real data, and nothing to delete.
# If anything here fails, the shell exits and the recording stops rather than show real data.
Type 'export RIP_DATA_DIR="$(mktemp -d)" RIP_CONFIG_DIR="$(mktemp -d)" RIP_TOKEN_STORE=file && test -d "$RIP_DATA_DIR" && test -d "$RIP_CONFIG_DIR" && uv run python scripts/demo/seed_demo.py && clear || exit 1'
Enter
Sleep 3s
Show

Type "uv run rip --event reinvent2026 tui"
Enter
Sleep 3s

# Search, then Escape back to the one-key shortcuts
Type "/"
Type "agents"
Sleep 1.5s
Escape
Sleep 500ms

# The day: timeline and Strip map
Type "p"
Sleep 4.5s

# Reservation day: the website copilot counts down and walks the picks (no network)
Type "l"
Sleep 800ms
Type "2026-10-06 10:00"
Sleep 500ms
Tab
Enter
Sleep 300ms
Down
Enter
Sleep 300ms
Tab
Enter
Sleep 2.5s
Type "f"
Sleep 1.2s
Type "b"
Sleep 3s

Type "q"
Sleep 1s
