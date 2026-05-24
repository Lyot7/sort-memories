"""Sort Memories — desktop entry point (v0.2.0).

Workflow :
1. Lance le serveur Flask (sort_memories.core) en thread daemon.
2. Ouvre une fenêtre WKWebView native sur http://127.0.0.1:7777.
3. La vue d'accueil de l'UI Flask gère la sélection des dossiers (via /api/pick_folder
   qui appelle pywebview.create_file_dialog en réponse à un clic utilisateur).
4. Fenêtre fermée → arrêt propre du processus.

Dev :
- python app.py                                    → app complète (welcome → triage)
- SORT_MEMORIES_MEDIA_DIR=/path python app.py      → bypass welcome, démarre direct
- python -m sort_memories.core                     → Flask seul (UI dans navigateur)
"""
from __future__ import annotations

import socket
import sys
import threading
import time

import webview


PORT = 7777
URL  = f"http://127.0.0.1:{PORT}"


def _wait_for_server(timeout: float = 15.0) -> bool:
    """Bloque jusqu'à ce que Flask écoute sur PORT, ou timeout."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.3)
            try:
                s.connect(("127.0.0.1", PORT))
                return True
            except OSError:
                time.sleep(0.2)
    return False


def main() -> int:
    # Import APRÈS la mise en place de l'env (core lit les vars au chargement).
    from sort_memories import core

    server_thread = threading.Thread(
        target=lambda: core.run_server(port=PORT, open_browser=False),
        daemon=True,
    )
    server_thread.start()

    if not _wait_for_server():
        print(f"Erreur : le serveur Flask n'a pas démarré sur {URL}", file=sys.stderr)
        return 1

    main_window = webview.create_window(
        "Sort Memories",
        URL,
        width=1280,
        height=820,
        min_size=(800, 600),
    )

    # Expose la window à core pour que /api/pick_folder puisse appeler create_file_dialog.
    core.set_main_window(main_window)

    webview.start(gui="cocoa" if sys.platform == "darwin" else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
