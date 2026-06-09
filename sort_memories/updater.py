"""Auto-updater Sort Memories — check GitHub Releases + install in-place.

Stratégie :
- Check : GitHub Releases API → compare version courante vs latest
- Download : zip release dans /tmp, progress callback via thread
- Install : génère un script bash relauncher qui swap le .app et relance

Pas de Sparkle (overhead Objective-C/Swift pour app Python) — custom léger,
mais robuste : HTTPS strict, vérification format release, swap atomique via ditto.
"""
import json
import os
import re
import ssl
import stat
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from sort_memories import __version__ as CURRENT_VERSION

GITHUB_REPO        = "Lyot7/sort-memories"
GITHUB_API_LATEST  = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
USER_AGENT         = f"SortMemories/{CURRENT_VERSION} (auto-updater)"
HTTP_TIMEOUT       = 10  # secondes


# ── Contexte SSL ─────────────────────────────────────────────────────────────


def _ssl_context() -> ssl.SSLContext:
    """Contexte SSL avec bundle CA explicite via certifi.

    Dans un bundle PyInstaller figé, `ssl.create_default_context()` ne trouve
    aucun certificat racine (les chemins OpenSSL par défaut n'existent pas dans
    le .app) → `CERTIFICATE_VERIFY_FAILED`. certifi embarque cacert.pem, que
    PyInstaller bundle automatiquement, donc on pointe le contexte dessus.
    Fallback sur le contexte par défaut si certifi est absent (dev hors bundle).
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        return ssl.create_default_context()


# ── Version compare ──────────────────────────────────────────────────────────

_SEMVER_RE = re.compile(r"^v?(\d+)\.(\d+)\.(\d+)(?:[-+].*)?$")


def parse_version(s: str) -> tuple[int, int, int] | None:
    """Parse 'v0.5.0' ou '0.5.0' → (0, 5, 0). None si format invalide."""
    if not s:
        return None
    m = _SEMVER_RE.match(s.strip())
    if not m:
        return None
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)))


def is_newer(latest: str, current: str) -> bool:
    """True si latest > current selon semver strict. False sinon (incl. parsing fail)."""
    lp, cp = parse_version(latest), parse_version(current)
    if lp is None or cp is None:
        return False
    return lp > cp


# ── GitHub Releases API ──────────────────────────────────────────────────────


def check_latest() -> dict:
    """Interroge l'API GitHub Releases. Retourne un dict normalisé.

    {
      'ok': bool,
      'current': str,          # version courante (sans 'v')
      'latest': str | None,    # tag de la dernière release (avec 'v')
      'available': bool,       # True si une mise à jour est dispo
      'url': str | None,       # URL directe du zip macOS
      'size': int | None,      # taille en octets de l'asset
      'notes': str,            # release notes (markdown)
      'published_at': str,     # ISO timestamp
      'error': str | None,     # message d'erreur si ok=False
    }
    """
    result = {
        "ok": False, "current": CURRENT_VERSION, "latest": None, "available": False,
        "url": None, "size": None, "notes": "", "published_at": "", "error": None,
    }
    try:
        req = urllib.request.Request(GITHUB_API_LATEST, headers={
            "User-Agent": USER_AGENT,
            "Accept": "application/vnd.github+json",
        })
        ctx = _ssl_context()
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        result["error"] = f"GitHub API: HTTP {e.code}"
        return result
    except urllib.error.URLError as e:
        result["error"] = f"Connexion impossible: {e.reason}"
        return result
    except Exception as e:
        result["error"] = f"Erreur API: {str(e)[:120]}"
        return result

    tag = data.get("tag_name", "")
    result["latest"]       = tag
    result["notes"]        = data.get("body", "") or ""
    result["published_at"] = data.get("published_at", "")

    # Trouver l'asset macOS (.zip)
    for asset in data.get("assets", []):
        name = asset.get("name", "")
        if name.endswith(".zip") and "macos" in name.lower():
            result["url"]  = asset.get("browser_download_url")
            result["size"] = asset.get("size")
            break

    result["available"] = is_newer(tag, CURRENT_VERSION) and result["url"] is not None
    result["ok"]        = True
    return result


# ── Download avec progress ──────────────────────────────────────────────────


def download_release(url: str, dest_path: Path, progress_cb=None) -> None:
    """Télécharge le zip dans dest_path. progress_cb(bytes_done, total) appelé périodiquement."""
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    ctx = _ssl_context()
    with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT, context=ctx) as resp:
        total = int(resp.headers.get("Content-Length", 0) or 0)
        done  = 0
        chunk = 64 * 1024
        with open(dest_path, "wb") as f:
            while True:
                buf = resp.read(chunk)
                if not buf:
                    break
                f.write(buf)
                done += len(buf)
                if progress_cb:
                    progress_cb(done, total)


# ── Détection du chemin du .app courant ─────────────────────────────────────


def current_app_bundle() -> Path | None:
    """Retourne le chemin du Sort Memories.app courant si lancé depuis un bundle, sinon None.

    Detection : sys.executable est dans .../Sort Memories.app/Contents/MacOS/SortMemories
    """
    exe = Path(sys.executable).resolve()
    for parent in exe.parents:
        if parent.suffix == ".app" and parent.name == "Sort Memories.app":
            return parent
    return None


# ── Install : script bash relauncher + sys.exit ─────────────────────────────


_RELAUNCH_TEMPLATE = """#!/bin/bash
# Auto-generated by Sort Memories updater. Safe to delete after install.
set -e
sleep 2  # laisser l'app meurir

