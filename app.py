"""Sort Memories — desktop entry point.

Workflow :
1. Affiche un dialog macOS natif pour sélectionner le dossier à trier.
2. Lance le serveur Flask (sort_memories.core) en thread, pointé sur ce dossier.
3. Ouvre une fenêtre WKWebView native sur http://127.0.0.1:7777.
4. La fenêtre fermée → arrêt propre du processus.

Pour le dev de la UI Flask seule (sans le shell desktop) :
    SORT_MEMORIES_MEDIA_DIR=/path/to/folder python -m sort_memories.core
"""
from __future__ import annotations

import os
import socket
import sys
import threading
import time
from pathlib import Path

import webview


PORT = 7777
URL = f"http://127.0.0.1:{PORT}"


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


def _pick_folder() -> Path | None:
    """Affiche un dialog FOLDER macOS via pywebview (sans fenêtre principale)."""
    chooser = webview.create_window(
        "Sort Memories — Sélectionner un dossier",
        html="<html><body style='background:#0a0a0a;color:#fff;font-family:-apple-system,sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;margin:0'><div style='text-align:center'><h2 style='margin:0 0 12px;font-weight:500'>Sort Memories</h2><p style='opacity:.6;margin:0'>Choisissez le dossier à trier…</p></div></body></html>",
        width=420,
        height=240,
        resizable=False,
    )
    selected: list[str] = []

    def _run(window):
        result = window.create_file_dialog(
            webview.FileDialog.FOLDER if hasattr(webview, "FileDialog") else webview.FOLDER_DIALOG,
            allow_multiple=False,
            directory=str(Path.home() / "Pictures"),
        )
        if result:
            selected.extend(result)
        window.destroy()

    webview.start(_run, chooser)
    return Path(selected[0]) if selected else None


def main() -> int:
    # Skip le picker si SORT_MEMORIES_MEDIA_DIR est déjà défini et pointe sur un dossier valide.
    preset = os.environ.get("SORT_MEMORIES_MEDIA_DIR")
    if preset and Path(preset).expanduser().is_dir():
        media_dir = Path(preset).expanduser()
    else:
        media_dir = _pick_folder()
        if media_dir is None:
            print("Aucun dossier sélectionné. Sortie.")
            return 0

    os.environ["SORT_MEMORIES_MEDIA_DIR"] = str(media_dir)

    # Import APRÈS avoir fixé l'env var (le module lit MEDIA_DIR au chargement).
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
    webview.start(gui="cocoa" if sys.platform == "darwin" else None)
    return 0


if __name__ == "__main__":
    sys.exit(main())
