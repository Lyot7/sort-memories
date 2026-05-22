"""Sort Memories — desktop entry point.

Launches the Flask backend on 127.0.0.1:7777 in a background thread, then
opens a native WKWebView window pointing at it. This is the production
entry point invoked by the PyInstaller-bundled .app.

For dev work on the Flask UI alone (without the desktop shell), run
`python -m sort_memories.core` directly and open http://127.0.0.1:7777 in a
browser.

Status: stub. Wiring happens in Phase 3 (cf. plan).
"""
from __future__ import annotations

import sys


def main() -> int:
    print("Sort Memories — stub entry point.")
    print("Phase 1 scaffolding only. Run `python -m sort_memories.core` for the Flask UI.")
    print("Phase 3 will wire pywebview around the Flask backend here.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