NEW_APP="{new_app}"
TARGET="{target}"

# Si target existe, l'envoyer à la corbeille (réversible)
if [ -d "$TARGET" ]; then
  if command -v trash >/dev/null 2>&1; then
    trash "$TARGET" 2>/dev/null || true
  else
    # fallback : déplacer vers ~/.Trash manuellement
    mv "$TARGET" "$HOME/.Trash/Sort Memories.app.old-$(date +%s)" 2>/dev/null || true
  fi
fi

# Copie atomique du nouveau .app (ditto préserve symlinks + permissions)
ditto "$NEW_APP" "$TARGET"

# Garde-fou : garantir le bit exécutable sur le binaire principal
chmod +x "$TARGET/Contents/MacOS/"* 2>/dev/null || true

# Relance
open -a "$TARGET"

# Nettoie l'extract temp (corbeille si dispo, sinon mv vers ~/.Trash)
TMP_EXTRACT="{tmp_extract}"
if [ -d "$TMP_EXTRACT" ]; then
  if command -v trash >/dev/null 2>&1; then
    trash "$TMP_EXTRACT" 2>/dev/null || true
  else
    mv "$TMP_EXTRACT" "$HOME/.Trash/sortmem-extract-$(date +%s)" 2>/dev/null || true
  fi
fi
"""


def install_release(zip_path: Path, on_ready=None) -> None:
    """Décompresse zip, génère script relauncher, l'exécute en background, exit l'app.

    on_ready() est appelé juste avant sys.exit (pour notifier l'UI / shutdown Flask propre).
    """
    target_app = current_app_bundle()
    if target_app is None:
        raise RuntimeError(
            "Impossible de localiser le bundle .app courant. "
            "L'auto-update fonctionne uniquement depuis l'app installée (pas en dev)."
        )

    # Extract dans /tmp via `ditto -x -k` (PAS zipfile.extractall : ce dernier
    # APLATIT les symlinks en fichiers et PERD le bit exécutable → bundle .app
    # corrompu, "Impossible d'ouvrir l'application". ditto préserve symlinks,
    # permissions et resource forks, exactement comme `ditto -c -k` à la création.
    extract_dir = Path(tempfile.mkdtemp(prefix="sortmem-update-"))
    r = subprocess.run(
        ["ditto", "-x", "-k", str(zip_path), str(extract_dir)],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"Échec extraction (ditto): {(r.stderr or '')[:200]}")

    # Localiser le .app dans l'extract
    new_app = None
    for p in extract_dir.rglob("Sort Memories.app"):
        if p.is_dir():
            new_app = p
            break
    if new_app is None:
        raise RuntimeError("Le zip téléchargé ne contient pas Sort Memories.app à la racine.")

    # Garde-fou : le binaire doit exister ET être exécutable, sinon l'app ne
    # s'ouvrira pas. Si ditto a fait son travail c'est déjà bon ; on force par
    # sécurité (défense en profondeur).
    main_exe = new_app / "Contents" / "MacOS" / "SortMemories"
    if not main_exe.exists():
        raise RuntimeError("Bundle téléchargé invalide : binaire principal absent.")
    try:
        main_exe.chmod(main_exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    except Exception:
        pass

    # Génère le script relauncher
    script_path = extract_dir / "_relauncher.sh"
    script_path.write_text(_RELAUNCH_TEMPLATE.format(
        new_app=str(new_app),
        target=str(target_app),
        tmp_extract=str(extract_dir),
    ))
    script_path.chmod(script_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP)

    # Démarre le script en background, détaché du parent
    subprocess.Popen(
        ["/bin/bash", str(script_path)],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,  # détache de la session courante
    )

    if on_ready:
        try:
            on_ready()
        except Exception:
            pass

    # Quitte l'app — le script relauncher est déjà parti et va attendre 2s
    os._exit(0)
