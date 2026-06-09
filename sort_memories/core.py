#!/usr/bin/env python3
"""
Sort Memories — local media triage and dedupe (Flask backend).

Raccourcis : → Garder | ← Retour | D/Suppr Supprimer | O Toggle overlay
Déduplication : scan pHash au démarrage, groupes N-voies côte-à-côte
Recherche sémantique : CLIP ViT-L/14 (optionnel, si torch + open_clip installés)

Configuration (env vars) :
- SORT_MEMORIES_MEDIA_DIR : dossier source à trier (obligatoire en production)
- SORT_MEMORIES_STATE_DIR : override du répertoire d'état (par défaut appdirs)
"""
import datetime as _datetime
import json
import os
import re as _re
import shutil
import subprocess
import tempfile
import threading
import webbrowser
from collections import defaultdict
from itertools import combinations
from pathlib import Path

import imagehash
from flask import Flask, jsonify, render_template_string, request, send_file
from PIL import Image

# HEIC/HEIF/AVIF — enregistre les openers Pillow (iPhone, format par défaut depuis
# iOS 11). Sans ça, Image.open échoue sur .heic et l'écriture HEIC est impossible.
# AVIF est géré nativement par Pillow 12 si dispo, sinon via register_avif_opener.
# HEIF_AVAILABLE est lu par _save_image_in_place (rotate/crop).
try:
    from pillow_heif import register_heif_opener
    register_heif_opener()
    HEIF_AVAILABLE = True
except Exception:
    HEIF_AVAILABLE = False
try:
    from pillow_heif import register_avif_opener
    register_avif_opener()
except Exception:
    pass  # AVIF couvert nativement par Pillow >= 12 si compilé avec libavif

# RAW (libraw) — décodage des fichiers appareil photo (DNG/CR2/NEF/ARW…).
try:
    import rawpy as _rawpy
    RAW_AVAILABLE = True
except Exception:
    RAW_AVAILABLE = False

# exifread — lecture EXIF robuste, fallback pour RAW/TIFF quand getexif() échoue.
try:
    import exifread as _exifread
    EXIFREAD_AVAILABLE = True
except Exception:
    EXIFREAD_AVAILABLE = False

# Mémo des dates de capture, keyé (chemin_abs, mtime) → datetime | None.
# Évite de relire EXIF/ffprobe à chaque /api/state. Plus robuste que le cache
# pHash (qui est keyé rel-BASE et incohérent en multi-source).
_capture_date_memo: dict = {}

try:
    import appdirs
    _STATE_DEFAULT = Path(appdirs.user_data_dir("SortMemories", "EliottBouquerel"))
except ImportError:
    _STATE_DEFAULT = Path.home() / ".sort-memories"

try:
    import numpy as np
    import open_clip as _open_clip
    import torch
    CLIP_AVAILABLE = True
except ImportError:
    CLIP_AVAILABLE = False

from sort_memories import updater as _updater

# ── Répertoires ──────────────────────────────────────────────────────────────
# MEDIA_DIR  = dossier source à trier (sélectionné par l'utilisateur au lancement)
# STATE_DIR  = répertoire d'état (cache pHash, embeddings CLIP, état triage, licence)
MEDIA_DIR = Path(os.environ.get("SORT_MEMORIES_MEDIA_DIR", str(Path.home() / "Pictures"))).expanduser().resolve()
STATE_DIR = Path(os.environ.get("SORT_MEMORIES_STATE_DIR", str(_STATE_DEFAULT))).expanduser().resolve()
STATE_DIR.mkdir(parents=True, exist_ok=True)

# Alias historique conservé pour compat interne (anciennes refs `BASE / rel` = media)
BASE           = MEDIA_DIR
TRASH_DIR      = MEDIA_DIR / "_a_supprimer"

# Namespace les fichiers d'état par dossier source (hash court du chemin absolu),
# pour permettre à un même STATE_DIR de servir plusieurs MEDIA_DIR sans collision.
import hashlib as _hashlib

_MEDIA_NS      = _hashlib.sha1(str(MEDIA_DIR).encode()).hexdigest()[:12]
_NS_DIR        = STATE_DIR / "folders" / _MEDIA_NS
_NS_DIR.mkdir(parents=True, exist_ok=True)
(_NS_DIR / "source.txt").write_text(str(MEDIA_DIR))   # trace humaine

STATE_FILE     = _NS_DIR / "triage_state.json"
CACHE_FILE     = _NS_DIR / "dedupe_cache.json"
GROUPS_FILE    = _NS_DIR / "dedupe_groups.json"

# Formats pour lesquels Pillow peut écrire en gardant l'extension d'origine.
# Les autres (.heic/.heif via pillow-heif) sont gérés à part dans _do_rotate/_do_crop.
# (IMAGE_EXT/VIDEO_EXT/MEDIA_EXT sont définis plus bas, après SESSION_FILE — superset v0.6.0.)
PIL_NATIVE_WRITE = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".tiff", ".tif", ".bmp"}

HASH_THRESHOLD = 10   # distance Hamming ≤ 10/64 bits (~15%) — couvre ré-encodage, resize, changement format
VIDEO_FRAMES   = 5
VIDEO_MATCH    = 3    # frames minimum correspondantes sur VIDEO_FRAMES

app = Flask(__name__)

# ── Session configuration (v0.2.0) ────────────────────────────────────────────
# Une "session" = N dossiers sources + options de tri. Persiste dans STATE_DIR.
# Tant que `configured` est False, l'UI affiche la vue accueil et collect_files
# retourne vide.

SESSION_FILE = STATE_DIR / "session.json"

# Formats RAW appareil photo (décodés via rawpy/libraw, jamais rendus par le navigateur).
RAW_EXT = {".dng", ".cr2", ".cr3", ".nef", ".arw", ".raf", ".orf", ".rw2", ".srw", ".pef", ".raw"}

# Toutes les photos prises en charge (triage + dédup + compression).
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".heif",
             ".avif", ".tiff", ".tif", ".bmp"} | RAW_EXT

# Tous les conteneurs vidéo décodables par ffmpeg.
VIDEO_EXT = {".mp4", ".mov", ".m4v", ".avi", ".mkv", ".wmv", ".flv", ".3gp",
             ".3g2", ".mpg", ".mpeg", ".webm", ".ts", ".mts", ".m2ts", ".hevc"}

MEDIA_EXT = IMAGE_EXT | VIDEO_EXT

# Sous-ensembles rendus nativement par WKWebView (sinon → preview JPEG généré, cf. /preview).
WEB_IMG_EXT   = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".heif", ".avif"}
WEB_VIDEO_EXT = {".mp4", ".mov", ".m4v"}

def _is_web_renderable(rel: str) -> bool:
    """True si WKWebView peut afficher le fichier directement (sinon preview JPEG)."""
    return Path(rel).suffix.lower() in (WEB_IMG_EXT | WEB_VIDEO_EXT)

def _entry_is_video(entry: str) -> bool:
    """True si l'entry pointe vers une vidéo (selon l'extension)."""
    return Path(_entry_rel(entry)).suffix.lower() in VIDEO_EXT

def _entry_matches_filter(entry: str, ftype: str) -> bool:
    """True si l'entry correspond au filtre de tri ('all' | 'photo' | 'video')."""
    if ftype == "video":
        return _entry_is_video(entry)
    if ftype == "photo":
        return not _entry_is_video(entry)
    return True

# Filtre de tri actif (en mémoire, partagé single + galerie) : 'all' | 'photo' | 'video'.
_triage_filter = "all"

DEFAULT_OPTIONS = {
    "by_year":     True,
    "by_month":    False,
    "split_media": False,
    "rename":      False,
    "order":       "default",   # "default" | "largest" — ordre de la file de triage
}

# Options non-booléennes (validées séparément dans api_config_set).
_ORDER_VALUES = {"default", "largest"}

# Pywebview window injectée par app.py via set_main_window() pour exposer le
# folder picker natif à l'UI Flask via /api/pick_folder. None en dev browser.
_main_window = None

def set_main_window(window):
    global _main_window
    _main_window = window

_session_config = {
    "configured": False,
    "sources":    [],            # list[str] — chemins absolus
    "options":    dict(DEFAULT_OPTIONS),
}

def _save_session_config():
    try:
        SESSION_FILE.write_text(json.dumps(_session_config, ensure_ascii=False, indent=2))
    except Exception:
        pass

def _load_session_config():
    global _session_config
    if SESSION_FILE.exists():
        try:
            data = json.loads(SESSION_FILE.read_text())
            if data.get("configured"):
                _session_config.update(data)
                _session_config["options"] = {**DEFAULT_OPTIONS, **(data.get("options") or {})}
                return
        except Exception:
            pass
    # Fallback : si SORT_MEMORIES_MEDIA_DIR set explicitement → auto-config
    if os.environ.get("SORT_MEMORIES_MEDIA_DIR"):
        _session_config["configured"] = True
        _session_config["sources"]    = [str(MEDIA_DIR)]
        _session_config["options"]    = dict(DEFAULT_OPTIONS)

def _is_configured() -> bool:
    return bool(_session_config["configured"] and _session_config["sources"])

def _sources() -> list:
    return [Path(p) for p in _session_config["sources"]]

def _opts() -> dict:
    return _session_config["options"]

def _make_entry(src_idx: int, rel: str) -> str:
    """Encode (source_index, relative_path) → string JSON-safe."""
    return f"{src_idx}::{rel}"

def _parse_entry(entry: str):
    """Décode "<idx>::<rel>" → (src_idx, rel). Legacy "<rel>" → (0, rel)."""
    if "::" in entry:
        idx_s, rel = entry.split("::", 1)
        try:
            return int(idx_s), rel
        except ValueError:
            return 0, entry
    return 0, entry

def _entry_source(entry: str) -> Path:
    idx, _ = _parse_entry(entry)
    srcs = _sources()
    return srcs[idx] if 0 <= idx < len(srcs) else MEDIA_DIR

def _entry_path(entry: str) -> Path:
    idx, rel = _parse_entry(entry)
    return _entry_source(entry) / rel

def _entry_rel(entry: str) -> str:
    """Retourne juste le chemin relatif à la source."""
    _, rel = _parse_entry(entry)
    return rel

def _config_namespace() -> str:
    """Hash court de la config (sources + options) pour namespacer le state."""
    payload = json.dumps({
        "sources": sorted(_session_config["sources"]),
        "options": _session_config["options"],
    }, sort_keys=True)
    return _hashlib.sha1(payload.encode()).hexdigest()[:12]

def _is_image(rel: str) -> bool:
    return Path(rel).suffix.lower() in IMAGE_EXT

def _is_video(rel: str) -> bool:
    return Path(rel).suffix.lower() in VIDEO_EXT

def compute_keep_destination(entry: str) -> Path:
    """Calcule le chemin de destination pour un fichier 'gardé' selon les options.

    Structure : <source>/Tri/Gardées/[images|videos/]/[YYYY/]/[MM/]/[renamed_]filename
    """
    src    = _entry_source(entry)
    rel    = _entry_rel(entry)
    opts   = _opts()
    parts  = [src, "Tri", "Gardées"]

    if opts.get("split_media"):
        if _is_image(rel):
            parts.append("images")
        elif _is_video(rel):
            parts.append("videos")
        else:
            parts.append("autres")

    # Date de capture RÉELLE (EXIF/creation_time/nom/mtime), pas la date de fichier.
    capture_dt = _capture_datetime(entry)

    if opts.get("by_year") or opts.get("by_month"):
        if capture_dt:
            if opts.get("by_year"):
                parts.append(str(capture_dt.year))
            if opts.get("by_month"):
                parts.append(f"{capture_dt.month:02d}")

    if opts.get("rename"):
        if capture_dt:
            stamp = capture_dt.strftime("%Y-%m-%d")
        else:
            import re
            m = re.match(r"^(\d{4})-(\d{2})-(\d{2})", Path(rel).name)
            stamp = f"{m.group(1)}-{m.group(2)}-{m.group(3)}" if m else "undated"
        short_hash = _hashlib.sha1(rel.encode()).hexdigest()[:8]
        name = f"{stamp}_{short_hash}{Path(rel).suffix.lower()}"
    else:
        name = Path(rel).name

    dst_dir = Path(*[str(p) for p in parts])
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / name
    n = 1
    while dst.exists():
        dst = dst_dir / f"{Path(name).stem}_{n}{Path(name).suffix}"
        n += 1
    return dst

def compute_trash_destination(entry: str) -> Path:
    """Chemin de la corbeille pour un fichier supprimé : <source>/Tri/Supprimées/<name>."""
    src     = _entry_source(entry)
    rel     = _entry_rel(entry)
    dst_dir = src / "Tri" / "Supprimées"
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / Path(rel).name
    n = 1
    while dst.exists():
        dst = dst_dir / f"{Path(rel).stem}_{n}{Path(rel).suffix}"
        n += 1
    return dst

# ── Licence ───────────────────────────────────────────────────────────────────
# v0.1.0 : paywall désactivé (open trial). Le code reste en place pour réactivation
# future via la variable d'environnement SORT_MEMORIES_LICENSE_ENFORCE=1.
LICENSE_ENFORCE   = os.environ.get("SORT_MEMORIES_LICENSE_ENFORCE") == "1"
LICENSE_FILE      = STATE_DIR / "license.json"
TRIAL_LIMIT       = 1000
VERIFY_URL        = "https://triage.eliottbouquerel.fr/api/verify"   # backend à déployer
OFFLINE_GRACE_DAYS = 30

_license_cache    = None   # None = pas encore vérifié, True/False = résultat

def _load_license():
    if not LICENSE_FILE.exists():
        return None
    try:
        return json.loads(LICENSE_FILE.read_text())
    except Exception:
        return None

def check_license() -> bool:
    global _license_cache
    # v0.1.0 : paywall désactivé sauf si SORT_MEMORIES_LICENSE_ENFORCE=1
    if not LICENSE_ENFORCE:
        _license_cache = True
        return True
    if _license_cache is not None:
        return _license_cache
    lic = _load_license()
    if not lic or not lic.get("key"):
        _license_cache = False
        return False
    # Grace period offline : si vérification récente (< OFFLINE_GRACE_DAYS)
    import datetime
    verified_at = lic.get("verified_at")
    if verified_at:
        try:
            last = datetime.datetime.fromisoformat(verified_at)
            if (datetime.datetime.now() - last).days < OFFLINE_GRACE_DAYS:
                _license_cache = True
                return True
        except Exception:
            pass
    # Vérification en ligne
    try:
        import urllib.request
        req  = urllib.request.Request(
            VERIFY_URL,
            data=json.dumps({"key": lic["key"]}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=5) as r:
            resp = json.loads(r.read())
        if resp.get("valid"):
            lic["verified_at"] = datetime.datetime.now().isoformat()
            LICENSE_FILE.write_text(json.dumps(lic, ensure_ascii=False))
            _license_cache = True
            return True
    except Exception:
        # Serveur inaccessible → grace period étendu si clé présente
        if lic.get("key"):
            _license_cache = True
            return True
    _license_cache = False
    return False

def activate_license(key: str) -> dict:
    global _license_cache
    import datetime
    import re
    key = key.strip()
    if not re.match(r'^[A-Z0-9\-]{10,}$', key, re.I):
        return {"ok": False, "error": "Clé invalide — format incorrect"}
    # Vérification en ligne
    try:
        import urllib.request
        req  = urllib.request.Request(
            VERIFY_URL,
            data=json.dumps({"key": key}).encode(),
            headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=8) as r:
            resp = json.loads(r.read())
        if not resp.get("valid"):
            return {"ok": False, "error": resp.get("error", "Clé non reconnue")}
    except Exception:
        # Serveur non encore déployé → accepter la clé (mode dev/test)
        pass
    lic = {"key": key, "activated_at": datetime.datetime.now().isoformat(),
           "verified_at": datetime.datetime.now().isoformat()}
    LICENSE_FILE.write_text(json.dumps(lic, ensure_ascii=False))
    _license_cache = True
    return {"ok": True}

_scan_status = {"running": False, "done": False, "progress": 0, "total": 0, "groups_count": 0, "error": None}
_scan_lock   = threading.Lock()

# ── Caches en mémoire — chargés une fois au démarrage ────────────────────────
_mem_lock       = threading.Lock()
_mem_hash_cache = {}   # {rel: {hash, resolution, mtime, size, video_hashes}}
_mem_groups     = {}   # {gid: {paths, similarity}}
_mem_file2group = {}   # {rel: gid}
_mem_state      = None # dict courant (files, current, history)

# ── CLIP ViT-L/14 — groupes sémantiques ──────────────────────────────────────
CLIP_THRESHOLD        = 0.82   # cosine similarity ≥ 82% → groupe sémantique
CLIP_GROUPS_PATH      = _NS_DIR / "clip_groups.json"
CLIP_EMB_PATH         = _NS_DIR / "clip_embeddings.npy"
CLIP_INDEX_PATH       = _NS_DIR / "clip_index.json"

_clip_model           = None   # modèle chargé une fois
_clip_preprocess      = None
_clip_text_emb        = None   # embeddings texte des labels (cache)
_clip_lock            = threading.Lock()
_clip_scan_status     = {"running": False, "done": False, "progress": 0, "total": 0,
                         "groups_count": 0, "error": None, "phase": "idle"}

_mem_clip_groups      = {}   # {cgid: {paths, similarity, label_icon, label_text}}
_mem_clip_file2group  = {}   # {rel: cgid}

CLIP_LABELS = [
    ("🚗", "a car or vehicle"),
    ("👤", "a selfie or portrait of a person"),
    ("🍕", "food or a meal"),
    ("🌅", "a landscape or nature scene"),
    ("🏛", "a building or architecture"),
    ("🐕", "a dog or cat or animal"),
    ("👥", "a group of people celebrating"),
    ("📄", "a screenshot or document or text"),
    ("🎆", "a concert or event or fireworks"),
    ("🌃", "a city street at night"),
    ("🏖", "a beach or swimming pool"),
    ("🖼", "an image"),
]
CLIP_LABEL_FR = {
    "a car or vehicle":                     "Véhicule",
    "a selfie or portrait of a person":     "Portrait",
    "food or a meal":                       "Nourriture",
    "a landscape or nature scene":          "Paysage",
    "a building or architecture":           "Architecture",
    "a dog or cat or animal":               "Animal",
    "a group of people celebrating":        "Groupe",
    "a screenshot or document or text":     "Document",
    "a concert or event or fireworks":      "Événement",
    "a city street at night":               "Nuit",
    "a beach or swimming pool":             "Plage",
    "an image":                             "Photos similaires",
}

def _rebind_state_paths():
    """Re-pointe STATE_FILE, CACHE_FILE, etc. selon le namespace de la session courante."""
    global STATE_FILE, CACHE_FILE, GROUPS_FILE, _NS_DIR
    global CLIP_GROUPS_PATH, CLIP_EMB_PATH, CLIP_INDEX_PATH
    if not _is_configured():
        return
    ns = _config_namespace()
    _NS_DIR = STATE_DIR / "sessions" / ns
    _NS_DIR.mkdir(parents=True, exist_ok=True)
    # Trace humaine : quelles sources, quelles options
    try:
        (_NS_DIR / "config.json").write_text(json.dumps(_session_config, indent=2, ensure_ascii=False))
    except Exception:
        pass
    STATE_FILE       = _NS_DIR / "triage_state.json"
    CACHE_FILE       = _NS_DIR / "dedupe_cache.json"
    GROUPS_FILE      = _NS_DIR / "dedupe_groups.json"
    CLIP_GROUPS_PATH = _NS_DIR / "clip_groups.json"
    CLIP_EMB_PATH    = _NS_DIR / "clip_embeddings.npy"
    CLIP_INDEX_PATH  = _NS_DIR / "clip_index.json"

def _reset_mem():
    """Vide tous les caches mémoire (utilisé lors d'un /api/reset ou /api/config POST)."""
    global _mem_hash_cache, _mem_groups, _mem_file2group, _mem_state
    global _mem_clip_groups, _mem_clip_file2group
    _mem_hash_cache       = {}
    _mem_groups           = {}
    _mem_file2group       = {}
    _mem_clip_groups      = {}
    _mem_clip_file2group  = {}
    _mem_state            = None

def _init_mem():
    """Charge tout en mémoire au démarrage. Appelé après /api/config ou au boot si déjà configuré."""
    global _mem_hash_cache, _mem_groups, _mem_file2group, _mem_state
    if not _is_configured():
        _mem_state = {"files": [], "current": 0, "history": []}
        return
    _rebind_state_paths()
    # Hash cache
    if CACHE_FILE.exists():
        try:
            with open(CACHE_FILE) as f:
                _mem_hash_cache = json.load(f)
        except Exception:
            _mem_hash_cache = {}
    # Groups (fichier léger séparé)
    if GROUPS_FILE.exists():
        try:
            with open(GROUPS_FILE) as f:
                g = json.load(f)
            _mem_groups     = g.get("groups", {})
            _mem_file2group = g.get("file_to_group", {})
        except Exception:
            pass
    # State (sans groupes — fichier léger)
    if STATE_FILE.exists():
        try:
            s = json.loads(STATE_FILE.read_text())
            # Migration : extraire les groupes s'ils étaient dans le state
            if "groups" in s and not _mem_groups:
                _mem_groups     = s.pop("groups", {})
                _mem_file2group = s.pop("file_to_group", {})
                _save_groups_file()
            else:
                s.pop("groups", None)
                s.pop("file_to_group", None)
            s.pop("dedupe_scan_done", None)
            _mem_state = s
        except Exception:
            _mem_state = None
    if _mem_state is None:
        _mem_state = {"files": [], "current": 0, "history": []}
    if not _mem_state.get("files"):
        _mem_state["files"] = collect_files()
        _write_state_file()
    else:
        # Validation post-conversion : drop entries pointant sur fichiers absents,
        # puis ajouter les nouveaux fichiers (extensions changées : .jpg → .webp etc.)
        existing      = [f for f in _mem_state["files"] if _entry_path(f).exists()]
        already_known = set(existing)
        new_files     = [f for f in collect_files() if f not in already_known]
        if len(existing) != len(_mem_state["files"]) or new_files:
            _mem_state["files"] = existing + new_files
            # Ajuster current si on a perdu des fichiers avant lui
            if _mem_state["current"] > len(_mem_state["files"]):
                _mem_state["current"] = len(_mem_state["files"])
            _write_state_file()
    # Tri de la file selon l'option (ex : traiter d'abord les plus volumineux)
    if _apply_queue_order(_mem_state["files"]):
        _write_state_file()
    # Groupes sémantiques CLIP
    if CLIP_GROUPS_PATH.exists():
        try:
            cg = json.loads(CLIP_GROUPS_PATH.read_text())
            _mem_clip_groups     = cg.get("groups", {})
            _mem_clip_file2group = cg.get("file_to_group", {})
        except Exception:
            pass

def _entry_size(entry: str) -> int:
    try:
        return _entry_path(entry).stat().st_size
    except Exception:
        return 0

def _apply_queue_order(files: list) -> bool:
    """Réordonne la file de triage en place selon l'option `order`.
    Retourne True si l'ordre a changé. `largest` = plus volumineux d'abord."""
    if _opts().get("order") != "largest":
        return False
    ordered = sorted(files, key=_entry_size, reverse=True)
    if ordered != files:
        files[:] = ordered
        return True
    return False

def _write_state_file():
    STATE_FILE.write_text(json.dumps(_mem_state, indent=2, ensure_ascii=False))

def _save_groups_file():
    GROUPS_FILE.write_text(json.dumps(
        {"groups": _mem_groups, "file_to_group": _mem_file2group}, ensure_ascii=False))

# ──────────────────────────────────────────────────────────────────────────────
# UNION-FIND
# ──────────────────────────────────────────────────────────────────────────────

class UnionFind:
    def __init__(self):
        self.parent = {}

    def find(self, x):
        if x not in self.parent:
            self.parent[x] = x
        if self.parent[x] != x:
            self.parent[x] = self.find(self.parent[x])
        return self.parent[x]

    def union(self, x, y):
        rx, ry = self.find(x), self.find(y)
        if rx != ry:
            self.parent[ry] = rx

    def groups(self):
        g = defaultdict(list)
        for x in self.parent:
            g[self.find(x)].append(x)
        return {root: members for root, members in g.items() if len(members) > 1}

# ──────────────────────────────────────────────────────────────────────────────
# HASH COMPUTATION
# ──────────────────────────────────────────────────────────────────────────────

def compute_image_hash(p: Path):
    try:
        ext = p.suffix.lower()
        if ext in RAW_EXT:
            if not RAW_AVAILABLE:
                return None, None
            with _rawpy.imread(str(p)) as raw:
                rgb = raw.postprocess(use_camera_wb=True, half_size=True)
            img = Image.fromarray(rgb)
            res = [img.width * 2, img.height * 2]  # half_size → résolution réelle ×2
        else:
            img = Image.open(p)   # HEIC/AVIF/TIFF/BMP via openers enregistrés
            res = list(img.size)
        h = str(imagehash.phash(img))
        return h, res
    except Exception:
        return None, None

def compute_video_hash(p: Path):
    try:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-show_entries", "format=duration",
             "-of", "csv=p=0", str(p)],
            capture_output=True, text=True, timeout=10)
        dur = float(probe.stdout.strip() or "0")
        if dur <= 0:
            return []
        interval = dur / VIDEO_FRAMES
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run([
                "ffmpeg", "-y", "-i", str(p),
                "-vf", f"fps=1/{interval}",
                "-frames:v", str(VIDEO_FRAMES), "-q:v", "5", f"{tmp}/f%d.jpg"
            ], capture_output=True, timeout=60)
            hashes = []
            for f in sorted(Path(tmp).glob("f*.jpg"))[:VIDEO_FRAMES]:
                try:
                    hashes.append(str(imagehash.phash(Image.open(f))))
                except Exception:
                    pass
            return hashes
    except Exception:
        return []

def images_similar(h1, h2) -> bool:
    try:
        return imagehash.hex_to_hash(h1) - imagehash.hex_to_hash(h2) <= HASH_THRESHOLD
    except Exception:
        return False

def videos_similar(hashes1, hashes2) -> bool:
    if len(hashes1) < 2 or len(hashes2) < 2:
        return False
    matches = sum(
        1 for h1, h2 in zip(hashes1, hashes2, strict=False)
        if not (imagehash.hex_to_hash(h1) - imagehash.hex_to_hash(h2) > HASH_THRESHOLD)
    )
    return matches >= VIDEO_MATCH

def load_hash_cache() -> dict:
    if CACHE_FILE.exists():
        try:
            return json.loads(CACHE_FILE.read_text())
        except Exception:
            pass
    return {}

def save_hash_cache(cache: dict):
    CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False))

def all_scannable_files() -> list:
    """Tous les médias sous les sources configurées, à scanner pour pHash."""
    result = []
    skip_dirs = {"_a_supprimer", "Tri"}
    for src in _sources():
        if not src.exists():
            continue
        for f in sorted(src.rglob("*")):
            if not f.is_file():
                continue
            if f.suffix.lower() not in MEDIA_EXT:
                continue
            if f.name.startswith(".") or f.name.endswith("-overlay.png"):
                continue
            try:
                rel_parts = f.relative_to(src).parts
            except Exception:
                continue
            if any(part in skip_dirs for part in rel_parts):
                continue
            result.append(f)
    return result

# ──────────────────────────────────────────────────────────────────────────────
# GROUP BUILDING
# ──────────────────────────────────────────────────────────────────────────────

def quality_score(rel: str, entry: dict) -> int:
    score = 0
    if "Gardés" in rel:
        score += 1_000_000_000
    score += entry.get("size", 0)
    res = entry.get("resolution")
    if res:
        score += res[0] * res[1] * 100
    return score

def build_groups(cache: dict):
    uf = UnionFind()
    image_entries = [(r, v) for r, v in cache.items() if v.get("hash")]
    video_entries = [(r, v) for r, v in cache.items() if v.get("video_hashes")]

    for (r1, v1), (r2, v2) in combinations(image_entries, 2):
        if images_similar(v1["hash"], v2["hash"]):
            uf.union(r1, r2)

    for (r1, v1), (r2, v2) in combinations(video_entries, 2):
        if videos_similar(v1["video_hashes"], v2["video_hashes"]):
            uf.union(r1, r2)

    raw = uf.groups()
    groups       = {}
    file_to_group = {}

    for i, (_, members) in enumerate(raw.items()):
        gid     = f"grp_{i}"
        max_dist = 0
        for m1, m2 in combinations(members, 2):
            e1, e2 = cache.get(m1, {}), cache.get(m2, {})
            if e1.get("hash") and e2.get("hash"):
                try:
                    d = imagehash.hex_to_hash(e1["hash"]) - imagehash.hex_to_hash(e2["hash"])
                    max_dist = max(max_dist, d)
                except Exception:
                    pass
        similarity = round(1.0 - max_dist / 64, 2)
        groups[gid] = {"paths": members, "similarity": similarity}
        for m in members:
            file_to_group[m] = gid

    return groups, file_to_group

# ──────────────────────────────────────────────────────────────────────────────
# CLIP ViT-L/14 — reconnaissance sémantique
# ──────────────────────────────────────────────────────────────────────────────

def load_clip_model() -> bool:
    global _clip_model, _clip_preprocess
    if _clip_model is not None:
        return True
    if not CLIP_AVAILABLE:
        return False
    try:
        with _clip_lock:
            if _clip_model is None:
                m, _, prep = _open_clip.create_model_and_transforms(
                    "ViT-L-14", pretrained="openai")
                m.eval()
                _clip_model      = m
                _clip_preprocess = prep
        return True
    except Exception:
        return False

def get_text_embeddings():
    global _clip_text_emb
    if _clip_text_emb is not None:
        return _clip_text_emb
    if not load_clip_model():
        return None
    try:
        texts  = [t for _, t in CLIP_LABELS]
        tokens = _open_clip.tokenize(texts)
        with torch.no_grad():
            tf = _clip_model.encode_text(tokens)
            tf = tf / tf.norm(dim=-1, keepdim=True)
        _clip_text_emb = tf.cpu().numpy()
        return _clip_text_emb
    except Exception:
        return None

def compute_clip_embedding(p: Path):
    if not load_clip_model():
        return None
    try:
        img = _clip_preprocess(Image.open(p).convert("RGB")).unsqueeze(0)
        with torch.no_grad():
            emb = _clip_model.encode_image(img)
            emb = emb / emb.norm(dim=-1, keepdim=True)
        return emb[0].cpu().numpy().astype(np.float32)
    except Exception:
        return None

def compute_sharpness(p: Path) -> float:
    try:
        arr = np.array(Image.open(p).convert("L"), dtype=np.float32)
        lap = (arr[:-2, 1:-1] + arr[2:, 1:-1] +
               arr[1:-1, :-2] + arr[1:-1, 2:] - 4 * arr[1:-1, 1:-1])
        return float(lap.var())
    except Exception:
        return 0.0

def clip_group_label(centroid) -> tuple:
    tf = get_text_embeddings()
    if tf is None or centroid is None:
        return ("🖼", "Photos similaires")
    try:
        sims = centroid @ tf.T
        idx  = int(np.argmax(sims))
        em, en = CLIP_LABELS[idx]
        return (em, CLIP_LABEL_FR.get(en, "Photos similaires"))
    except Exception:
        return ("🖼", "Photos similaires")

def clip_quality_score(rel: str, sharpness: float) -> float:
    p   = _entry_path(rel)
    sz  = p.stat().st_size if p.exists() else 0
    ent = _mem_hash_cache.get(rel, {})
    res = ent.get("resolution")
    rs  = res[0] * res[1] if res else 0
    return sharpness * 0.5 + rs * 0.3 + sz * 0.2

def build_clip_groups(embeddings, rels: list, sharpness: dict):
    if not CLIP_AVAILABLE:
        return {}, {}
    n         = len(rels)
    rel_to_idx = {r: i for i, r in enumerate(rels)}
    uf        = UnionFind()
    norms     = np.linalg.norm(embeddings, axis=1, keepdims=True)
    norms[norms == 0] = 1
    emb_n     = embeddings / norms

    chunk = 500
    for i in range(0, n, chunk):
        batch = emb_n[i:i+chunk]
        sims  = batch @ emb_n.T
        rows, cols = np.where(sims >= CLIP_THRESHOLD)
        for bi, j in zip(rows, cols, strict=False):
            gi = i + bi
            if j <= gi:
                continue
            r1, r2 = rels[gi], rels[j]
            # Ignorer les paires déjà dans le même groupe pHash (doublons exacts)
            if _mem_file2group.get(r1) and _mem_file2group.get(r1) == _mem_file2group.get(r2):
                continue
            uf.union(r1, r2)

    raw    = uf.groups()
    groups = {}
    f2g    = {}

    for i, (_, members) in enumerate(raw.items()):
        idxs = [rel_to_idx[m] for m in members if m in rel_to_idx]
        if len(idxs) < 2:
            continue
        sub = emb_n[idxs]
        sim_mat = sub @ sub.T
        np.fill_diagonal(sim_mat, 0)
        avg_sim = float(sim_mat.max())

        centroid = sub.mean(axis=0)
        norm_c   = np.linalg.norm(centroid)
        centroid = centroid / norm_c if norm_c > 0 else centroid

        members_sorted = sorted(members,
            key=lambda r: clip_quality_score(r, sharpness.get(r, 0)), reverse=True)

        cgid = f"cgrp_{i}"
        em, lbl = clip_group_label(centroid)
        groups[cgid] = {
            "paths":      members_sorted,
            "similarity": round(avg_sim, 2),
            "label_icon": em,
            "label_text": lbl,
        }
        for m in members:
            f2g[m] = cgid

    return groups, f2g

def save_clip_groups_file():
    CLIP_GROUPS_PATH.write_text(
        json.dumps({"groups": _mem_clip_groups, "file_to_group": _mem_clip_file2group},
                   ensure_ascii=False))

# ──────────────────────────────────────────────────────────────────────────────
# BACKGROUND SCAN
# ──────────────────────────────────────────────────────────────────────────────

def scan_and_update():
    global _scan_status, _mem_hash_cache, _mem_groups, _mem_file2group
    with _scan_lock:
        _scan_status = {"running": True, "done": False, "progress": 0, "total": 0, "groups_count": 0, "error": None}
    try:
        files = all_scannable_files()
        cache = dict(_mem_hash_cache)  # copie de travail locale
        with _scan_lock:
            _scan_status["total"] = len(files)

        for i, p in enumerate(files):
            rel = str(p.relative_to(BASE))
            try:
                stat = p.stat()
            except FileNotFoundError:
                with _scan_lock:
                    _scan_status["progress"] = i + 1
                continue
            entry = cache.get(rel, {})
            if entry.get("mtime") != stat.st_mtime or entry.get("size") != stat.st_size:
                entry = {"mtime": stat.st_mtime, "size": stat.st_size}
                ext   = p.suffix.lower()
                try:
                    if ext in IMAGE_EXT:
                        h, res              = compute_image_hash(p)
                        entry["hash"]       = h
                        entry["resolution"] = res
                    elif ext in VIDEO_EXT:
                        entry["video_hashes"] = compute_video_hash(p)
                except Exception:
                    pass
                cache[rel] = entry
            with _scan_lock:
                _scan_status["progress"] = i + 1

        # Écrire le cache sur disque
        CACHE_FILE.write_text(json.dumps(cache, ensure_ascii=False))

        groups, file_to_group = build_groups(cache)

        # Mettre à jour les globals mémoire (thread-safe)
        with _mem_lock:
            _mem_hash_cache = cache
            _mem_groups     = groups
            _mem_file2group = file_to_group
            _save_groups_file()

        with _scan_lock:
            _scan_status["running"]      = False
            _scan_status["done"]         = True
            _scan_status["groups_count"] = len(groups)
    except Exception as e:
        with _scan_lock:
            _scan_status["running"] = False
            _scan_status["error"]   = str(e)

def scan_clip():
    """Scan CLIP ViT-L/14 — embeddings sémantiques + groupes par contenu visuel."""
    global _clip_scan_status, _mem_clip_groups, _mem_clip_file2group
    if not CLIP_AVAILABLE:
        with _clip_lock:
            _clip_scan_status["error"] = "open_clip_torch non installé — pip3 install open_clip_torch"
        return

    with _clip_lock:
        _clip_scan_status = {"running": True, "done": False, "progress": 0, "total": 0,
                             "groups_count": 0, "error": None, "phase": "modèle"}
    try:
        # Phase 1 : charger le modèle (télécharge ~890 Mo si absent)
        if not load_clip_model():
            raise RuntimeError("Impossible de charger ViT-L/14")

        img_files = [f for f in all_scannable_files()
                     if f.suffix.lower() in IMAGE_EXT]

        with _clip_lock:
            _clip_scan_status["total"] = len(img_files)
            _clip_scan_status["phase"] = "embeddings"

        # Charger le cache embeddings existant
        old_emb, old_rel2i = None, {}
        if CLIP_EMB_PATH.exists() and CLIP_INDEX_PATH.exists():
            try:
                old_emb   = np.load(str(CLIP_EMB_PATH))
                old_index = json.loads(CLIP_INDEX_PATH.read_text())
                old_rel2i = {r: int(k) for k, r in old_index.items()}
            except Exception:
                old_emb, old_rel2i = None, {}

        embeddings = []
        rels       = []
        sharpness  = {}

        for i, p in enumerate(img_files):
            rel = str(p.relative_to(BASE))
            try:
                p.stat()
            except FileNotFoundError:
                with _clip_lock:
                    _clip_scan_status["progress"] = i + 1
                continue

            ci = old_rel2i.get(rel)
            if ci is not None and old_emb is not None and ci < len(old_emb):
                emb = old_emb[ci]
            else:
                emb = compute_clip_embedding(p)
                if emb is None:
                    with _clip_lock:
                        _clip_scan_status["progress"] = i + 1
                    continue

            embeddings.append(emb)
            rels.append(rel)
            sharpness[rel] = compute_sharpness(p)

            with _clip_lock:
                _clip_scan_status["progress"] = i + 1

        # Sauvegarder les embeddings
        if embeddings:
            arr = np.stack(embeddings)
            np.save(str(CLIP_EMB_PATH), arr)
            CLIP_INDEX_PATH.write_text(
                json.dumps({str(k): r for k, r in enumerate(rels)}, ensure_ascii=False))

        with _clip_lock:
            _clip_scan_status["phase"] = "groupes"

        groups, f2g = {}, {}
        if len(embeddings) >= 2:
            groups, f2g = build_clip_groups(np.stack(embeddings), rels, sharpness)

        with _mem_lock:
            _mem_clip_groups     = groups
            _mem_clip_file2group = f2g
            save_clip_groups_file()

        with _clip_lock:
            _clip_scan_status["running"]      = False
            _clip_scan_status["done"]         = True
            _clip_scan_status["groups_count"] = len(groups)
            _clip_scan_status["phase"]        = "done"

    except Exception as e:
        with _clip_lock:
            _clip_scan_status["running"] = False
            _clip_scan_status["error"]   = str(e)
            _clip_scan_status["phase"]   = "error"

# ──────────────────────────────────────────────────────────────────────────────
# EXISTING HELPERS
# ──────────────────────────────────────────────────────────────────────────────

def _exif_datetime(p: Path):
    """Date de prise de vue depuis l'EXIF d'une image. None si absente/illisible.

    Priorité tags : DateTimeOriginal (36867) > DateTimeDigitized (36868) > DateTime (306).
    Couvre JPEG/TIFF/PNG/HEIC (via pillow_heif). Fallback exifread pour RAW/TIFF.
    """
    # 1) Pillow getexif (rapide, couvre la majorité).
    #    IMPORTANT : DateTimeOriginal (36867) et DateTimeDigitized (36868) vivent
    #    dans le SOUS-IFD Exif (pointeur 0x8769), pas dans l'IFD0. Les lire via
    #    get_ifd(0x8769). Sinon exif.get(36867) renvoie toujours None et on
    #    retombe sur le mtime (faux après une conversion qui réécrit la date fichier).
    try:
        exif = Image.open(p).getexif()
        try:
            sub = exif.get_ifd(0x8769)   # ExifTags.IFD.Exif
        except Exception:
            sub = {}
        for val in (sub.get(36867), sub.get(36868), exif.get(306)):
            if val:
                try:
                    return _datetime.datetime.strptime(str(val).strip(), "%Y:%m:%d %H:%M:%S")
                except ValueError:
                    continue
    except Exception:
        pass
    # 2) exifread (fallback RAW/TIFF où Pillow ne lit pas l'IFD)
    if EXIFREAD_AVAILABLE:
        try:
            with open(p, "rb") as fh:
                tags = _exifread.process_file(fh, details=False, stop_tag="DateTimeOriginal")
            for key in ("EXIF DateTimeOriginal", "EXIF DateTimeDigitized", "Image DateTime"):
                if key in tags:
                    try:
                        return _datetime.datetime.strptime(str(tags[key]).strip(), "%Y:%m:%d %H:%M:%S")
                    except ValueError:
                        continue
        except Exception:
            pass
    return None


def _video_creation_datetime(p: Path):
    """creation_time du conteneur vidéo via ffprobe. None si absent."""
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format_tags=creation_time",
             "-of", "default=nw=1:nk=1", str(p)],
            capture_output=True, text=True, timeout=10)
        raw = r.stdout.strip()
        if not raw:
            return None
        # Formats typiques : 2019-08-01T13:45:02.000000Z
        raw = raw.replace("Z", "+00:00")
        try:
            return _datetime.datetime.fromisoformat(raw)
        except ValueError:
            return _datetime.datetime.strptime(raw[:19], "%Y-%m-%dT%H:%M:%S")
    except Exception:
        return None


def _capture_datetime(entry: str):
    """Date de capture réelle d'un média. None si totalement indéterminable.

    Ordre STRICT (priorité aux vraies métadonnées) :
      1. EXIF (image)  2. creation_time (vidéo)
      3. AAAA[-_]MM dans le nom (convention Snapchat)
      4. mtime du fichier  5. None
    Mémoïsé par (chemin, mtime) pour éviter les relectures EXIF/ffprobe.
    """
    p = _entry_path(entry)
    try:
        mtime = p.stat().st_mtime
    except Exception:
        mtime = None
    memo_key = (str(p), mtime)
    if memo_key in _capture_date_memo:
        return _capture_date_memo[memo_key]

    ext = p.suffix.lower()
    dt = None
    if ext in IMAGE_EXT:
        dt = _exif_datetime(p)
    elif ext in VIDEO_EXT:
        dt = _video_creation_datetime(p)

    if dt is None:
        m = _re.match(r"^(\d{4})[-_](\d{2})?", Path(_entry_rel(entry)).name)
        if m and 1900 <= int(m.group(1)) <= 2100:
            month = int(m.group(2)) if m.group(2) and 1 <= int(m.group(2)) <= 12 else 1
            dt = _datetime.datetime(int(m.group(1)), month, 1)

    if dt is None and mtime is not None:
        dt = _datetime.datetime.fromtimestamp(mtime)

    _capture_date_memo[memo_key] = dt
    return dt


def _year_label(entry: str) -> str:
    """Année d'affichage, fondée sur les vraies métadonnées (cf. _capture_datetime)."""
    dt = _capture_datetime(entry)
    return str(dt.year) if dt else "—"


def collect_files():
    """Collecte tous les médias sous toutes les sources configurées.

    Retourne une liste d'entries "<src_idx>::<rel>" (cf. _make_entry).
    Exclusions : fichiers cachés, dossier Tri/ (résultat), _a_supprimer/, overlays.
    """
    if not _is_configured():
        return []
    files = []
    skip_dirs = {"_a_supprimer", "Tri", "Gardés", "Gardes"}
    for idx, src in enumerate(_sources()):
        if not src.exists():
            continue
        for f in sorted(src.rglob("*")):
            if not f.is_file():
                continue
            if f.suffix.lower() not in MEDIA_EXT:
                continue
            if f.name.startswith(".") or f.name.endswith("-overlay.png"):
                continue
            try:
                rel_parts = f.relative_to(src).parts
            except Exception:
                continue
            if any(part in skip_dirs for part in rel_parts):
                continue
            files.append(_make_entry(idx, str(f.relative_to(src))))
    return files

def _append_new_files() -> int:
    """Ajoute à la file de tri tout média présent sur le disque mais absent de la file.

    Sert au re-scan automatique : quand on rouvre l'app après un tri terminé, de
    nouveaux fichiers ont pu être ajoutés aux dossiers sources. On les enfile à la
    suite pour reprendre le tri sans repartir de zéro. Retourne le nombre ajouté.
    """
    if not _is_configured():
        return 0
    s        = load_state()
    existing = set(s.get("files", []))
    new      = [e for e in collect_files()
                if e not in existing and _entry_path(e).exists()]
    if not new:
        return 0
    s["files"] = s.get("files", []) + new
    save_state(s)
    return len(new)

def find_overlay(entry):
    """Trouve l'overlay -overlay.png correspondant à un fichier -main.<ext>.

    Retourne un entry "<src_idx>::<rel_overlay>" ou None.
    """
    src_idx, rel = _parse_entry(entry)
    p     = _entry_path(entry)
    stem  = p.stem
    if not stem.endswith("-main"):
        return None
    overlay = p.parent / f"{stem[:-5]}-overlay.png"
    if not overlay.exists():
        return None
    source = _entry_source(entry)
    try:
        rel_ov = str(overlay.relative_to(source))
    except ValueError:
        return None
    return _make_entry(src_idx, rel_ov)

def merge_overlay(main_path: Path, overlay_path: Path):
    ext = main_path.suffix.lower()
    if ext in {".jpg", ".jpeg", ".png", ".webp"}:
        base   = Image.open(main_path).convert("RGBA")
        ov     = Image.open(overlay_path).convert("RGBA")
        if ov.size != base.size:
            ov = ov.resize(base.size, Image.LANCZOS)
        merged = Image.alpha_composite(base, ov)
        if ext in {".jpg", ".jpeg"}:
            merged.convert("RGB").save(str(main_path), "JPEG", quality=95, subsampling=0)
        else:
            merged.save(str(main_path))
    elif ext in {".mp4", ".mov"}:
        probe = subprocess.run(
            ["ffprobe", "-v", "quiet", "-select_streams", "v:0",
             "-show_entries", "stream=width,height", "-of", "csv=p=0", str(main_path)],
            capture_output=True, text=True)
        dims = probe.stdout.strip()
        if not dims:
            return
        w, h = dims.split(",")
        tmp  = main_path.with_name(f"_tmp_{main_path.name}")
        result = subprocess.run([
            "ffmpeg", "-y", "-i", str(main_path), "-i", str(overlay_path),
            "-filter_complex", f"[1:v]scale={w}:{h}[ov];[0:v][ov]overlay=0:0:format=auto",
            "-c:v", "libx264", "-crf", "18", "-preset", "fast", "-c:a", "copy", str(tmp)
        ], capture_output=True)
        if result.returncode == 0:
            tmp.replace(main_path)
        else:
            tmp.unlink(missing_ok=True)
            raise RuntimeError(result.stderr.decode()[-300:])

def move_to_gardes(entry: str, overlay_entry: str = None, overlay_visible: bool = True) -> dict:
    """Déplace immédiatement un fichier vers <source>/Tri/Gardées/[options]/.

    Fusionne l'overlay si visible, le trash sinon.
    Retourne un dict avec les chemins (absolus) pour permettre l'undo.
    """
    src = _entry_path(entry)
    dst = compute_keep_destination(entry)

    result = {"original": entry, "kept_path": str(dst)}

    if overlay_entry:
        ov_src = _entry_path(overlay_entry)
        if overlay_visible and ov_src.exists():
            try:
                merge_overlay(src, ov_src)
                ov_src.unlink()
                result["overlay_merged"] = overlay_entry
            except Exception:
                pass
        elif not overlay_visible and ov_src.exists():
            odst = trash_file(overlay_entry)
            result["overlay_trashed"]    = overlay_entry
            result["overlay_trash_path"] = odst

    shutil.move(str(src), str(dst))
    return result

def trash_file(entry):
    """Déplace un fichier vers <source>/Tri/Supprimées/. Retourne le chemin absolu de destination."""
    src = _entry_path(entry)
    if not src.exists():
        return None
    dst = compute_trash_destination(entry)
    shutil.move(str(src), str(dst))
    return str(dst)

def load_state():
    with _mem_lock:
        return dict(_mem_state)   # shallow copy — lecture seule hors lock

def save_state(s):
    with _mem_lock:
        _mem_state.update(s)
        _write_state_file()

# ──────────────────────────────────────────────────────────────────────────────
# API
# ──────────────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template_string(PAGE)

@app.route("/api/dedupe_status")
def api_dedupe_status():
    with _scan_lock:
        return jsonify(dict(_scan_status))

@app.route("/api/rescan", methods=["POST"])
def api_rescan():
    with _scan_lock:
        if _scan_status["running"]:
            return jsonify({"ok": False})
    threading.Thread(target=scan_and_update, daemon=True).start()
    return jsonify({"ok": True})

@app.route("/api/refresh_queue", methods=["POST"])
def api_refresh_queue():
    """Re-scan manuel : enfile les nouveaux médias des dossiers sources (CTA écran fin)."""
    if not _is_configured():
        return jsonify({"ok": False, "error": "session non configurée"}), 400
    added = _append_new_files()
    return jsonify({"ok": True, "added": added})

@app.route("/api/triage_filter", methods=["POST"])
def api_triage_filter():
    """Définit le filtre de tri par type de média : 'all' | 'photo' | 'video'."""
    global _triage_filter
    data  = request.get_json() or {}
    value = data.get("value", "all")
    if value not in ("all", "photo", "video"):
        return jsonify({"ok": False, "error": "valeur invalide"}), 400
    _triage_filter = value
    return jsonify({"ok": True, "filter": _triage_filter})

@app.route("/api/queue_stats")
def api_queue_stats():
    """Comptes des médias restant à trier, par type (pour le sélecteur Tout/Photos/Vidéos)."""
    if not _is_configured():
        return jsonify({"photo": 0, "video": 0, "total": 0, "filter": _triage_filter})
    s       = load_state()
    files   = s.get("files", [])
    idx     = s.get("current", 0)
    pending = [f for f in files[idx:] if _entry_path(f).exists()]
    n_video = sum(1 for f in pending if _entry_is_video(f))
    return jsonify({
        "photo":  len(pending) - n_video,
        "video":  n_video,
        "total":  len(pending),
        "filter": _triage_filter,
    })

@app.route("/api/license", methods=["GET"])
def api_license_status():
    lic = _load_license()
    return jsonify({
        "licensed":  check_license(),
        "key":       lic["key"][:8] + "…" if lic and lic.get("key") else None,
        "trial_remaining": max(0, TRIAL_LIMIT - len(load_state().get("history", []))),
    })

@app.route("/api/license/activate", methods=["POST"])
def api_license_activate():
    global _license_cache
    data = request.get_json()
    key  = (data or {}).get("key", "").strip()
    if not key:
        return jsonify({"ok": False, "error": "Clé manquante"})
    _license_cache = None   # forcer re-vérification
    result = activate_license(key)
    return jsonify(result)

@app.route("/api/clip_status")
def api_clip_status():
    with _clip_lock:
        return jsonify(dict(_clip_scan_status))

@app.route("/api/clip_rescan", methods=["POST"])
def api_clip_rescan():
    with _clip_lock:
        if _clip_scan_status["running"]:
            return jsonify({"ok": False, "reason": "already running"})
    threading.Thread(target=scan_clip, daemon=True).start()
    return jsonify({"ok": True, "available": CLIP_AVAILABLE})

def _state_done_payload(files, idx):
    """Payload 'tri terminé' enrichi des comptes restants par type (pour les CTA/filtre)."""
    pending = [f for f in files[idx:] if _entry_path(f).exists()]
    n_video = sum(1 for f in pending if _entry_is_video(f))
    return jsonify({
        "done":          True,
        "total":         len(files),
        "pending":       len(pending),
        "pending_photo": len(pending) - n_video,
        "pending_video": n_video,
        "filter":        _triage_filter,
    })

@app.route("/api/state")
def api_state():
    # Si aucune session configurée → UI affiche la vue accueil.
    if not _is_configured():
        return jsonify({
            "mode":     "needs_config",
            "done":     False,
            "defaults": {"options": DEFAULT_OPTIONS},
        })

    s          = load_state()
    idx, files = s["current"], s["files"]

    # Re-scan auto : à la frontière "fin de file", chercher de nouveaux médias
    # ajoutés aux dossiers depuis la dernière session avant de déclarer le tri fini.
    if idx >= len(files):
        if _append_new_files():
            s = load_state()
            idx, files = s["current"], s["files"]
        else:
            return _state_done_payload(files, idx)

    # Sauter les fichiers absents du disque (déjà déplacés vers Gardés/ ou trashés)
    start_idx = idx
    while idx < len(files) and not _entry_path(files[idx]).exists():
        idx += 1
    if idx != start_idx:
        s["current"] = idx
        save_state(s)

    if idx >= len(files):
        if _append_new_files():
            s = load_state()
            files = s["files"]
            while idx < len(files) and not _entry_path(files[idx]).exists():
                idx += 1
            if idx != s["current"]:
                s["current"] = idx
                save_state(s)
        if idx >= len(files):
            return _state_done_payload(files, idx)

    # Filtre par type (transient : ne touche pas au current sauvegardé, pour qu'un
    # changement de filtre révèle à nouveau les fichiers du type précédemment masqué).
    if _triage_filter != "all":
        j = idx
        while j < len(files) and not (
            _entry_path(files[j]).exists() and _entry_matches_filter(files[j], _triage_filter)):
            j += 1
        if j >= len(files):
            return _state_done_payload(files, idx)
        idx = j

    # Paywall essai gratuit
    processed = len(s.get("history", []))
    if processed >= TRIAL_LIMIT and not check_license():
        return jsonify({
            "mode":      "trial_limit",
            "done":      False,
            "processed": processed,
            "limit":     TRIAL_LIMIT,
            "total":     len(files),
        })

    rel = files[idx]
    gid = _mem_file2group.get(rel)

    # GROUP MODE — utilise les caches mémoire, zéro lecture disque
    if gid and gid in _mem_groups:
        group_paths    = _mem_groups[gid]["paths"]
        files_set      = set(files)
        group_in_queue = [p for p in group_paths if p in files_set]

        if len(group_in_queue) >= 2:
            group_in_queue.sort(
                key=lambda r: quality_score(r, _mem_hash_cache.get(r, {})), reverse=True)

            def file_meta(r):
                p        = _entry_path(r)
                rel_only = _entry_rel(r)
                entry    = _mem_hash_cache.get(r, {})
                res      = entry.get("resolution")
                ov       = find_overlay(r)
                return {
                    "rel":         r,
                    "url":         f"/media/{r}",
                    "preview_url": None if _is_web_renderable(rel_only) else f"/preview/{r}",
                    "is_video":    Path(rel_only).suffix.lower() in VIDEO_EXT,
                    "size_kb":     round(p.stat().st_size / 1024) if p.exists() else 0,
                    "resolution":  f"{res[0]}×{res[1]}" if res else "?",
                    "date":        _year_label(r),
                    "name":        Path(rel_only).name,
                    "overlay_url": f"/media/{ov}" if ov else None,
                    "overlay_rel": ov,
                }

            return jsonify({
                "mode":        "group",
                "done":        False,
                "index":       idx,
                "total":       len(files),
                "group_id":    gid,
                "similarity":  _mem_groups[gid]["similarity"],
                "group_files": [file_meta(r) for r in group_in_queue],
                "can_back":    len(s["history"]) > 0,
            })

    # SEMANTIC GROUP MODE (CLIP)
    cgid = _mem_clip_file2group.get(rel)
    if cgid and cgid in _mem_clip_groups:
        cg_paths = _mem_clip_groups[cgid]["paths"]
        files_set = set(files)
        cg_in_queue = [p for p in cg_paths if p in files_set]

        if len(cg_in_queue) >= 2:
            def clip_file_meta(r):
                p     = _entry_path(r)
                entry = _mem_hash_cache.get(r, {})
                res   = entry.get("resolution")
                ov    = find_overlay(r)
                return {
                    "rel":         r,
                    "url":         f"/media/{r}",
                    "preview_url": None if _is_web_renderable(_entry_rel(r)) else f"/preview/{r}",
                    "is_video":    Path(r).suffix.lower() in VIDEO_EXT,
                    "size_kb":     round(p.stat().st_size / 1024) if p.exists() else 0,
                    "resolution":  f"{res[0]}×{res[1]}" if res else "?",
                    "date":        _year_label(r),
                    "overlay_url": f"/media/{ov}" if ov else None,
                    "overlay_rel": ov,
                }
            cg = _mem_clip_groups[cgid]
            return jsonify({
                "mode":        "semantic_group",
                "done":        False,
                "index":       idx,
                "total":       len(files),
                "group_id":    cgid,
                "similarity":  cg["similarity"],
                "label_icon":  cg.get("label_icon", "🖼"),
                "label_text":  cg.get("label_text", "Photos similaires"),
                "group_files": [clip_file_meta(r) for r in cg_in_queue],
                "can_back":    len(s["history"]) > 0,
            })

    # SINGLE MODE
    rel_only = _entry_rel(rel)
    ext      = Path(rel_only).suffix.lower()
    overlay  = find_overlay(rel)
    src_idx, _ = _parse_entry(rel)
    src_name = Path(_entry_source(rel)).name
    return jsonify({
        "mode":           "single",
        "done":           False,
        "index":          idx,
        "total":          len(files),
        "name":           Path(rel_only).name,
        "year":           _year_label(rel),
        "url":            f"/media/{rel}",
        "preview_url":    None if _is_web_renderable(rel_only) else f"/preview/{rel}",
        "is_video":       ext in VIDEO_EXT,
        "can_back":       len(s["history"]) > 0,
        "overlay_url":    f"/media/{overlay}" if overlay else None,
        "overlay_rel":    overlay,
        "source_idx":     src_idx,
        "source_name":    src_name,
        "sources_total":  len(_sources()),
        "filter":         _triage_filter,
    })

@app.route("/api/action", methods=["POST"])
def api_action():
    data   = request.get_json()
    action = data["action"]
    s      = load_state()
    files  = s["files"]
    idx    = s["current"]

    # ── BACK ──────────────────────────────────────────────────────────────────
    if action == "back":
        if not s["history"]:
            return jsonify({"ok": False})
        last = s["history"].pop()

        if last["action"] in ("keep_from_group", "keep_all_group", "trash_all_group", "decide_semantic_group"):
            # Restore trashed files to disk
            for item in last.get("trashed", []):
                tp = item.get("trash_path")
                if tp and Path(tp).exists():
                    shutil.move(tp, str(_entry_path(item["file"])))
                otp = item.get("overlay_trash_path")
                if otp and Path(otp).exists():
                    shutil.move(otp, str(_entry_path(item["overlay_rel"])))
            # Restore kept file(s) from leur Tri/Gardées/
            if last["action"] == "keep_from_group":
                kept_path = last.get("kept_path")
                if kept_path and Path(kept_path).exists():
                    shutil.move(kept_path, str(_entry_path(last["file"])))
            elif last["action"] in ("keep_all_group", "decide_semantic_group"):
                for item in last.get("kept_items", []):
                    kp = item.get("kept_path")
                    if kp and Path(kp).exists():
                        shutil.move(kp, str(_entry_path(item["file"])))
            # Re-insert group files at current position
            group_files       = last.get("group_files_at_time", [])
            current_files_set = set(s["files"])
            to_insert         = [f for f in group_files if f not in current_files_set]
            ins               = s["current"]
            s["files"]        = s["files"][:ins] + to_insert + s["files"][ins:]

        elif last["action"] == "trash":
            target_file = last["file"]
            tp = last.get("trash_path")
            if tp and Path(tp).exists():
                shutil.move(tp, str(_entry_path(target_file)))
            if target_file not in s["files"]:
                s["files"].insert(s["current"], target_file)
            if last.get("overlay_trash_path"):
                otp = Path(last["overlay_trash_path"])
                if otp.exists():
                    shutil.move(str(otp), str(_entry_path(last["overlay_rel"])))
            if target_file in s["files"]:
                s["current"] = s["files"].index(target_file)

        elif last["action"] == "keep":
            target_file = last["file"]
            kept_path   = last.get("kept_path")
            if kept_path and Path(kept_path).exists():
                shutil.move(kept_path, str(_entry_path(target_file)))
            if last.get("overlay_trashed") and last.get("overlay_trash_path"):
                otp = Path(last["overlay_trash_path"])
                if otp.exists():
                    shutil.move(str(otp), str(_entry_path(last["overlay_trashed"])))
            if target_file not in s["files"]:
                s["files"].insert(s["current"], target_file)
            if target_file in s["files"]:
                s["current"] = s["files"].index(target_file)

        save_state(s)
        return jsonify({"ok": True})

    # ── keep_from_group ───────────────────────────────────────────────────────
    if action == "keep_from_group":
        keep_rel    = data["keep_rel"]
        gid         = data["group_id"]
        group_paths = _mem_groups.get(gid, {}).get("paths", [])
        files_set   = set(files)
        in_queue    = [p for p in group_paths if p in files_set]

        trashed = []
        for rel in in_queue:
            if rel == keep_rel:
                continue
            dst = trash_file(rel)
            item = {"file": rel, "trash_path": dst}
            ov = find_overlay(rel)
            if ov:
                odst = trash_file(ov)
                item["overlay_rel"]        = ov
                item["overlay_trash_path"] = odst
            trashed.append(item)

        # Déplacer immédiatement le fichier gardé vers Gardés/YYYY/
        ov_keep     = find_overlay(keep_rel)
        keep_result = move_to_gardes(keep_rel, ov_keep, True)

        s["history"].append({
            "action":              "keep_from_group",
            "file":                keep_rel,
            "kept_path":           keep_result["kept_path"],
            "group_id":            gid,
            "trashed":             trashed,
            "group_files_at_time": in_queue,
        })
        remove_set = set(in_queue)
        s["files"] = [f for f in files if f not in remove_set]
        save_state(s)
        return jsonify({"ok": True})

    # ── keep_all_group ────────────────────────────────────────────────────────
    if action == "keep_all_group":
        gid         = data["group_id"]
        group_paths = _mem_groups.get(gid, {}).get("paths", [])
        files_set   = set(files)
        in_queue    = [p for p in group_paths if p in files_set]

        # Déplacer immédiatement tous les fichiers du groupe vers Gardés/YYYY/
        kept_items = []
        for rel in in_queue:
            ov     = find_overlay(rel)
            result = move_to_gardes(rel, ov, True)
            kept_items.append({"file": rel, "kept_path": result["kept_path"]})

        s["history"].append({
            "action":              "keep_all_group",
            "file":                in_queue[0] if in_queue else "",
            "group_id":            gid,
            "trashed":             [],
            "kept_items":          kept_items,
            "group_files_at_time": in_queue,
        })
        remove_set = set(in_queue)
        s["files"] = [f for f in files if f not in remove_set]
        save_state(s)
        return jsonify({"ok": True})

    # ── trash_all_group ───────────────────────────────────────────────────────
    if action == "trash_all_group":
        gid         = data["group_id"]
        group_paths = _mem_groups.get(gid, {}).get("paths", [])
        files_set   = set(files)
        in_queue    = [p for p in group_paths if p in files_set]

        trashed = []
        for rel in in_queue:
            dst  = trash_file(rel)
            item = {"file": rel, "trash_path": dst}
            ov   = find_overlay(rel)
            if ov:
                odst = trash_file(ov)
                item["overlay_rel"]        = ov
                item["overlay_trash_path"] = odst
            trashed.append(item)

        s["history"].append({
            "action":              "trash_all_group",
            "file":                in_queue[0] if in_queue else "",
            "group_id":            gid,
            "trashed":             trashed,
            "group_files_at_time": in_queue,
        })
        remove_set = set(in_queue)
        s["files"] = [f for f in files if f not in remove_set]
        save_state(s)
        return jsonify({"ok": True})

    # ── decide_semantic_group ─────────────────────────────────────────────────
    if action == "decide_semantic_group":
        cgid      = data["group_id"]
        decisions = data["decisions"]   # {rel: "keep"|"trash"}
        cg_paths  = _mem_clip_groups.get(cgid, {}).get("paths", [])
        files_set = set(files)
        in_queue  = [p for p in cg_paths if p in files_set]

        kept_items = []
        trashed    = []
        for rel in in_queue:
            dec = decisions.get(rel, "keep")
            if dec == "trash":
                dst  = trash_file(rel)
                item = {"file": rel, "trash_path": dst}
                ov   = find_overlay(rel)
                if ov:
                    odst = trash_file(ov)
                    item["overlay_rel"]        = ov
                    item["overlay_trash_path"] = odst
                trashed.append(item)
            else:
                ov     = find_overlay(rel)
                result = move_to_gardes(rel, ov, True)
                kept_items.append({"file": rel, "kept_path": result["kept_path"]})

        s["history"].append({
            "action":              "decide_semantic_group",
            "file":                in_queue[0] if in_queue else "",
            "group_id":            cgid,
            "trashed":             trashed,
            "kept_items":          kept_items,
            "group_files_at_time": in_queue,
        })
        remove_set = set(in_queue)
        s["files"] = [f for f in files if f not in remove_set]
        save_state(s)
        return jsonify({"ok": True})

    # ── SINGLE FILE ACTIONS ───────────────────────────────────────────────────
    overlay_rel     = data.get("overlay_rel")
    overlay_visible = data.get("overlay_visible", True)

    if action == "keep":
        rel    = files[idx]
        result = move_to_gardes(rel, overlay_rel, overlay_visible)
        entry  = {"action": "keep", "file": rel, "kept_path": result["kept_path"]}
        if "overlay_merged" in result:
            entry["overlay_merged"] = result["overlay_merged"]
        if "overlay_trashed" in result:
            entry["overlay_trashed"]    = result["overlay_trashed"]
            entry["overlay_trash_path"] = result["overlay_trash_path"]
        s["history"].append(entry)
        s["current"] += 1

    elif action == "trash":
        rel = files[idx]
        dst = trash_file(rel)
        entry = {"action": "trash", "file": rel, "trash_path": dst}
        if overlay_rel:
            odst = trash_file(overlay_rel)
            entry["overlay_rel"]        = overlay_rel
            entry["overlay_trash_path"] = odst
        s["history"].append(entry)
        s["current"] += 1

    save_state(s)
    return jsonify({"ok": True})

@app.route("/api/config", methods=["GET"])
def api_config_get():
    """Renvoie la config session courante + valeurs par défaut + info sources existantes."""
    sources_info = []
    if _is_configured():
        for idx, p in enumerate(_session_config["sources"]):
            path = Path(p)
            sources_info.append({
                "idx":    idx,
                "path":   str(path),
                "exists": path.exists(),
                "name":   path.name or str(path),
            })
    return jsonify({
        "configured":      _is_configured(),
        "sources":         _session_config["sources"],
        "sources_info":    sources_info,
        "options":         _session_config["options"],
        "default_options": DEFAULT_OPTIONS,
    })

@app.route("/api/config", methods=["POST"])
def api_config_set():
    """Définit la config de session.

    Payload : {"sources": ["/abs/path1", "/abs/path2"], "options": {...}}
    """
    data    = request.get_json() or {}
    sources = data.get("sources") or []
    options = data.get("options") or {}

    valid_sources = []
    for s in sources:
        if not s:
            continue
        p = Path(str(s)).expanduser()
        if p.exists() and p.is_dir():
            valid_sources.append(str(p.resolve()))

    if not valid_sources:
        return jsonify({"ok": False, "error": "Aucun dossier source valide fourni."}), 400

    # Booléens coercés en bool ; `order` validé comme string dans _ORDER_VALUES.
    clean_opts = {}
    for k, v in options.items():
        if k == "order":
            clean_opts["order"] = v if v in _ORDER_VALUES else "default"
        elif k in DEFAULT_OPTIONS:
            clean_opts[k] = bool(v)
    _session_config["sources"]    = valid_sources
    _session_config["options"]    = {**DEFAULT_OPTIONS, **clean_opts}
    _session_config["configured"] = True
    _save_session_config()

    _reset_mem()
    _init_mem()

    return jsonify({"ok": True, "configured": True,
                    "sources_count": len(valid_sources),
                    "files_count":   len(_mem_state.get("files", []))})

@app.route("/api/reset", methods=["POST"])
def api_reset():
    """Reset complet de la session : retour à la vue accueil."""
    _session_config["sources"]    = []
    _session_config["options"]    = dict(DEFAULT_OPTIONS)
    _session_config["configured"] = False
    try:
        SESSION_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    _reset_mem()
    return jsonify({"ok": True})

@app.route("/api/transform", methods=["POST"])
def api_transform():
    """Applique une transformation in-place sur le fichier courant.

    Actions :
    - rotate : {action:"rotate", angle:90|180|270} → rotation horaire (image et vidéo)
    - crop   : {action:"crop", entry, x, y, w, h}  → coords pixels sur l'image affichée
    - trim   : {action:"trim", entry, start_s, end_s} → coupe vidéo (start..end en secondes)
    """
    data   = request.get_json() or {}
    action = data.get("action")
    entry  = data.get("entry")
    if not entry:
        return jsonify({"ok": False, "error": "entry manquant"}), 400
    p = _entry_path(entry)
    if not p.exists():
        return jsonify({"ok": False, "error": "fichier introuvable"}), 404

    try:
        if action == "rotate":
            angle = int(data.get("angle", 90))
            return _do_rotate(p, angle)
        if action == "crop":
            return _do_crop(p, int(data["x"]), int(data["y"]), int(data["w"]), int(data["h"]))
        if action == "trim":
            return _do_trim(p, float(data["start_s"]), float(data["end_s"]))
        return jsonify({"ok": False, "error": f"action inconnue: {action}"}), 400
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

def _exif_for_save(p: Path, reset_orientation: bool = False):
    """Bytes EXIF de p à réinjecter lors d'une sauvegarde in-place.

    Préserve la date de capture (DateTimeOriginal, sous-IFD 0x8769) et les autres
    métadonnées lors d'une rotation/rognage. Si `reset_orientation`, force le tag
    Orientation (274) à 1 : la rotation est déjà appliquée aux pixels, on évite donc
    une double rotation à l'affichage. Retourne None si aucune EXIF lisible.
    """
    try:
        if reset_orientation:
            exif = Image.open(p).getexif()
            if not exif:
                return None
            exif.get_ifd(0x8769)   # force le chargement du sous-IFD (DateTimeOriginal)
            exif[274] = 1
            return exif.tobytes()
        return Image.open(p).info.get("exif")
    except Exception:
        return None


def _save_image_in_place(img: Image.Image, p: Path, ext: str, exif=None):
    """Sauvegarde img au chemin p en respectant le format d'origine.

    JPEG/HEIC nécessitent un mode RGB sans alpha. `exif` (bytes) est réinjecté pour
    les formats qui le supportent, afin de préserver la date de capture après
    rotation/rognage. HEIC/HEIF requiert pillow-heif registered (HEIF_AVAILABLE).
    """
    kw = {"exif": exif} if exif else {}
    if ext in (".jpg", ".jpeg"):
        img = img.convert("RGB")
        img.save(str(p), "JPEG", quality=95, subsampling=0, **kw)
    elif ext in (".heic", ".heif"):
        if img.mode not in ("RGB", "L"):
            img = img.convert("RGB")
        img.save(str(p), format="HEIF", quality=90, **kw)
    elif ext in (".webp", ".png", ".tiff", ".tif"):
        img.save(str(p), **kw)
    else:
        img.save(str(p))   # gif/bmp : pas de conteneur EXIF


def _do_rotate(p: Path, angle: int):
    """Rotation horaire de l'angle donné (90, 180, 270). In-place."""
    ext = p.suffix.lower()
    if ext in IMAGE_EXT:
        img = Image.open(p)
        # PIL : ROTATE_270 = 90° clockwise (sens montre)
        if angle == 90:
            rotated = img.transpose(Image.Transpose.ROTATE_270)
        elif angle == 180:
            rotated = img.transpose(Image.Transpose.ROTATE_180)
        elif angle == 270:
            rotated = img.transpose(Image.Transpose.ROTATE_90)
        else:
            return jsonify({"ok": False, "error": "angle doit être 90/180/270"}), 400
        exif = _exif_for_save(p, reset_orientation=True)   # préserve la date, neutralise l'orientation
        _save_image_in_place(rotated, p, ext, exif)
        return jsonify({"ok": True, "kind": "image", "angle": angle})

    if ext in VIDEO_EXT:
        # ffmpeg transpose : 1=90°CW, 2=90°CCW. 180° = double transpose ou hflip+vflip.
        tmp = p.with_name(f"_rot_{p.name}")
        vf  = {90: "transpose=1", 180: "transpose=2,transpose=2", 270: "transpose=2"}.get(angle)
        if not vf:
            return jsonify({"ok": False, "error": "angle doit être 90/180/270"}), 400
        result = subprocess.run([
            "ffmpeg", "-y", "-i", str(p), "-vf", vf,
            "-c:v", "libx264", "-crf", "18", "-preset", "fast",
            "-c:a", "copy", str(tmp),
        ], capture_output=True, timeout=300)
        if result.returncode != 0:
            tmp.unlink(missing_ok=True)
            return jsonify({"ok": False, "error": result.stderr.decode()[-300:]}), 500
        tmp.replace(p)
        return jsonify({"ok": True, "kind": "video", "angle": angle})

    return jsonify({"ok": False, "error": f"extension non supportée: {ext}"}), 400

def _do_crop(p: Path, x: int, y: int, w: int, h: int):
    """Crop in-place d'une IMAGE. Coords en pixels par rapport à l'image source."""
    ext = p.suffix.lower()
    if ext not in IMAGE_EXT:
        return jsonify({"ok": False, "error": "crop disponible uniquement sur les images (vidéo → trim)"}), 400
    img = Image.open(p)
    W, H = img.size
    left   = max(0, min(W, x))
    top    = max(0, min(H, y))
    right  = max(left + 1, min(W, x + w))
    bottom = max(top + 1, min(H, y + h))
    if right - left < 4 or bottom - top < 4:
        return jsonify({"ok": False, "error": "zone de crop trop petite"}), 400
    cropped = img.crop((left, top, right, bottom))
    exif = _exif_for_save(p)   # verbatim : préserve la date de capture (pas de rotation)
    _save_image_in_place(cropped, p, ext, exif)
    return jsonify({"ok": True, "kind": "image",
                    "crop": {"x": left, "y": top, "w": right - left, "h": bottom - top}})

def _do_trim(p: Path, start_s: float, end_s: float):
    """Trim in-place d'une VIDÉO entre start_s et end_s (secondes)."""
    ext = p.suffix.lower()
    if ext not in VIDEO_EXT:
        return jsonify({"ok": False, "error": "trim disponible uniquement sur les vidéos"}), 400
    if end_s <= start_s + 0.1:
        return jsonify({"ok": False, "error": "end doit être > start + 0.1s"}), 400
    tmp = p.with_name(f"_trim_{p.name}")
    # Re-encode pour précision frame-accurate (vs -c copy qui est keyframe-aligné).
    # Vidéo courte donc OK pour les workflows de tri.
    result = subprocess.run([
        "ffmpeg", "-y", "-ss", f"{start_s:.3f}", "-to", f"{end_s:.3f}",
        "-i", str(p),
        "-c:v", "libx264", "-crf", "18", "-preset", "fast",
        "-c:a", "aac", "-b:a", "192k",
        str(tmp),
    ], capture_output=True, timeout=600)
    if result.returncode != 0:
        tmp.unlink(missing_ok=True)
        return jsonify({"ok": False, "error": result.stderr.decode()[-300:]}), 500
    tmp.replace(p)
    return jsonify({"ok": True, "kind": "video",
                    "trim": {"start_s": start_s, "end_s": end_s, "duration_s": end_s - start_s}})

# ── Trim vidéo asynchrone avec progression (v0.7.0) ───────────────────────────
# Le trim ré-encode (ffmpeg) et peut durer plusieurs minutes. On le lance dans un
# thread worker et on expose une vraie progression %, comme la conversion.
_trim_status = {
    "running": False, "done": False, "ok": False, "error": None,
    "percent": 0, "entry": "", "started_at": None, "finished_at": None,
}
_trim_lock   = threading.Lock()
_trim_thread = None

def _trim_worker(entry: str, start_s: float, end_s: float):
    global _trim_status
    import datetime as _dt
    import tempfile
    import time as _time
    p      = _entry_path(entry)
    ext    = p.suffix.lower()
    target = max(0.001, end_s - start_s)
    with _trim_lock:
        _trim_status.update({
            "running": True, "done": False, "ok": False, "error": None,
            "percent": 0, "entry": entry,
            "started_at": _dt.datetime.now().isoformat(), "finished_at": None,
        })
    if ext not in VIDEO_EXT:
        with _trim_lock:
            _trim_status.update({"running": False, "done": True, "ok": False,
                                 "error": "trim disponible uniquement sur les vidéos"})
        return

    tmp     = p.with_name(f"_trim_{p.name}")
    err_log = tempfile.NamedTemporaryFile(delete=False, suffix=".log")
    err_log.close()
    try:
        with open(err_log.name, "w") as ef:
            proc = subprocess.Popen([
                "ffmpeg", "-y", "-ss", f"{start_s:.3f}", "-to", f"{end_s:.3f}",
                "-i", str(p),
                "-c:v", "libx264", "-crf", "18", "-preset", "fast",
                "-c:a", "aac", "-b:a", "192k",
                "-progress", "pipe:1", "-nostats",
                str(tmp),
            ], stdout=subprocess.PIPE, stderr=ef, text=True)
            t0 = _time.monotonic()
            for line in proc.stdout:
                line = line.strip()
                # out_time_us = position de sortie en microsecondes (fiable cross-build).
                if line.startswith("out_time_us="):
                    try:
                        secs = int(line.split("=", 1)[1]) / 1_000_000.0
                    except ValueError:
                        continue
                    pct = max(0, min(99, int(secs / target * 100)))
                    with _trim_lock:
                        _trim_status["percent"] = pct
                if _time.monotonic() - t0 > 600:
                    proc.kill()
                    raise RuntimeError("timeout (10 min)")
            proc.wait()

        if proc.returncode != 0:
            tmp.unlink(missing_ok=True)
            try:
                msg = Path(err_log.name).read_text()[-300:]
            except Exception:
                msg = f"ffmpeg a échoué (code {proc.returncode})"
            with _trim_lock:
                _trim_status.update({"running": False, "done": True, "ok": False, "error": msg})
            return

        tmp.replace(p)
        with _trim_lock:
            _trim_status.update({"running": False, "done": True, "ok": True,
                                 "percent": 100, "error": None,
                                 "finished_at": _dt.datetime.now().isoformat()})
    except Exception as e:
        tmp.unlink(missing_ok=True)
        with _trim_lock:
            _trim_status.update({"running": False, "done": True, "ok": False, "error": str(e)})
    finally:
        Path(err_log.name).unlink(missing_ok=True)

@app.route("/api/trim/start", methods=["POST"])
def api_trim_start():
    global _trim_thread
    data  = request.get_json() or {}
    entry = data.get("entry")
    if not entry:
        return jsonify({"ok": False, "error": "entry manquant"}), 400
    if not _entry_path(entry).exists():
        return jsonify({"ok": False, "error": "fichier introuvable"}), 404
    try:
        start_s = float(data["start_s"])
        end_s   = float(data["end_s"])
    except (KeyError, ValueError, TypeError):
        return jsonify({"ok": False, "error": "start_s/end_s invalides"}), 400
    if end_s <= start_s + 0.1:
        return jsonify({"ok": False, "error": "end doit être > start + 0.1s"}), 400
    with _trim_lock:
        if _trim_status["running"]:
            return jsonify({"ok": False, "error": "trim déjà en cours"}), 409
    _trim_thread = threading.Thread(target=_trim_worker, args=(entry, start_s, end_s), daemon=True)
    _trim_thread.start()
    return jsonify({"ok": True})

@app.route("/api/trim/status")
def api_trim_status():
    with _trim_lock:
        return jsonify(dict(_trim_status))

@app.route("/api/trash_info", methods=["GET"])
def api_trash_info():
    """Compte et taille totale des fichiers dans tous les Tri/Supprimées/ des sources actives."""
    if not _is_configured():
        return jsonify({"count": 0, "size_bytes": 0, "size_human": "0 B", "trash_dirs": []})
    count = 0
    size  = 0
    dirs  = []
    for src in _sources():
        td = src / "Tri" / "Supprimées"
        if not td.exists():
            continue
        dir_count = 0
        dir_size  = 0
        for f in td.rglob("*"):
            if f.is_file():
                count     += 1
                dir_count += 1
                try:
                    sz = f.stat().st_size
                    size     += sz
                    dir_size += sz
                except Exception:
                    pass
        if dir_count > 0:
            dirs.append({"path": str(td), "count": dir_count, "size_bytes": dir_size})

    def _human(b):
        for unit in ("B", "KB", "MB", "GB"):
            if b < 1024:
                return f"{b:.1f} {unit}"
            b /= 1024
        return f"{b:.1f} TB"

    return jsonify({"count": count, "size_bytes": size, "size_human": _human(size), "trash_dirs": dirs})

@app.route("/api/empty_trash", methods=["POST"])
def api_empty_trash():
    """Envoie tous les fichiers de Tri/Supprimées/ à la Corbeille macOS (réversible)."""
    if not _is_configured():
        return jsonify({"ok": False, "error": "session non configurée"}), 400
    try:
        from send2trash import send2trash
    except ImportError:
        return jsonify({"ok": False, "error": "send2trash non installé"}), 500

    moved  = 0
    failed = []
    for src in _sources():
        td = src / "Tri" / "Supprimées"
        if not td.exists():
            continue
        for f in list(td.rglob("*")):
            if not f.is_file():
                continue
            try:
                send2trash(str(f))
                moved += 1
            except Exception as e:
                failed.append({"path": str(f), "error": str(e)})
        # Vider aussi les sous-dossiers vides
        try:
            for sub in sorted(td.rglob("*"), reverse=True):
                if sub.is_dir() and not any(sub.iterdir()):
                    sub.rmdir()
        except Exception:
            pass

    return jsonify({"ok": True, "moved": moved, "failed": failed,
                    "message": f"{moved} fichier(s) envoyé(s) à la Corbeille macOS."})

# ── Conversion / compression (v0.4.0) ────────────────────────────────────────
CONVERT_PRESETS = {
    "lossless": {"label": "Sans perte",  "webp_quality": 90, "x265_crf": 22, "x265_preset": "medium"},
    "balanced": {"label": "Équilibré",   "webp_quality": 82, "x265_crf": 25, "x265_preset": "medium"},
    "compact":  {"label": "Compact",     "webp_quality": 72, "x265_crf": 28, "x265_preset": "medium"},
}

_convert_status = {
    "running": False, "done": False, "cancelled": False,
    "total": 0, "current": 0,
    "current_file": "", "preset": "",
    "bytes_before": 0, "bytes_after": 0,
    "converted": 0, "skipped": 0, "errors": [],
    "started_at": None, "finished_at": None,
}
_convert_lock = threading.Lock()
_convert_thread = None
MIN_CONVERT_SIZE = 50_000  # ne pas convertir les fichiers < 50 KB (gain négligeable)

def _collect_convertible_files():
    """Liste tous les médias des sources hors Tri/ et _a_supprimer/."""
    if not _is_configured():
        return []
    files = []
    skip_dirs = {"Tri", "_a_supprimer"}
    for src in _sources():
        if not src.exists():
            continue
        for f in src.rglob("*"):
            if not f.is_file():
                continue
            if f.suffix.lower() not in MEDIA_EXT:
                continue
            if f.name.startswith(".") or f.name.endswith("-overlay.png"):
                continue
            try:
                rel_parts = f.relative_to(src).parts
            except Exception:
                continue
            if any(part in skip_dirs for part in rel_parts):
                continue
            files.append(f)
    return files

def _video_codec(p: Path) -> str:
    try:
        r = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=codec_name", "-of", "csv=p=0", str(p)],
            capture_output=True, text=True, timeout=10)
        return r.stdout.strip().lower()
    except Exception:
        return ""

def _raw_exif_bytes(p: Path):
    """Construit un bloc EXIF minimal (DateTimeOriginal) pour un RAW, à réinjecter
    dans le WebP de sortie. Retourne bytes | None."""
    dt = _exif_datetime(p)
    if not dt:
        return None
    try:
        exif = Image.Exif()
        exif[36867] = dt.strftime("%Y:%m:%d %H:%M:%S")  # DateTimeOriginal
        exif[306]   = dt.strftime("%Y:%m:%d %H:%M:%S")   # DateTime
        return exif.tobytes()
    except Exception:
        return None


def _convert_one(p: Path, preset: dict):
    """Convertit un fichier en préservant ses métadonnées. Retourne (before, after, status, err)."""
    ext = p.suffix.lower()
    st  = p.stat()
    size_before = st.st_size
    orig_times  = (st.st_atime, st.st_mtime)

    def _preserve_times(target: Path):
        # Filet de sécurité : le mtime d'origine reste la date affichée même si une
        # métadonnée interne manquait. C'est la garantie anti « tout en 2026 ».
        try:
            os.utime(target, orig_times)
        except Exception:
            pass

    if size_before < MIN_CONVERT_SIZE:
        return size_before, size_before, "skipped", "trop petit"

    if ext in IMAGE_EXT:
        if ext == ".webp":
            return size_before, size_before, "skipped", "déjà WebP"
        try:
            if ext in RAW_EXT:
                if not RAW_AVAILABLE:
                    return size_before, size_before, "skipped", "rawpy indisponible"
                with _rawpy.imread(str(p)) as raw:
                    rgb = raw.postprocess(use_camera_wb=True)
                img = Image.fromarray(rgb)
                exif = _raw_exif_bytes(p)
                icc = xmp = None
            else:
                img  = Image.open(p)
                # Métadonnées à transporter vers le WebP AVANT toute conversion de mode.
                exif = img.info.get("exif")
                icc  = img.info.get("icc_profile")
                xmp  = img.info.get("xmp")
                if img.mode in ("RGBA", "P", "LA"):
                    bg = Image.new("RGB", img.size, (0, 0, 0))
                    if img.mode == "P":
                        img = img.convert("RGBA")
                    if img.mode in ("RGBA", "LA"):
                        bg.paste(img, mask=img.split()[-1])
                        img = bg
                elif img.mode != "RGB":
                    img = img.convert("RGB")

            target = p.with_suffix(".webp")
            tmp = target.with_name("_conv_" + target.name)
            save_kwargs = {"quality": preset["webp_quality"], "method": 6}
            for k, v in {"exif": exif, "icc_profile": icc, "xmp": xmp}.items():
                if v:
                    save_kwargs[k] = v
            img.save(tmp, "WEBP", **save_kwargs)
            size_after = tmp.stat().st_size
            if size_after >= size_before * 0.95:
                tmp.unlink(missing_ok=True)
                return size_before, size_before, "skipped", "pas de gain (≤5%)"
            p.unlink()
            tmp.rename(target)
            _preserve_times(target)
            return size_before, size_after, "converted", None
        except Exception as e:
            return size_before, size_before, "error", str(e)[:200]

    if ext in VIDEO_EXT:
        codec = _video_codec(p)
        if codec in ("hevc", "h265"):
            return size_before, size_before, "skipped", "déjà H.265"
        tmp = p.with_name(f"_conv_{p.stem}.mp4")
        try:
            r = subprocess.run([
                "ffmpeg", "-y", "-i", str(p),
                "-c:v", "libx265", "-crf", str(preset["x265_crf"]),
                "-preset", preset["x265_preset"],
                "-c:a", "aac", "-b:a", "128k",
                "-tag:v", "hvc1",
                "-map_metadata", "0",          # copie creation_time + tags conteneur
                "-movflags", "use_metadata_tags",
                str(tmp),
            ], capture_output=True, timeout=1800)
            if r.returncode != 0:
                tmp.unlink(missing_ok=True)
                return size_before, size_before, "error", r.stderr.decode()[-200:]
            size_after = tmp.stat().st_size
            if size_after >= size_before * 0.95:
                tmp.unlink(missing_ok=True)
                return size_before, size_before, "skipped", "pas de gain (≤5%)"
            new_path = p.with_suffix(".mp4")
            p.unlink()
            tmp.rename(new_path)
            _preserve_times(new_path)
            return size_before, size_after, "converted", None
        except subprocess.TimeoutExpired:
            tmp.unlink(missing_ok=True)
            return size_before, size_before, "error", "timeout (>30 min)"
        except Exception as e:
            tmp.unlink(missing_ok=True)
            return size_before, size_before, "error", str(e)[:200]

    return size_before, size_before, "skipped", "format non supporté"

def _convert_worker(preset_key: str):
    global _convert_status
    import datetime as _dt
    preset = CONVERT_PRESETS.get(preset_key)
    files = _collect_convertible_files()
    with _convert_lock:
        _convert_status.update({
            "running": True, "done": False, "cancelled": False,
            "total": len(files), "current": 0,
            "current_file": "", "preset": preset_key,
            "bytes_before": 0, "bytes_after": 0,
            "converted": 0, "skipped": 0, "errors": [],
            "started_at": _dt.datetime.now().isoformat(), "finished_at": None,
        })

    for i, f in enumerate(files):
        with _convert_lock:
            if _convert_status["cancelled"]:
                break
            _convert_status["current"] = i + 1
            _convert_status["current_file"] = f.name

        size_before, size_after, status, err = _convert_one(f, preset)

        with _convert_lock:
            _convert_status["bytes_before"] += size_before
            _convert_status["bytes_after"]  += size_after
            if status == "converted":
                _convert_status["converted"] += 1
            elif status == "skipped":
                _convert_status["skipped"] += 1
            elif status == "error":
                _convert_status["errors"].append({"file": f.name, "error": err})

    with _convert_lock:
        _convert_status["running"] = False
        _convert_status["done"] = True
        _convert_status["finished_at"] = _dt.datetime.now().isoformat()

    # Si une session existait, invalider son STATE_FILE pour que /api/config relise
    # la liste des fichiers (extensions ont changé jpg→webp, mp4 peut être renommé).
    if _is_configured() and _convert_status["converted"] > 0:
        try:
            STATE_FILE.unlink(missing_ok=True)
        except Exception:
            pass
        _reset_mem()

@app.route("/api/convert/preview", methods=["GET"])
def api_convert_preview():
    files = _collect_convertible_files()
    total = sum(f.stat().st_size for f in files if f.exists())
    def _human(b):
        for u in ("B", "KB", "MB", "GB"):
            if b < 1024:
                return f"{b:.1f} {u}"
            b /= 1024
        return f"{b:.1f} TB"
    return jsonify({
        "count":      len(files),
        "size_bytes": total,
        "size_human": _human(total),
        "presets":    {k: {"label": v["label"]} for k, v in CONVERT_PRESETS.items()},
    })

@app.route("/api/convert/start", methods=["POST"])
def api_convert_start():
    global _convert_thread
    if not _is_configured():
        return jsonify({"ok": False, "error": "Ajoute au moins un dossier source d'abord."}), 400
    with _convert_lock:
        if _convert_status["running"]:
            return jsonify({"ok": False, "error": "Conversion déjà en cours."}), 409
    data = request.get_json() or {}
    preset_key = data.get("preset")
    if preset_key not in CONVERT_PRESETS:
        return jsonify({"ok": False, "error": f"Preset invalide. Choix : {list(CONVERT_PRESETS)}"}), 400
    _convert_thread = threading.Thread(target=_convert_worker, args=(preset_key,), daemon=True)
    _convert_thread.start()
    return jsonify({"ok": True, "preset": preset_key})

@app.route("/api/convert/status", methods=["GET"])
def api_convert_status():
    with _convert_lock:
        st = dict(_convert_status)
    def _human(b):
        for u in ("B", "KB", "MB", "GB"):
            if abs(b) < 1024:
                return f"{b:.1f} {u}"
            b /= 1024
        return f"{b:.1f} TB"
    saved = st["bytes_before"] - st["bytes_after"]
    st["bytes_before_h"] = _human(st["bytes_before"])
    st["bytes_after_h"]  = _human(st["bytes_after"])
    st["bytes_saved_h"]  = _human(saved)
    st["bytes_saved"]    = saved
    st["percent"]        = round((st["current"] / st["total"] * 100) if st["total"] else 0)
    return jsonify(st)

@app.route("/api/convert/cancel", methods=["POST"])
def api_convert_cancel():
    with _convert_lock:
        if not _convert_status["running"]:
            return jsonify({"ok": False, "error": "Pas de conversion en cours."}), 400
        _convert_status["cancelled"] = True
    return jsonify({"ok": True})

@app.route("/api/pick_folder", methods=["POST"])
def api_pick_folder():
    """Ouvre un dialog FOLDER macOS natif via pywebview. Renvoie le chemin sélectionné.

    Disponible UNIQUEMENT en contexte pywebview (app.py). En dev browser, renvoie une erreur :
    l'utilisateur doit alors coller le chemin à la main dans le champ texte.
    """
    if _main_window is None:
        return jsonify({"ok": False, "error": "Dialog natif indisponible (mode dev browser). Colle le chemin à la main."}), 503
    try:
        # Import local pour éviter dépendance dure côté core (pywebview = côté app.py)
        import webview
        dialog_type = getattr(getattr(webview, "FileDialog", None), "FOLDER", None)
        if dialog_type is None:
            dialog_type = getattr(webview, "FOLDER_DIALOG", 2)
        result = _main_window.create_file_dialog(
            dialog_type,
            allow_multiple=False,
            directory=str(Path.home()),
        )
        if not result:
            return jsonify({"ok": False, "cancelled": True})
        return jsonify({"ok": True, "path": str(Path(result[0]).expanduser().resolve())})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 500

@app.route("/media/<path:entry>")
def serve_media(entry):
    """Sert un fichier média. `entry` est de la forme '<src_idx>::<rel>'.

    Le '::' peut arriver URL-encodé en %3A%3A (Flask le décode automatiquement).
    Pour la rétrocompat single-source, '<rel>' sans '::' est aussi accepté.
    """
    return send_file(str(_entry_path(entry)))


_PREVIEW_DIR = _NS_DIR / "previews"
_PREVIEW_DIR.mkdir(parents=True, exist_ok=True)


def _build_preview(p: Path, out: Path) -> bool:
    """Génère un JPEG d'aperçu (max 1280px) pour un format non rendu par le navigateur.
    Images RAW/TIFF via rawpy/PIL, vidéos non-MP4 via poster frame ffmpeg. True si OK."""
    ext = p.suffix.lower()
    try:
        if ext in IMAGE_EXT:
            if ext in RAW_EXT:
                if not RAW_AVAILABLE:
                    return False
                with _rawpy.imread(str(p)) as raw:
                    rgb = raw.postprocess(use_camera_wb=True, half_size=True)
                img = Image.fromarray(rgb)
            else:
                img = Image.open(p)
                if img.mode != "RGB":
                    img = img.convert("RGB")
            img.thumbnail((1280, 1280))
            img.save(out, "JPEG", quality=85)
            return out.exists()
        if ext in VIDEO_EXT:
            r = subprocess.run([
                "ffmpeg", "-y", "-ss", "1", "-i", str(p),
                "-frames:v", "1", "-vf", "scale='min(1280,iw)':-2",
                "-q:v", "4", str(out),
            ], capture_output=True, timeout=30)
            return r.returncode == 0 and out.exists()
    except Exception:
        return False
    return False


@app.route("/preview/<path:entry>")
def serve_preview(entry):
    """JPEG d'aperçu pour les formats non affichables nativement (RAW, AVI/MKV…).
    Mis en cache dans previews/, invalidé sur changement de mtime."""
    p = _entry_path(entry)
    if not p.exists():
        return jsonify({"error": "not found"}), 404
    try:
        sig = f"{p}:{p.stat().st_mtime_ns}"
    except Exception:
        sig = str(p)
    key = _hashlib.sha1(sig.encode()).hexdigest()[:16]
    out = _PREVIEW_DIR / f"{key}.jpg"
    if not out.exists():
        if not _build_preview(p, out):
            return jsonify({"error": "preview failed"}), 415
    return send_file(str(out), mimetype="image/jpeg")


@app.route("/api/gallery")
def api_gallery():
    """Galerie de médias, triable.

    Params :
    - sort  : size_desc|size_asc|date_desc|date_asc|name
    - scope : 'all' (défaut, tous les médias) | 'queue' (uniquement les médias
              restant à trier, clés `rel` compatibles /api/gallery_action)
    - type  : 'all' (défaut) | 'photo' | 'video'
    """
    if not _is_configured():
        return jsonify({"ok": False, "error": "session non configurée"}), 400
    scope = request.args.get("scope", "all")
    ftype = request.args.get("type", "all")
    sort  = request.args.get("sort", "date_asc" if scope == "queue" else "size_desc")

    if scope == "queue":
        s        = load_state()
        files    = s.get("files", [])
        idx      = s.get("current", 0)
        entries  = [f for f in files[idx:] if _entry_path(f).exists()]
    else:
        entries = collect_files()
    if ftype in ("photo", "video"):
        entries = [e for e in entries if _entry_matches_filter(e, ftype)]

    items = []
    for entry in entries:
        rel_only = _entry_rel(entry)
        try:
            sz = _entry_path(entry).stat().st_size
        except Exception:
            sz = 0
        dt = _capture_datetime(entry)
        items.append({
            "rel":         entry,
            "url":         f"/media/{entry}",
            "preview_url": None if _is_web_renderable(rel_only) else f"/preview/{entry}",
            "is_video":    Path(rel_only).suffix.lower() in VIDEO_EXT,
            "size_kb":     round(sz / 1024),
            "size_bytes":  sz,
            "year":        str(dt.year) if dt else "—",
            "ts":          dt.timestamp() if dt else 0,
            "name":        Path(rel_only).name,
        })
    keyfns = {
        "size_desc": (lambda it: it["size_bytes"], True),
        "size_asc":  (lambda it: it["size_bytes"], False),
        "date_desc": (lambda it: it["ts"], True),
        "date_asc":  (lambda it: it["ts"], False),
        "name":      (lambda it: it["name"].lower(), False),
    }
    keyfn, rev = keyfns.get(sort, keyfns["size_desc"])
    items.sort(key=keyfn, reverse=rev)
    total_bytes = sum(it["size_bytes"] for it in items)
    return jsonify({"ok": True, "sort": sort, "scope": scope, "type": ftype,
                    "count": len(items),
                    "total_mb": round(total_bytes / (1024 * 1024), 1), "items": items})

@app.route("/api/gallery_action", methods=["POST"])
def api_gallery_action():
    """Applique un tri par lot depuis la galerie : {keep:[entries], trash:[entries]}.

    Réutilise move_to_gardes / trash_file et journalise chaque opération dans
    l'historique (l'undo standard de /api/action reste fonctionnel).
    """
    if not _is_configured():
        return jsonify({"ok": False, "error": "session non configurée"}), 400
    data       = request.get_json() or {}
    keep_list  = data.get("keep", []) or []
    trash_list = data.get("trash", []) or []
    s          = load_state()
    files_set  = set(s["files"])
    processed  = 0

    for rel in keep_list:
        if rel not in files_set:
            continue
        ov     = find_overlay(rel)
        result = move_to_gardes(rel, ov, True)
        s["history"].append({"action": "keep", "file": rel,
                             "kept_path": result["kept_path"]})
        processed += 1

    for rel in trash_list:
        if rel not in files_set:
            continue
        dst   = trash_file(rel)
        entry = {"action": "trash", "file": rel, "trash_path": dst}
        ov    = find_overlay(rel)
        if ov:
            entry["overlay_rel"]        = ov
            entry["overlay_trash_path"] = trash_file(ov)
        s["history"].append(entry)
        processed += 1

    # Retirer de la file les fichiers traités (déplacés sur disque).
    done_set   = set(keep_list) | set(trash_list)
    s["files"] = [f for f in s["files"] if f not in done_set]
    s["current"] = min(s["current"], len(s["files"]))
    save_state(s)
    return jsonify({"ok": True, "processed": processed})

@app.route("/api/reorganize", methods=["POST"])
def api_reorganize():
    """Deprecated en v0.2.0 : les actions keep/trash déplacent immédiatement.

    Plus de "queue" à appliquer en différé — l'historique sert uniquement à l'undo.
    """
    return jsonify({"ok": False, "deprecated": True,
                    "message": "Les actions sont appliquées immédiatement depuis v0.2.0."}), 410

# ──────────────────────────────────────────────────────────────────────────────
# AUTO-UPDATE (v0.5.0+)
# ──────────────────────────────────────────────────────────────────────────────

_update_lock = threading.Lock()
_update_state = {
    "checked":     False,        # True dès qu'un check (réussi ou non) a été fait
    "checking":    False,        # True pendant le fetch GitHub API
    "ok":          False,        # True si le dernier check a abouti
    "current":     "",           # version courante
    "latest":      None,         # tag de la dernière release
    "available":   False,        # update dispo ?
    "url":         None,         # URL du zip
    "size":        None,         # bytes
    "notes":       "",           # release notes
    "error":       None,         # message d'erreur si check a échoué
    "downloading": False,        # download en cours
    "dl_done":     0,            # bytes téléchargés
    "dl_total":    0,            # total bytes
    "installing":  False,        # extract + relauncher en cours
    "install_err": None,         # erreur d'install
}

def _do_check_update():
    """Worker : appelle l'API GitHub et met à jour _update_state."""
    with _update_lock:
        _update_state["checking"] = True
        _update_state["error"]    = None
    try:
        result = _updater.check_latest()
    except Exception as e:
        with _update_lock:
            _update_state["checking"] = False
            _update_state["checked"]  = True
            _update_state["ok"]       = False
            _update_state["error"]    = f"Exception: {str(e)[:120]}"
        return
    with _update_lock:
        _update_state.update({
            "checking":  False,
            "checked":   True,
            "ok":        result["ok"],
            "current":   result["current"],
            "latest":    result["latest"],
            "available": result["available"],
            "url":       result["url"],
            "size":      result["size"],
            "notes":     result["notes"],
            "error":     result["error"],
        })

def _kickoff_startup_check():
    """À appeler depuis app.py au démarrage : check silencieux en thread daemon."""
    t = threading.Thread(target=_do_check_update, daemon=True, name="update-check")
    t.start()

def _do_install_update():
    """Worker : download + extract + relauncher + sys.exit. NE REVIENT JAMAIS."""
    with _update_lock:
        url = _update_state["url"]
    if not url:
        with _update_lock:
            _update_state["installing"]  = False
            _update_state["install_err"] = "URL de téléchargement manquante (relancez la vérification)."
        return

    def progress(done, total):
        with _update_lock:
            _update_state["dl_done"]  = done
            _update_state["dl_total"] = total

    try:
        with _update_lock:
            _update_state["downloading"] = True
            _update_state["installing"]  = True
            _update_state["install_err"] = None
        zip_path = Path(tempfile.gettempdir()) / "sort-memories-update.zip"
        _updater.download_release(url, zip_path, progress_cb=progress)
        with _update_lock:
            _update_state["downloading"] = False
        # install_release ne revient pas (os._exit)
        _updater.install_release(zip_path)
    except Exception as e:
        with _update_lock:
            _update_state["downloading"] = False
            _update_state["installing"]  = False
            _update_state["install_err"] = str(e)[:200]

@app.route("/api/update/check", methods=["POST"])
def api_update_check():
    """Déclenche un check (async). L'UI poll /api/update/status."""
    with _update_lock:
        if _update_state["checking"] or _update_state["installing"]:
            return jsonify({"ok": False, "busy": True}), 409
    t = threading.Thread(target=_do_check_update, daemon=True, name="update-check-manual")
    t.start()
    return jsonify({"ok": True, "triggered": True})

@app.route("/api/update/status", methods=["GET"])
def api_update_status():
    """État courant de l'updater (check + download + install). Poll-friendly."""
    with _update_lock:
        return jsonify(dict(_update_state))

@app.route("/api/update/install", methods=["POST"])
def api_update_install():
    """Déclenche le download + install + relauncher. L'app va quitter."""
    with _update_lock:
        if not _update_state.get("available") or not _update_state.get("url"):
            return jsonify({"ok": False, "error": "Aucune mise à jour disponible."}), 400
        if _update_state["installing"] or _update_state["downloading"]:
            return jsonify({"ok": False, "busy": True}), 409
    t = threading.Thread(target=_do_install_update, daemon=True, name="update-install")
    t.start()
    return jsonify({"ok": True, "triggered": True})

# ──────────────────────────────────────────────────────────────────────────────
# PAGE HTML
# ──────────────────────────────────────────────────────────────────────────────

PAGE = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>Triage Snapchat</title>
<style>
  /* ════════════════════════════════════════════════════════════════
     Sort Memories — design system « Calme & Pro » (v0.8.0)
     Direction : sombre neutre type Apple Photos. Le média est la star,
     l'UI s'efface. Hiérarchie lisible, gros gestes garder/supprimer,
     undo toujours visible, feedback rassurant.
     ════════════════════════════════════════════════════════════════ */
  :root {
    /* Surfaces (échelle de gris neutre, proche Apple dark) */
    --bg:        #0a0a0b;
    --bg-1:      #161618;   /* barres, surfaces élevées */
    --bg-2:      #1f1f22;   /* contrôles, hover */
    --bg-3:      #2a2a2e;   /* hover fort, inputs */
    --line:      rgba(255,255,255,.08);
    --line-2:    rgba(255,255,255,.14);
    /* Texte */
    --txt:       #f5f5f7;
    --txt-2:     #aeaeb2;
    --txt-3:     #8e8e93;
    --txt-4:     #7c7c82;   /* relevé pour passer WCAG AA (≈4.6:1 sur --bg) */
    /* Accents sémantiques (couleurs système Apple) */
    --keep:      #30d158;
    --keep-bg:   rgba(48,209,88,.16);
    --keep-line: rgba(48,209,88,.42);
    --del:       #ff453a;
    --del-bg:    rgba(255,69,58,.16);
    --del-line:  rgba(255,69,58,.42);
    --blue:      #0a84ff;
    --blue-bg:   rgba(10,132,255,.16);
    --amber:     #ff9f0a;
    --amber-bg:  rgba(255,159,10,.15);
    --violet:    #bf5af2;
    --violet-bg: rgba(191,90,242,.16);
    /* Rayons, ombres, motion */
    --r-sm: 8px; --r-md: 12px; --r-lg: 16px; --r-pill: 999px;
    --shadow: 0 16px 50px rgba(0,0,0,.55), 0 2px 8px rgba(0,0,0,.4);
    --shadow-sm: 0 6px 24px rgba(0,0,0,.4);
    --ease: cubic-bezier(.2,.7,.2,1);
    --dur: .22s;
  }

  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  html, body {
    height: 100%; background: var(--bg); color: var(--txt);
    font-family: -apple-system, BlinkMacSystemFont, "SF Pro Text", "Segoe UI", sans-serif;
    font-size: 14px; line-height: 1.45; letter-spacing: -.01em;
    -webkit-font-smoothing: antialiased; text-rendering: optimizeLegibility;
    display: flex; flex-direction: column; overflow: hidden;
  }
  ::selection { background: rgba(10,132,255,.35); }
  @media (prefers-reduced-motion: reduce) {
    *, *::before, *::after { animation-duration: .001ms !important; transition-duration: .001ms !important; }
  }

  /* Base bouton neutre — chaque composant affine ensuite */
  button {
    font-family: inherit; color: var(--txt);
    display: inline-flex; align-items: center; justify-content: center; gap: 7px;
    border: none; border-radius: var(--r-sm); padding: 9px 15px;
    font-size: 13px; font-weight: 600; cursor: pointer; flex-shrink: 0;
    transition: background var(--dur) var(--ease), border-color var(--dur), color var(--dur), opacity .15s, transform .08s;
  }
  button:active:not(:disabled) { transform: scale(.97); }
  button:disabled { opacity: .35; cursor: default; }
  button:focus-visible, .tf-btn:focus-visible, input:focus-visible {
    outline: 2px solid var(--blue); outline-offset: 2px;
  }
  .key {
    font-size: 10px; font-weight: 600; opacity: .75;
    background: rgba(255,255,255,.09); border-radius: 5px; padding: 1px 5px;
    font-variant-numeric: tabular-nums; line-height: 1.4;
  }

  /* ── Barre de progression (fil très fin tout en haut) ── */
  #progress-wrap { height: 2px; background: rgba(255,255,255,.06); flex-shrink: 0; }
  #progress-bar  { height: 100%; background: var(--keep); width: 0; transition: width .35s var(--ease); }

  /* ── Topbar : contexte du média + utilitaires (calme, translucide) ── */
  #topbar {
    flex-shrink: 0; display: none; align-items: center; gap: 12px;
    padding: 10px 18px; min-height: 52px;
    background: rgba(16,16,18,.72); backdrop-filter: saturate(180%) blur(20px);
    border-bottom: 1px solid var(--line);
  }
  #topbar.show { display: flex; }
  #topbar .tb-left  { display: flex; align-items: center; gap: 10px; flex: 1; min-width: 0; }
  #topbar .tb-right { display: flex; align-items: center; gap: 8px; }
  #info { min-width: 0; }
  #info .name { font-size: 13px; font-weight: 600; color: var(--txt); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  #info .meta { font-size: 11.5px; color: var(--txt-3); margin-top: 1px; font-variant-numeric: tabular-nums; }

  /* Bouton outil discret (ghost) */
  .toolbtn {
    background: transparent; border: 1px solid var(--line); color: var(--txt-2);
    border-radius: var(--r-sm); padding: 7px 12px; font-size: 12.5px; font-weight: 500;
  }
  .toolbtn:hover:not(:disabled) { background: var(--bg-2); color: var(--txt); border-color: var(--line-2); }
  .toolbtn.icon { padding: 7px 10px; }

  /* ── Stage (média en mode single) ── */
  #stage {
    flex: 1; display: flex; align-items: center; justify-content: center;
    overflow: hidden; position: relative; padding: 28px; min-height: 0;
  }
  #media-wrap {
    position: relative; display: inline-flex; align-items: center; justify-content: center;
    max-width: 100%; max-height: 100%; cursor: pointer;
  }
  #main-img {
    display: block; max-width: 100%; max-height: calc(100vh - 200px);
    object-fit: contain; border-radius: var(--r-md); cursor: default;
    box-shadow: var(--shadow);
  }
  #main-video {
    display: block; max-width: 100%; max-height: calc(100vh - 220px);
    object-fit: contain; border-radius: var(--r-md); background: #000;
    box-shadow: var(--shadow);
  }
  #overlay-img {
    position: absolute; top: 0; left: 0; width: 100%; height: 100%;
    object-fit: contain; pointer-events: none;
    transition: opacity var(--dur) var(--ease); border-radius: var(--r-md);
  }
  #overlay-img.hidden { opacity: 0; }
  #play-icon {
    position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
    width: 76px; height: 76px; border-radius: 50%;
    background: rgba(0,0,0,.5); backdrop-filter: blur(8px);
    display: flex; align-items: center; justify-content: center;
    font-size: 34px; color: #fff; opacity: 0; pointer-events: none;
    transition: opacity .2s var(--ease), transform .2s var(--ease);
  }
  #play-icon.show { opacity: 1; }
  #overlay-badge {
    position: absolute; top: 12px; right: 12px;
    background: rgba(10,132,255,.9); color: #fff;
    font-size: 11px; font-weight: 600; padding: 4px 11px;
    border-radius: var(--r-pill); pointer-events: none; transition: opacity .2s;
  }
  #overlay-badge.hidden { opacity: 0; }

  /* ── Mode doublons (pHash) ── */
  #group-stage {
    flex: 1; display: none; flex-direction: column;
    padding: 16px 20px; gap: 14px; min-height: 0; overflow: hidden;
  }
  #group-header, #sem-header {
    flex-shrink: 0; display: flex; align-items: center; gap: 12px;
    font-size: 13px; color: var(--txt-2);
  }
  #group-count, #sem-count { font-weight: 600; color: var(--txt); }
  #sim-badge { padding: 4px 12px; border-radius: var(--r-pill); font-size: 12px; font-weight: 700; }
  .sim-green  { background: var(--keep-bg);   color: var(--keep); }
  .sim-orange { background: var(--amber-bg);  color: var(--amber); }
  .sim-red    { background: var(--del-bg);    color: var(--del); }

  #group-grid, #sem-grid {
    flex: 1; display: grid; gap: 14px; overflow: auto; min-height: 0; align-content: start;
  }
  /* Cellules du mode doublons / sémantique (scopées pour éviter collision galerie) */
  #group-grid .g-cell, .s-cell {
    display: flex; flex-direction: column; align-items: center;
    background: var(--bg-1); border: 2px solid var(--line);
    border-radius: var(--r-md); padding: 10px;
    cursor: pointer; transition: border-color var(--dur), background var(--dur), transform .12s var(--ease);
    overflow: hidden; min-height: 0; user-select: none;
  }
  #group-grid .g-cell:hover { border-color: var(--keep-line); background: var(--keep-bg); transform: translateY(-2px); }
  #group-grid .g-cell:hover .g-keep-hint { opacity: 1; }
  .s-cell.keep  { border-color: var(--keep-line); background: var(--keep-bg); }
  .s-cell.trash { border-color: var(--del-line);  background: var(--del-bg); }
  .s-cell:not(.keep):not(.trash):hover { border-color: var(--violet); background: var(--violet-bg); }
  .g-media-wrap {
    flex: 1; display: flex; align-items: center; justify-content: center;
    overflow: hidden; width: 100%; min-height: 0; position: relative;
  }
  .g-img, .g-video {
    max-width: 100%; max-height: 100%; object-fit: contain; border-radius: var(--r-sm); display: block;
  }
  .g-video { background: #000; }
  .g-overlay {
    position: absolute; top: 0; left: 0; width: 100%; height: 100%;
    object-fit: contain; pointer-events: none; border-radius: var(--r-sm);
  }
  .g-meta {
    flex-shrink: 0; display: flex; gap: 6px; flex-wrap: wrap; justify-content: center;
    padding: 8px 0 2px; font-size: 10.5px; color: var(--txt-3);
  }
  .g-meta span { background: var(--bg-2); padding: 2px 7px; border-radius: var(--r-pill); }
  .g-keep-hint {
    position: absolute; top: 8px; left: 50%; transform: translateX(-50%);
    background: var(--keep); color: #04130a; font-size: 11px; font-weight: 700;
    padding: 4px 11px; border-radius: var(--r-pill); opacity: 0;
    transition: opacity .15s; pointer-events: none; white-space: nowrap;
  }
  .s-cell-badge {
    position: absolute; top: 8px; left: 50%; transform: translateX(-50%);
    font-size: 11px; font-weight: 700; padding: 4px 11px; border-radius: var(--r-pill);
    pointer-events: none; white-space: nowrap;
  }
  .s-cell.keep  .s-cell-badge { background: var(--keep); color: #04130a; }
  .s-cell.trash .s-cell-badge { background: var(--del);  color: #fff; }
  .s-cell:not(.keep):not(.trash) .s-cell-badge { display: none; }
  .s-best-badge { font-size: 10px; color: var(--violet); background: var(--violet-bg); padding: 2px 7px; border-radius: var(--r-sm); }
  #sem-label-badge { padding: 4px 12px; border-radius: var(--r-pill); font-size: 12px; font-weight: 700; background: var(--violet-bg); color: var(--violet); }
  #sem-decision-bar { display: flex; align-items: center; gap: 12px; font-size: 12px; color: var(--txt-3); }
  #sem-counts { font-variant-numeric: tabular-nums; }
  #btn-sem-validate { background: var(--violet); color: #fff; min-width: 140px; }
  #btn-sem-validate:disabled { opacity: .3; }

  /* ── Contrôles vidéo custom (single) ── */
  #video-ctrl {
    flex-shrink: 0; display: none; align-items: center; gap: 12px;
    background: var(--bg-1); border-top: 1px solid var(--line); padding: 8px 20px; height: 44px;
  }
  #video-ctrl.on { display: flex; }
  #vc-pp { background: none; border: none; color: var(--txt); font-size: 16px; cursor: pointer; padding: 0; width: 24px; }
  #vc-prog-wrap { flex: 1; height: 5px; background: var(--bg-3); border-radius: var(--r-pill); cursor: pointer; position: relative; }
  #vc-prog-fill { height: 100%; background: var(--txt); border-radius: var(--r-pill); pointer-events: none; width: 0; }
  #vc-time { font-size: 11.5px; color: var(--txt-3); min-width: 74px; text-align: right; font-variant-numeric: tabular-nums; }
  #vc-mute { background: none; border: none; color: var(--txt-2); font-size: 15px; cursor: pointer; padding: 0; }
  #vc-vol  { width: 70px; accent-color: var(--txt-2); cursor: pointer; }

  /* ── Barre d'action principale (bas) ── */
  #bar {
    flex-shrink: 0; background: rgba(16,16,18,.82); backdrop-filter: saturate(180%) blur(20px);
    border-top: 1px solid var(--line); padding: 14px 18px;
    display: flex; align-items: center; gap: 10px;
  }
  #bar .bar-spacer { flex: 1; }
  #bar .bar-tools  { display: flex; align-items: center; gap: 8px; }

  /* Actions primaires garder / supprimer : grandes, claires, rassurantes */
  #btn-keep, #btn-trash {
    min-width: 150px; padding: 13px 22px; font-size: 14px; font-weight: 700;
    border-radius: var(--r-md); gap: 9px;
  }
  #btn-keep  { background: var(--keep); color: #04130a; }
  #btn-keep:hover:not(:disabled)  { background: #28c14e; }
  #btn-trash { background: var(--del); color: #fff; }
  #btn-trash:hover:not(:disabled) { background: #f5392d; }
  #btn-keep .key  { background: rgba(0,0,0,.16); opacity: .9; }
  #btn-trash .key { background: rgba(0,0,0,.22); color: #fff; opacity: .9; }

  /* Undo : présent et lisible, jamais agressif */
  #btn-back {
    background: var(--bg-2); color: var(--txt); border: 1px solid var(--line);
    border-radius: var(--r-md); padding: 11px 16px; font-weight: 600;
  }
  #btn-back:hover:not(:disabled) { background: var(--bg-3); border-color: var(--line-2); }

  /* Outils média (rotation, rogner, overlay) : ghost discrets */
  #btn-rotate, #btn-crop, #btn-overlay {
    background: transparent; border: 1px solid var(--line); color: var(--txt-2);
    border-radius: var(--r-sm); padding: 10px 13px; font-weight: 500; font-size: 12.5px;
  }
  #btn-rotate:hover, #btn-crop:hover, #btn-overlay:hover { background: var(--bg-2); color: var(--txt); border-color: var(--line-2); }
  #btn-overlay { color: var(--blue); border-color: var(--blue-bg); }
  #btn-overlay.off { color: var(--txt-4); border-color: var(--line); }

  /* Actions de groupe */
  #btn-keep-all  { background: var(--keep); color: #04130a; min-width: 150px; padding: 13px 22px; font-size: 14px; font-weight: 700; border-radius: var(--r-md); }
  #btn-keep-all:hover { background: #28c14e; }
  #btn-trash-all { background: var(--del); color: #fff; min-width: 150px; padding: 13px 22px; font-size: 14px; font-weight: 700; border-radius: var(--r-md); }
  #btn-trash-all:hover { background: #f5392d; }
  #btn-sem-validate.in-bar { background: var(--violet); color: #fff; min-width: 150px; padding: 13px 22px; font-size: 14px; border-radius: var(--r-md); }

  /* Utilitaires topbar : accueil, vider, scan, gallery toggle */
  #btn-reset, #btn-empty-trash, #btn-gallery {
    background: transparent; border: 1px solid var(--line); color: var(--txt-2);
    padding: 7px 12px; border-radius: var(--r-sm); font-size: 12.5px; font-weight: 500;
  }
  #btn-reset:hover, #btn-empty-trash:hover, #btn-gallery:hover { background: var(--bg-2); color: var(--txt); border-color: var(--line-2); }
  #btn-empty-trash[data-active="1"] { color: var(--amber); border-color: var(--amber-bg); }
  #btn-empty-trash[data-active="1"]:hover { background: var(--amber-bg); color: var(--amber); }
  #empty-trash-label { font-weight: 600; }
  #scan-indicator { font-size: 11.5px; color: var(--txt-3); }
  #scan-indicator.running { color: var(--blue); }
  #scan-indicator.done    { color: var(--keep); }
  #scan-indicator.error   { color: var(--del); }
  #btn-rescan { background: none; border: 1px solid var(--line); color: var(--txt-3); padding: 5px 9px; font-size: 11px; border-radius: var(--r-sm); }
  #btn-rescan:hover { color: var(--txt); border-color: var(--line-2); }

  .flash { animation: flash .2s var(--ease); }
  @keyframes flash { 0%,100%{opacity:1} 50%{opacity:.4} }

  /* ── Segmented control (filtre Tout / Photos / Vidéos) ── */
  .tf-group {
    display: inline-flex; background: var(--bg-2); border: 1px solid var(--line);
    border-radius: var(--r-sm); padding: 2px; gap: 2px;
  }
  .tf-btn {
    background: transparent; border: none; color: var(--txt-3); cursor: pointer;
    padding: 5px 12px; font-size: 12px; font-weight: 600; border-radius: 6px;
    transition: background .16s var(--ease), color .16s;
  }
  .tf-btn:hover { color: var(--txt); }
  .tf-btn.active { background: var(--bg-3); color: var(--txt); box-shadow: 0 1px 2px rgba(0,0,0,.3); }

  /* ── Écran de bienvenue / configuration ── */
  #welcome {
    position: fixed; inset: 0; z-index: 100; overflow: auto;
    background: radial-gradient(120% 80% at 50% -10%, #16161a 0%, var(--bg) 55%);
    display: none; align-items: flex-start; justify-content: center; padding: 48px 20px;
  }
  #welcome-card {
    max-width: 580px; width: 100%; background: var(--bg-1); border: 1px solid var(--line);
    border-radius: var(--r-lg); padding: 32px; box-shadow: var(--shadow);
  }
  .wc-title { text-align: center; margin-bottom: 28px; }
  .wc-title h1 { margin: 10px 0 5px; font-size: 26px; font-weight: 700; letter-spacing: -.02em; }
  .wc-icon { font-size: 42px; }
  .wc-tag { color: var(--txt-3); font-size: 14px; margin: 0; }
  .wc-section { margin: 24px 0; }
  .wc-section > label { display: block; font-size: 11px; text-transform: uppercase; letter-spacing: .07em; color: var(--txt-3); margin-bottom: 12px; font-weight: 600; }
  #wc-sources { list-style: none; padding: 0; margin: 0 0 10px; }
  #wc-sources li {
    display: flex; align-items: center; gap: 10px; background: var(--bg-2);
    border: 1px solid var(--line); border-radius: var(--r-sm); padding: 10px 14px; margin-bottom: 7px; font-size: 13px;
  }
  #wc-sources .wc-src-name { flex: 1; color: var(--txt); font-weight: 500; }
  #wc-sources .wc-src-path { font-size: 10.5px; color: var(--txt-4); max-width: 240px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  #wc-sources .wc-src-rm { background: transparent; border: 0; color: var(--del); cursor: pointer; padding: 4px 8px; border-radius: 6px; font-size: 16px; line-height: 1; }
  #wc-sources .wc-src-rm:hover { background: var(--del-bg); }
  #wc-add {
    width: 100%; background: var(--bg-2); border: 1px dashed var(--line-2);
    color: var(--txt-2); padding: 12px; border-radius: var(--r-sm); cursor: pointer; font-size: 13px; font-weight: 500;
  }
  #wc-add:hover { background: var(--bg-3); color: var(--txt); border-color: var(--blue); }
  #wc-path-fallback {
    width: 100%; margin-top: 8px; background: var(--bg-2); border: 1px solid var(--line);
    color: var(--txt); padding: 11px; border-radius: var(--r-sm); font-size: 12px; font-family: ui-monospace, monospace;
  }
  .wc-opts { display: grid; grid-template-columns: 1fr 1fr; gap: 8px 12px; }
  .wc-opts label {
    display: flex; align-items: center; gap: 10px; font-size: 13px; color: var(--txt-2);
    background: var(--bg-2); border: 1px solid var(--line); padding: 11px; border-radius: var(--r-sm);
    cursor: pointer; transition: border-color .15s, background .15s;
  }
  .wc-opts label:hover { border-color: var(--line-2); color: var(--txt); }
  .wc-opts input[type=checkbox] { accent-color: var(--blue); margin: 0; width: 16px; height: 16px; }
  .wc-preview { background: var(--bg); border: 1px solid var(--line); border-radius: var(--r-sm); padding: 14px; font-size: 11.5px; color: var(--txt-3); display: flex; flex-direction: column; gap: 5px; }
  .wc-preview code { color: var(--blue); font-family: ui-monospace, monospace; font-size: 11.5px; word-break: break-all; }
  #wc-start {
    width: 100%; padding: 15px; background: var(--keep); color: #04130a;
    border: 0; border-radius: var(--r-md); font-size: 15px; font-weight: 700; cursor: pointer; margin-top: 14px;
    transition: background .15s, transform .08s;
  }
  #wc-start:disabled { background: var(--bg-3); color: var(--txt-4); cursor: not-allowed; }
  #wc-start:not(:disabled):hover { background: #28c14e; }
  #wc-start:not(:disabled):active { transform: scale(.99); }
  #wc-gallery-btn { width: 100%; margin-top: 8px; background: var(--bg-2); color: var(--txt); border: 1px solid var(--line); padding: 12px; border-radius: var(--r-md); font-weight: 600; }
  #wc-gallery-btn:not(:disabled):hover { background: var(--bg-3); }
  .wc-err { color: var(--del); font-size: 12px; margin: 8px 0 0; text-align: center; min-height: 18px; }
  #wc-source-err { color: var(--del); font-size: 11.5px; margin-top: 6px; min-height: 14px; }
  .wc-hint { font-size: 11.5px; color: var(--txt-3); margin: 0 0 10px; line-height: 1.5; }
  .wc-presets { display: flex; flex-direction: column; gap: 7px; }
  .wc-preset {
    display: grid; grid-template-columns: auto 1fr; column-gap: 12px; align-items: center;
    background: var(--bg-2); border: 1px solid var(--line); padding: 11px 13px; border-radius: var(--r-sm);
    cursor: pointer; transition: all .15s;
  }
  .wc-preset:hover { border-color: var(--line-2); background: var(--bg-3); }
  .wc-preset input { accent-color: var(--amber); margin: 0; grid-row: 1 / 3; }
  .wc-preset .wc-pres-name { font-size: 13px; color: var(--txt); font-weight: 600; }
  .wc-preset .wc-pres-desc { font-size: 11px; color: var(--txt-3); grid-column: 2; }
  .wc-preset:has(input:checked) { border-color: var(--amber); background: var(--amber-bg); }
  .wc-convert-info { margin-top: 10px; font-size: 12px; color: var(--txt-3); display: flex; align-items: center; gap: 10px; }
  .wc-convert-info button { background: var(--amber-bg); border: 1px solid rgba(255,159,10,.4); color: var(--amber); padding: 9px 15px; border-radius: var(--r-sm); cursor: pointer; font-size: 12px; font-weight: 600; }
  .wc-convert-info button:hover { background: rgba(255,159,10,.25); }
  .wc-convert-info button:disabled { background: var(--bg-2); color: var(--txt-4); border-color: var(--line); cursor: not-allowed; }

  /* Mises à jour (welcome) */
  .wc-update-row { display: flex; align-items: center; justify-content: space-between; font-size: 13px; color: var(--txt-2); }
  .wc-update-current b { color: var(--txt); font-weight: 600; }
  .wc-update-btn { background: var(--bg-2); border: 1px solid var(--line); color: var(--txt-2); padding: 7px 13px; border-radius: var(--r-sm); cursor: pointer; font-size: 12px; }
  .wc-update-btn:hover { border-color: var(--line-2); background: var(--bg-3); color: var(--txt); }
  .wc-update-btn:disabled { color: var(--txt-4); cursor: not-allowed; }
  .wc-update-banner { margin-top: 12px; padding: 14px; border-radius: var(--r-md); background: var(--violet-bg); border: 1px solid rgba(191,90,242,.35); display: flex; align-items: center; justify-content: space-between; gap: 12px; }
  .wc-update-banner-text { display: flex; align-items: center; gap: 12px; }
  .wc-update-emoji { font-size: 22px; }
  .wc-update-banner-text b { color: #e9d5ff; font-size: 13px; }
  .wc-update-btn-primary { background: var(--violet); border: 0; color: #fff; padding: 9px 15px; border-radius: var(--r-sm); cursor: pointer; font-size: 12px; font-weight: 700; white-space: nowrap; }
  .wc-update-btn-primary:hover { background: #ad4ce0; }
  .wc-update-btn-primary:disabled { opacity: .5; cursor: not-allowed; }

  /* ── Barres de progression (conversion / update) ── */
  #cvm-bar-wrap { background: var(--bg); border-radius: var(--r-pill); height: 8px; overflow: hidden; margin: 16px 0 10px; }
  #cvm-bar { height: 100%; width: 0%; background: linear-gradient(90deg, var(--amber), #ffb340); transition: width .3s var(--ease); }
  #cvm-stats { display: flex; justify-content: space-between; font-size: 12px; color: var(--txt-2); margin: 4px 0; font-variant-numeric: tabular-nums; }
  #cvm-stats b { color: var(--txt); }
  #cvm-summary { background: var(--bg); border: 1px solid var(--line); border-radius: var(--r-sm); padding: 14px; margin-top: 14px; font-size: 12px; color: var(--txt-2); line-height: 1.6; }
  #cvm-summary b { color: var(--keep); }

  /* ── Trial / Paywall ── */
  #trial-wall { display: none; flex-direction: column; align-items: center; justify-content: center; gap: 18px; padding: 48px; text-align: center; }
  #trial-wall .tw-icon { font-size: 56px; }
  #trial-wall h2 { font-size: 24px; font-weight: 700; letter-spacing: -.02em; }
  #trial-wall p  { color: var(--txt-3); font-size: 14px; max-width: 440px; line-height: 1.6; }
  #trial-wall .tw-badge { background: var(--violet-bg); color: var(--violet); padding: 5px 15px; border-radius: var(--r-pill); font-size: 12px; font-weight: 600; }
  #tw-key-wrap { display: flex; gap: 8px; width: 100%; max-width: 440px; }
  #tw-key-input { flex: 1; background: var(--bg-2); border: 1px solid var(--line); border-radius: var(--r-sm); color: var(--txt); font-size: 13px; padding: 11px 15px; outline: none; font-family: ui-monospace, monospace; letter-spacing: .08em; }
  #tw-key-input:focus { border-color: var(--violet); }
  #tw-activate { background: var(--violet); color: #fff; border-radius: var(--r-sm); padding: 11px 20px; font-size: 13px; font-weight: 600; cursor: pointer; border: none; flex-shrink: 0; }
  #tw-activate:disabled { opacity: .4; cursor: default; }
  #tw-error { color: var(--del); font-size: 12px; min-height: 16px; }
  #tw-benefits { display: flex; flex-direction: column; gap: 8px; font-size: 13px; color: var(--txt-2); text-align: left; width: 100%; max-width: 440px; }
  #tw-benefits span { display: flex; align-items: center; gap: 10px; }
  #tw-benefits span::before { content: "✓"; color: var(--keep); font-weight: 700; }

  /* ── Écran « tri terminé » ── */
  #done { display: none; flex-direction: column; align-items: center; justify-content: center; gap: 8px; padding: 40px; text-align: center; }
  #done .icon { font-size: 60px; margin-bottom: 8px; }
  #done h2 { font-size: 28px; font-weight: 700; letter-spacing: -.02em; }
  #done p { color: var(--txt-3); font-size: 15px; }
  #done-sub { color: var(--txt-4); font-size: 13px; margin-top: 2px; }
  #done-actions { display: flex; flex-wrap: wrap; gap: 10px; justify-content: center; margin-top: 26px; max-width: 540px; }
  .done-cta {
    background: var(--bg-1); border: 1px solid var(--line); color: var(--txt);
    padding: 11px 18px; border-radius: var(--r-md); cursor: pointer; font-size: 13px; font-weight: 600;
    transition: background .15s, border-color .15s, transform .08s;
  }
  .done-cta:hover { background: var(--bg-2); border-color: var(--line-2); }
  .done-cta:active { transform: scale(.98); }
  .done-cta.primary { background: var(--keep-bg); border-color: var(--keep-line); color: var(--keep); }
  .done-cta.primary:hover { background: rgba(48,209,88,.26); }

  /* ── Overlay de traitement (progression réelle) ── */
  #processing-overlay {
    position: fixed; inset: 0; z-index: 400; display: none; flex-direction: column;
    align-items: center; justify-content: center; gap: 20px; text-align: center;
    background: rgba(8,8,9,.9); backdrop-filter: blur(14px);
  }
  #processing-overlay.show { display: flex; }
  #proc-spinner { width: 48px; height: 48px; border-radius: 50%; border: 3px solid var(--line); border-top-color: var(--amber); animation: proc-spin .9s linear infinite; }
  @keyframes proc-spin { to { transform: rotate(360deg); } }
  #proc-title { font-size: 17px; font-weight: 700; color: var(--txt); }
  #proc-warn  { font-size: 13px; color: var(--amber); max-width: 380px; line-height: 1.5; }
  #proc-bar-wrap { width: 340px; height: 8px; background: var(--bg); border-radius: var(--r-pill); overflow: hidden; }
  #proc-bar { height: 100%; width: 0%; background: linear-gradient(90deg, var(--amber), #ffb340); transition: width .3s var(--ease); }
  #proc-meta { font-size: 12.5px; color: var(--txt-2); font-variant-numeric: tabular-nums; }
  #proc-bar-wrap.indeterminate #proc-bar { width: 35%; animation: proc-indet 1.1s ease-in-out infinite; }
  @keyframes proc-indet { 0%{transform:translateX(-120%)} 100%{transform:translateX(300%)} }
  #proc-error { font-size: 13px; color: var(--del); max-width: 400px; display: none; }
  #proc-close { display: none; background: var(--bg-2); border: 1px solid var(--line); color: var(--txt); padding: 10px 20px; border-radius: var(--r-sm); cursor: pointer; font-size: 13px; }

  /* ── Mode galerie de tri ── */
  #gallery-stage { display: none; flex-direction: column; flex: 1; min-height: 0; }
  #gallery-stage.show { display: flex; }
  #gallery-top {
    display: flex; align-items: center; gap: 14px; padding: 14px 20px;
    background: rgba(16,16,18,.72); backdrop-filter: saturate(180%) blur(20px);
    border-bottom: 1px solid var(--line); flex-shrink: 0; flex-wrap: wrap;
  }
  #gallery-top h2 { font-size: 15px; font-weight: 700; color: var(--txt); margin: 0; letter-spacing: -.01em; }
  #gallery-count { font-size: 12.5px; color: var(--txt-3); font-variant-numeric: tabular-nums; }
  #gallery-grid {
    flex: 1; min-height: 0; overflow-y: auto; padding: 20px;
    display: grid; grid-template-columns: repeat(auto-fill, minmax(180px, 1fr)); gap: 14px; align-content: start;
  }
  #gallery-grid .g-cell {
    position: relative; background: var(--bg-1); border: 2px solid var(--line);
    border-radius: var(--r-md); overflow: hidden; cursor: pointer;
    transition: border-color .14s var(--ease), transform .14s var(--ease), box-shadow .14s;
    display: block;
  }
  #gallery-grid .g-cell:hover { transform: translateY(-2px); box-shadow: var(--shadow-sm); border-color: var(--line-2); }
  #gallery-grid .g-cell:focus { outline: none; border-color: var(--blue); box-shadow: 0 0 0 3px var(--blue-bg); }
  #gallery-grid .g-cell.keep  { border-color: var(--keep); }
  #gallery-grid .g-cell.trash { border-color: var(--del); opacity: .65; }
  .g-thumb { aspect-ratio: 1; background: #000; display: flex; align-items: center; justify-content: center; overflow: hidden; }
  .g-thumb img { width: 100%; height: 100%; object-fit: cover; }
  .g-info { padding: 8px 10px 2px; font-size: 11.5px; color: var(--txt-2); display: flex; justify-content: space-between; gap: 6px; font-variant-numeric: tabular-nums; }
  .g-name { padding: 0 10px 9px; font-size: 10.5px; color: var(--txt-4); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .g-mark {
    position: absolute; top: 8px; right: 8px; width: 26px; height: 26px; border-radius: 50%;
    display: none; align-items: center; justify-content: center; font-size: 13px; color: #fff; font-weight: 700;
    box-shadow: 0 2px 8px rgba(0,0,0,.4);
  }
  #gallery-grid .g-cell.keep  .g-mark { display: flex; background: var(--keep); color: #04130a; }
  #gallery-grid .g-cell.trash .g-mark { display: flex; background: var(--del); }
  #gallery-foot {
    display: flex; align-items: center; gap: 16px; padding: 14px 20px;
    background: rgba(16,16,18,.82); backdrop-filter: saturate(180%) blur(20px);
    border-top: 1px solid var(--line); flex-shrink: 0; flex-wrap: wrap;
  }
  #gallery-hint { font-size: 12px; color: var(--txt-3); flex: 1; line-height: 1.8; }
  #gallery-hint .key { background: var(--bg-2); border: 1px solid var(--line); border-radius: 5px; padding: 1px 6px; font-size: 11px; color: var(--txt-2); margin: 0 1px; }
  #gallery-marks { font-size: 12.5px; color: var(--txt-2); font-variant-numeric: tabular-nums; }
  #gallery-validate { background: var(--keep); color: #04130a; padding: 11px 20px; border-radius: var(--r-md); cursor: pointer; font-size: 13px; font-weight: 700; }
  #gallery-validate:disabled { background: var(--bg-3); color: var(--txt-4); cursor: not-allowed; }
  #gallery-validate:not(:disabled):hover { background: #28c14e; }
  #btn-gallery-exit { background: var(--bg-2); border: 1px solid var(--line); color: var(--txt); padding: 8px 14px; border-radius: var(--r-sm); font-weight: 600; }
  #btn-gallery-exit:hover { background: var(--bg-3); border-color: var(--line-2); }

  /* ── Modales génériques ── */
  .modal-back { position: fixed; inset: 0; z-index: 200; background: rgba(0,0,0,.6); display: none; align-items: center; justify-content: center; backdrop-filter: blur(8px); }
  .modal-back.show { display: flex; }
  .modal-card { background: var(--bg-1); border: 1px solid var(--line); border-radius: var(--r-lg); padding: 26px; max-width: 480px; width: 90%; box-shadow: var(--shadow); }
  .modal-card h3 { margin: 0 0 8px; font-size: 17px; font-weight: 700; letter-spacing: -.01em; }
  .modal-card p { color: var(--txt-2); font-size: 13px; line-height: 1.5; margin: 8px 0; }
  .modal-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 20px; }
  .modal-actions button { padding: 9px 18px; border: 0; border-radius: var(--r-sm); font-size: 13px; cursor: pointer; font-weight: 600; }
  .modal-btn-cancel { background: var(--bg-2); color: var(--txt-2); }
  .modal-btn-cancel:hover { background: var(--bg-3); color: var(--txt); }
  .modal-btn-confirm { background: var(--amber); color: #1a1200; }
  .modal-btn-confirm:hover { background: #ffb340; }
  .modal-btn-danger { background: var(--del); color: #fff; }
  .modal-btn-danger:hover { background: #f5392d; }

  /* ── Crop overlay (image) ── */
  #crop-overlay { position: fixed; inset: 0; z-index: 250; background: rgba(0,0,0,.88); display: none; align-items: center; justify-content: center; flex-direction: column; }
  #crop-overlay.show { display: flex; }
  #crop-stage { position: relative; max-width: 90vw; max-height: 78vh; overflow: hidden; cursor: crosshair; background: var(--bg); border-radius: var(--r-md); }
  #crop-img { display: block; max-width: 90vw; max-height: 78vh; user-select: none; -webkit-user-drag: none; }
  #crop-rect { position: absolute; border: 2px solid #fff; background: rgba(10,132,255,.16); box-shadow: 0 0 0 99999px rgba(0,0,0,.6); display: none; }
  #crop-bar { display: flex; align-items: center; gap: 14px; margin-top: 20px; color: var(--txt-2); font-size: 13px; }
  #crop-info { font-family: ui-monospace, monospace; color: var(--txt-3); }
  #crop-bar button { padding: 11px 22px; border: 0; border-radius: var(--r-sm); cursor: pointer; font-size: 13px; font-weight: 600; }
  .crop-btn-cancel { background: var(--bg-2); color: var(--txt-2); }
  .crop-btn-cancel:hover { background: var(--bg-3); color: var(--txt); }
  .crop-btn-validate { background: var(--keep); color: #04130a; }
  .crop-btn-validate:hover { background: #28c14e; }
  .crop-btn-validate:disabled { background: var(--bg-3); color: var(--txt-4); cursor: not-allowed; }

  /* ── Trim overlay (vidéo) ── */
  #trim-overlay { position: fixed; inset: 0; z-index: 250; background: rgba(0,0,0,.92); display: none; align-items: center; justify-content: center; flex-direction: column; padding: 32px; }
  #trim-overlay.show { display: flex; }
  #trim-video { max-width: 80vw; max-height: 58vh; border-radius: var(--r-md); background: #000; box-shadow: var(--shadow); }
  #trim-timeline-wrap { width: min(80vw, 820px); margin-top: 26px; background: var(--bg-1); border: 1px solid var(--line); border-radius: var(--r-md); padding: 18px; }
  #trim-track { position: relative; height: 40px; background: var(--bg); border-radius: var(--r-sm); cursor: pointer; margin-bottom: 14px; }
  #trim-selection { position: absolute; top: 0; bottom: 0; background: var(--blue-bg); border-left: 3px solid var(--blue); border-right: 3px solid var(--blue); }
  .trim-handle { position: absolute; top: -4px; bottom: -4px; width: 16px; background: var(--blue); border-radius: 4px; cursor: ew-resize; transform: translateX(-8px); display: flex; align-items: center; justify-content: center; color: #fff; font-size: 11px; font-weight: bold; }
  #trim-handle-start { left: 0; }
  #trim-handle-end   { left: 100%; }
  #trim-times { display: flex; justify-content: space-between; font-size: 12px; font-family: ui-monospace, monospace; color: var(--txt-3); }
  #trim-times span b { color: var(--blue); }
  #trim-actions { display: flex; gap: 8px; justify-content: center; margin-top: 18px; }
  #trim-actions button { padding: 11px 22px; border: 0; border-radius: var(--r-sm); cursor: pointer; font-size: 13px; font-weight: 600; }

  /* ── Toast ── */
  #toast {
    position: fixed; bottom: 96px; left: 50%; transform: translateX(-50%) translateY(8px);
    background: rgba(40,40,44,.92); backdrop-filter: blur(20px); color: var(--txt);
    padding: 13px 22px; border: 1px solid var(--line-2); border-radius: var(--r-md);
    font-size: 13px; font-weight: 500; z-index: 300; opacity: 0;
    transition: opacity .22s var(--ease), transform .22s var(--ease);
    pointer-events: none; max-width: 80vw; text-align: center; box-shadow: var(--shadow-sm);
  }
  #toast.show { opacity: 1; transform: translateX(-50%) translateY(0); }
  #toast.error   { background: rgba(120,30,28,.94); border-color: var(--del-line); }
  #toast.success { background: rgba(20,70,38,.94); border-color: var(--keep-line); }

  /* CLIP/IA masqué (v0.2.0) */
  #clip-indicator { display: none !important; }
  #sem-stage      { display: none !important; }
</style>
</head>
<body>

<div id="progress-wrap"><div id="progress-bar"></div></div>

<!-- Topbar : contexte du média (nom, position) + utilitaires -->
<div id="topbar">
  <div class="tb-left">
    <div id="info">
      <div class="name" id="fname">Chargement…</div>
      <div class="meta" id="fmeta"></div>
    </div>
  </div>
  <div class="tb-right">
    <div id="triage-filter" class="tf-group" title="Filtrer les médias à trier"></div>
    <button id="btn-gallery" onclick="setTriageMode('gallery')" title="Basculer en mode galerie (tri par lot au clavier)">▦ Galerie</button>
    <button id="btn-empty-trash" onclick="confirmEmptyTrash()" title="Vider Tri/Supprimées vers la Corbeille macOS">🗑 <span id="empty-trash-label">Vider</span></button>
    <span id="scan-indicator">
      <button id="btn-rescan" onclick="triggerRescan()" title="Relancer l'analyse des doublons pHash">↻</button>
    </span>
    <span id="clip-indicator">
      <button id="btn-clip-rescan" onclick="triggerClipRescan()" title="Lancer l'analyse IA (CLIP ViT-L/14)">🤖</button>
    </span>
    <button id="btn-reset" onclick="wcReset()" title="Revenir à l'accueil (dossiers et options)">⚙ Accueil</button>
  </div>
</div>

<!-- Welcome view (v0.2.0) -->
<div id="welcome">
  <div id="welcome-card">
    <div class="wc-title">
      <div class="wc-icon">📁</div>
      <h1>Sort Memories</h1>
      <p class="wc-tag">Triez et nettoyez vos dossiers de médias locaux</p>
    </div>

    <section class="wc-section">
      <label>Dossiers à trier</label>
      <ul id="wc-sources"></ul>
      <button id="wc-add" onclick="wcAddSource()">+ Ajouter un dossier</button>
      <input id="wc-path-fallback" type="text" placeholder="Coller un chemin (ex: /Users/me/Pictures/Snapchat)" style="display:none">
      <div id="wc-source-err"></div>
    </section>

    <section class="wc-section">
      <label>Options de tri</label>
      <div class="wc-opts">
        <label><input type="checkbox" id="opt-by_year" checked> Grouper par année</label>
        <label><input type="checkbox" id="opt-by_month"> Aussi par mois</label>
        <label><input type="checkbox" id="opt-split_media"> Séparer images / vidéos</label>
        <label><input type="checkbox" id="opt-rename"> Renommer (YYYY-MM-DD_hash.ext)</label>
        <label><input type="checkbox" id="opt-order_largest"> Traiter d'abord les fichiers les plus volumineux</label>
      </div>
    </section>

    <div class="wc-preview">
      <span>Aperçu structure de sortie :</span>
      <code id="wc-preview-path">&lt;source&gt;/Tri/Gardées/2024/photo.jpg</code>
    </div>

    <section class="wc-section">
      <label>Compression avant tri (optionnel)</label>
      <p class="wc-hint">Convertit toutes les photos (JPG/PNG/HEIC/TIFF/RAW…) en WebP et toutes les vidéos (MP4/MOV/AVI/MKV…) en H.265, en <b style="color:#4ade80">préservant les métadonnées</b> (date de prise de vue, GPS). Gain disque + tri plus fluide. <b style="color:#fb923c">Irréversible</b> — les originaux sont remplacés.</p>
      <div class="wc-presets">
        <label class="wc-preset"><input type="radio" name="wc-preset" value="none" checked> <span class="wc-pres-name">Aucune</span><span class="wc-pres-desc">Tri direct sur les originaux</span></label>
        <label class="wc-preset"><input type="radio" name="wc-preset" value="lossless"> <span class="wc-pres-name">Sans perte</span><span class="wc-pres-desc">WebP q90 + H.265 CRF 22 — gain ~30-50%, imperceptible</span></label>
        <label class="wc-preset"><input type="radio" name="wc-preset" value="balanced"> <span class="wc-pres-name">Équilibré</span><span class="wc-pres-desc">WebP q82 + H.265 CRF 25 — gain ~50-70%, légère perte</span></label>
        <label class="wc-preset"><input type="radio" name="wc-preset" value="compact"> <span class="wc-pres-name">Compact</span><span class="wc-pres-desc">WebP q72 + H.265 CRF 28 — gain ~70-90%, perte acceptable</span></label>
      </div>
      <div id="wc-convert-info" class="wc-convert-info"></div>
    </section>

    <section class="wc-section">
      <label>Mises à jour</label>
      <div class="wc-update-row">
        <span class="wc-update-current">Version installée : <b id="wc-current-version">v?.?.?</b></span>
        <button id="wc-check-update" class="wc-update-btn" onclick="checkForUpdate(true)">Vérifier maintenant</button>
      </div>
      <div id="wc-update-banner" class="wc-update-banner" style="display:none">
        <div class="wc-update-banner-text">
          <span class="wc-update-emoji">🎉</span>
          <div>
            <b>Nouvelle version disponible : <span id="wc-update-latest">vX.Y.Z</span></b>
            <p class="wc-hint" id="wc-update-size"></p>
          </div>
        </div>
        <button id="wc-install-update" class="wc-update-btn-primary" onclick="installUpdate()">Mettre à jour →</button>
      </div>
      <p id="wc-update-msg" class="wc-hint"></p>
    </section>

    <button id="wc-start" onclick="wcStart()" disabled>Démarrer le triage →</button>
    <button id="wc-gallery-btn" onclick="openGallery()" disabled style="margin-top:8px;background:#1f1f1f;color:#ddd">📊 Galerie — voir les plus volumineux</button>
    <p id="wc-error" class="wc-err"></p>
  </div>
</div>

<!-- Modal galerie triable -->
<div class="modal-back" id="gallery-modal">
  <div class="modal-card" style="max-width:1100px;width:94vw;height:88vh;display:flex;flex-direction:column">
    <div style="display:flex;align-items:center;justify-content:space-between;gap:12px;flex-shrink:0">
      <h3 style="margin:0">Galerie <span id="gal-count" style="color:#777;font-weight:400;font-size:13px"></span></h3>
      <div style="display:flex;align-items:center;gap:10px">
        <select id="gal-sort" onchange="loadGallery(this.value)" style="background:#1a1a1a;color:#ddd;border:1px solid #333;border-radius:6px;padding:6px 10px;font-size:13px">
          <option value="size_desc">Plus volumineux d'abord</option>
          <option value="size_asc">Plus légers d'abord</option>
          <option value="date_desc">Plus récents d'abord</option>
          <option value="date_asc">Plus anciens d'abord</option>
          <option value="name">Nom (A→Z)</option>
        </select>
        <button class="modal-btn-cancel" onclick="closeGallery()" style="padding:6px 14px">Fermer</button>
      </div>
    </div>
    <div id="gal-grid" style="flex:1;overflow-y:auto;margin-top:12px;display:grid;grid-template-columns:repeat(auto-fill,minmax(150px,1fr));gap:10px;align-content:start"></div>
  </div>
</div>

<!-- Modal téléchargement mise à jour -->
<div class="modal-back" id="update-modal">
  <div class="modal-card" style="max-width:480px">
    <h3 id="um-title">Téléchargement en cours…</h3>
    <p class="wc-hint" id="um-detail">Sort Memories va se relancer automatiquement dès que l'installation est terminée.</p>
    <div id="cvm-bar-wrap" style="margin-top:16px">
      <div id="um-bar" style="height:100%;width:0%;background:#a78bfa;border-radius:6px;transition:width .2s"></div>
    </div>
    <div id="cvm-stats" style="margin-top:8px">
      <span><b id="um-pct">0%</b></span>
      <span id="um-bytes">0 / 0 MB</span>
    </div>
    <p id="um-error" style="color:#f87171;font-size:12px;margin-top:12px;display:none"></p>
    <div class="modal-actions">
      <button id="um-cancel" class="modal-btn-cancel" onclick="closeUpdateModal()" style="display:none">Fermer</button>
    </div>
  </div>
</div>

<!-- Modal progression conversion -->
<div class="modal-back" id="convert-modal">
  <div class="modal-card" style="max-width:540px">
    <h3 id="cvm-title">Conversion en cours…</h3>
    <p id="cvm-current" style="font-family:ui-monospace,monospace;font-size:11px;color:#777;min-height:14px;margin:4px 0 12px">—</p>
    <div id="cvm-bar-wrap">
      <div id="cvm-bar"></div>
    </div>
    <div id="cvm-stats">
      <span><b id="cvm-pct">0%</b> · <span id="cvm-progress">0 / 0</span></span>
      <span>Économisé : <b id="cvm-saved" style="color:#4ade80">0 B</b></span>
    </div>
    <div id="cvm-summary" style="display:none"></div>
    <div class="modal-actions" id="cvm-actions">
      <button id="cvm-cancel" class="modal-btn-cancel" onclick="cancelConvert()">Annuler</button>
      <button id="cvm-close"  class="modal-btn-confirm" onclick="closeConvertModal()" style="display:none">Continuer →</button>
    </div>
  </div>
</div>

<!-- Single mode -->
<div id="stage">
  <div id="trial-wall" style="display:none">
    <div class="tw-icon">🔒</div>
    <span class="tw-badge">Essai gratuit terminé</span>
    <h2>1 000 médias triés — continuez avec une licence</h2>
    <p>Vous avez utilisé votre essai gratuit de 1 000 fichiers. Activez votre licence à vie pour continuer le triage et sauvegarder votre progression.</p>
    <div id="tw-benefits">
      <span>Triage illimité — aucune limite de fichiers</span>
      <span>Détection IA des doublons (CLIP ViT-L/14)</span>
      <span>Progression sauvegardée en permanence</span>
      <span>Licence à vie — un seul paiement, pas d'abonnement</span>
      <span>Fonctionne 100% en local, vos photos restent sur votre machine</span>
    </div>
    <div id="tw-key-wrap">
      <input id="tw-key-input" type="text" placeholder="XXXX-XXXX-XXXX-XXXX"
             oninput="twKeyInput()" onkeydown="if(event.key==='Enter')activateLicense()">
      <button id="tw-activate" onclick="activateLicense()" disabled>Activer</button>
    </div>
    <div id="tw-error"></div>
    <p style="font-size:12px;color:#444">
      Pas encore de licence ?
      <a href="https://triage.eliottbouquerel.fr" target="_blank"
         style="color:#a78bfa;text-decoration:none">Obtenir la licence — 19€ à vie →</a>
    </p>
  </div>
  <div id="done">
    <div class="icon" id="done-icon">✅</div>
    <h2 id="done-title">Tri terminé !</h2>
    <p id="done-msg"></p>
    <p id="done-sub"></p>
    <div id="done-actions">
      <button class="done-cta primary" onclick="doRefreshQueue()">↻ Rechercher de nouveaux médias</button>
      <button class="done-cta" id="done-allfilter" onclick="setTriageFilter('all')" style="display:none">👁 Voir tout</button>
      <button class="done-cta" onclick="openGallery()">📊 Galerie</button>
      <button class="done-cta" onclick="confirmEmptyTrash()">🗑 Vider la corbeille</button>
      <button class="done-cta" onclick="wcReset()">⚙ Changer de dossiers</button>
    </div>
  </div>
  <div id="media-wrap" style="display:none" onclick="videoClick()">
    <div id="overlay-badge">Overlay ON</div>
    <div id="play-icon"></div>
  </div>
</div>

<!-- Group mode (doublons pHash) -->
<div id="group-stage">
  <div id="group-header">
    <span id="group-count"></span>
    <span id="sim-badge"></span>
    <span style="flex:1"></span>
    <span style="font-size:11px;color:#555">Cliquer sur la photo à garder</span>
  </div>
  <div id="group-grid"></div>
</div>

<!-- Semantic group mode (CLIP IA) -->
<div id="sem-stage">
  <div id="sem-header">
    <span id="sem-count"></span>
    <span id="sem-label-badge"></span>
    <span style="flex:1"></span>
    <div id="sem-decision-bar">
      <span id="sem-counts">—</span>
      <button id="btn-sem-validate" onclick="validateSemantic()" disabled>✓ Valider [→]</button>
    </div>
  </div>
  <div id="sem-grid"></div>
</div>

<!-- Mode galerie de tri (photos & vidéos, clavier) -->
<div id="gallery-stage">
  <div id="gallery-top">
    <button id="btn-gallery-exit" class="done-cta" onclick="setTriageMode('single')">▭ Un par un</button>
    <h2>📊 Galerie de tri</h2>
    <span id="gallery-count"></span>
    <span style="flex:1"></span>
    <div id="triage-filter-gal" class="tf-group"></div>
  </div>
  <div id="gallery-grid" tabindex="0"></div>
  <div id="gallery-foot">
    <span id="gallery-hint">
      <span class="key">←→↑↓</span> naviguer ·
      <span class="key">Espace</span>/<span class="key">K</span> garder ·
      <span class="key">D</span> supprimer ·
      <span class="key">U</span> annuler ·
      <span class="key">Entrée</span> valider le lot ·
      <span class="key">Échap</span> revenir
    </span>
    <span id="gallery-marks">0 gardé · 0 supprimé</span>
    <button id="gallery-validate" onclick="validateGalleryBatch()" disabled>Valider le lot →</button>
  </div>
</div>

<!-- Contrôles vidéo (single mode) -->
<div id="video-ctrl">
  <button id="vc-pp" onclick="vcPlayPause()">⏸</button>
  <div id="vc-prog-wrap" onclick="vcSeek(event)"><div id="vc-prog-fill"></div></div>
  <span id="vc-time">0:00 / 0:00</span>
  <button id="vc-mute" onclick="vcMute()">🔊</button>
  <input id="vc-vol" type="range" min="0" max="1" step="0.05" value="1" oninput="vcVolume(this.value)">
</div>

<div id="bar">
  <button id="btn-back" onclick="act('back')" disabled>↩ Annuler <span class="key">⌫</span></button>
  <div class="bar-tools">
    <button id="btn-rotate" onclick="transformRotate()" title="Rotation 90° horaire [T]">⟳ Pivoter <span class="key">T</span></button>
    <button id="btn-crop" onclick="openCropOrTrim()" title="Rogner (photo) / Découper (vidéo) [R]" style="display:none">✂ Rogner <span class="key">R</span></button>
    <button id="btn-overlay" onclick="toggleOverlay()" style="display:none">👁 Overlay <span class="key">↑</span></button>
  </div>
  <div class="bar-spacer"></div>
  <button id="btn-keep-all"    onclick="actGroupAll()"      style="display:none">✓ Tout garder <span class="key">→</span></button>
  <button id="btn-trash-all"   onclick="actGroupTrashAll()" style="display:none">✕ Tout supprimer <span class="key">↓</span></button>
  <button id="btn-sem-validate" class="in-bar" onclick="validateSemantic()" style="display:none" disabled>✓ Valider <span class="key">→</span></button>
  <button id="btn-trash" onclick="act('trash')">✕ Supprimer <span class="key">↓ D</span></button>
  <button id="btn-keep" onclick="act('keep')">✓ Garder <span class="key">→</span></button>
</div>

<!-- Toast notifications -->
<div id="toast"></div>

<!-- Overlay de traitement (trim/crop/rotate) avec progression -->
<div id="processing-overlay">
  <div id="proc-spinner"></div>
  <div id="proc-title">Traitement en cours…</div>
  <div id="proc-warn">⚠ Ne quittez pas et ne faites pas « Retour » : l'opération est en cours.</div>
  <div id="proc-bar-wrap"><div id="proc-bar"></div></div>
  <div id="proc-meta"></div>
  <div id="proc-error"></div>
  <button id="proc-close" onclick="hideProcessing()">Fermer</button>
</div>

<!-- Modal de confirmation générique (vider corbeille, etc.) -->
<div class="modal-back" id="confirm-modal">
  <div class="modal-card">
    <h3 id="cm-title">Confirmer</h3>
    <p id="cm-message">Êtes-vous sûr ?</p>
    <div class="modal-actions">
      <button class="modal-btn-cancel" onclick="closeConfirm()">Annuler</button>
      <button id="cm-confirm-btn" class="modal-btn-danger" onclick="confirmAction()">Confirmer</button>
    </div>
  </div>
</div>

<!-- Crop overlay (image) -->
<div id="crop-overlay">
  <div id="crop-stage">
    <img id="crop-img" src="" alt="">
    <div id="crop-rect"></div>
  </div>
  <div id="crop-bar">
    <span id="crop-info">Cliquer-glisser sur l'image pour sélectionner la zone à conserver</span>
    <button class="crop-btn-cancel" onclick="closeCrop()">Annuler <span class="key">Echap</span></button>
    <button id="crop-validate" class="crop-btn-validate" onclick="validateCrop()" disabled>✓ Valider</button>
  </div>
</div>

<!-- Trim overlay (vidéo) -->
<div id="trim-overlay">
  <video id="trim-video" controls preload="metadata"></video>
  <div id="trim-timeline-wrap">
    <div id="trim-track">
      <div id="trim-selection">
        <div class="trim-handle" id="trim-handle-start" title="Début">⟨</div>
        <div class="trim-handle" id="trim-handle-end" title="Fin">⟩</div>
      </div>
    </div>
    <div id="trim-times">
      <span>Début : <b id="trim-start-time">0:00</b></span>
      <span>Durée totale : <b id="trim-total-time">0:00</b></span>
      <span>Fin : <b id="trim-end-time">0:00</b></span>
    </div>
  </div>
  <div id="trim-actions">
    <button class="crop-btn-cancel" onclick="closeTrim()">Annuler <span class="key">Echap</span></button>
    <button id="trim-validate" class="crop-btn-validate" onclick="validateTrim()">✓ Valider la coupe</button>
  </div>
</div>

<script>
let current    = {};
let busy       = false;
let overlayOn  = true;
let vid        = null;
let scanDoneShown     = false;
let clipDoneShown     = false;
let semDecisions      = {};   // {rel: "keep"|"trash"} pour le mode semantic_group

/* ─── Scan status polling ─────────────────────────────────── */
function updateScanIndicator(st) {
  const el = document.getElementById('scan-indicator');
  const rescan = document.getElementById('btn-rescan');
  if (st.running) {
    const pct = st.total ? Math.round(st.progress / st.total * 100) : 0;
    el.className = 'running';
    el.innerHTML = `🔍 Analyse… ${pct}% <button id="btn-rescan" onclick="triggerRescan()" style="display:none"></button>`;
  } else if (st.error) {
    el.className = 'error';
    el.innerHTML = `⚠ Erreur scan <button id="btn-rescan" onclick="triggerRescan()" title="Relancer">↻</button>`;
  } else if (st.done) {
    if (!scanDoneShown) {
      scanDoneShown = true;
      el.className = 'done';
      el.innerHTML = `✓ ${st.groups_count} groupe${st.groups_count !== 1 ? 's' : ''} <button id="btn-rescan" onclick="triggerRescan()" title="Relancer">↻</button>`;
      // Reload state to pick up groups
      load();
      setTimeout(() => {
        el.className = '';
        el.innerHTML = `<button id="btn-rescan" onclick="triggerRescan()" title="Relancer l'analyse">↻</button>`;
      }, 6000);
    }
  } else {
    el.className = '';
    el.innerHTML = `<button id="btn-rescan" onclick="triggerRescan()" title="Relancer l'analyse">↻</button>`;
  }
}

async function pollScan() {
  try {
    const r  = await fetch('/api/dedupe_status');
    const st = await r.json();
    updateScanIndicator(st);
  } catch(_) {}
}

async function triggerRescan() {
  scanDoneShown = false;
  await fetch('/api/rescan', { method: 'POST' });
  pollScan();
}

/* ─── CLIP status polling ───────────────────────────────── */
function updateClipIndicator(st) {
  const el = document.getElementById('clip-indicator');
  if (st.running) {
    const pct = st.total ? Math.round(st.progress / st.total * 100) : 0;
    const phase = st.phase === 'modèle' ? '📥 Téléchargement modèle…'
                : st.phase === 'embeddings' ? `🤖 IA ${pct}%`
                : st.phase === 'groupes'    ? '🔗 Groupes…'
                : '🤖 En cours…';
    el.className = 'running';
    el.innerHTML = `<span style="color:#a78bfa">${phase}</span>`;
  } else if (st.error) {
    el.className = 'error';
    el.innerHTML = `<span style="color:#f87171" title="${st.error}">⚠ IA erreur</span> <button id="btn-clip-rescan" onclick="triggerClipRescan()" title="Relancer">🤖</button>`;
  } else if (st.done) {
    if (!clipDoneShown) {
      clipDoneShown = true;
      el.className = 'done';
      el.innerHTML = `<span style="color:#a78bfa">✓ ${st.groups_count} groupes IA</span> <button id="btn-clip-rescan" onclick="triggerClipRescan()" title="Relancer">🤖</button>`;
      load();
      setTimeout(() => {
        el.className = '';
        el.innerHTML = `<button id="btn-clip-rescan" onclick="triggerClipRescan()" title="Analyse IA (CLIP ViT-L/14)">🤖</button>`;
      }, 6000);
    }
  } else {
    el.className = '';
    el.innerHTML = `<button id="btn-clip-rescan" onclick="triggerClipRescan()" title="Analyse IA (CLIP ViT-L/14)">🤖</button>`;
  }
}

async function pollClip() {
  try {
    const r  = await fetch('/api/clip_status');
    const st = await r.json();
    updateClipIndicator(st);
  } catch(_) {}
}

async function triggerClipRescan() {
  clipDoneShown = false;
  const r = await fetch('/api/clip_rescan', { method: 'POST' });
  const j = await r.json();
  if (!j.available) alert('open_clip_torch non installé. pip3 install open_clip_torch');
  pollClip();
}

setInterval(pollScan,  2000);
setInterval(pollClip, 3000);
pollScan();
pollClip();

/* ─── Contrôles vidéo (single mode) ─────────────────────── */
function videoClick() { if (!vid) return; vid.paused ? vid.play() : vid.pause(); }
function vcPlayPause() { videoClick(); }
function vcSeek(e) {
  if (!vid || !vid.duration) return;
  const rect = e.currentTarget.getBoundingClientRect();
  vid.currentTime = ((e.clientX - rect.left) / rect.width) * vid.duration;
}
function vcMute() {
  if (!vid) return;
  vid.muted = !vid.muted;
  document.getElementById('vc-mute').textContent = vid.muted ? '🔇' : '🔊';
}
function vcVolume(v) {
  if (!vid) return;
  vid.volume = parseFloat(v);
  vid.muted  = parseFloat(v) === 0;
  document.getElementById('vc-mute').textContent = vid.muted ? '🔇' : '🔊';
}
function fmt(s) {
  const m = Math.floor(s / 60), sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2, '0')}`;
}
function bindVideoEvents(v) {
  const pp   = document.getElementById('vc-pp');
  const fill = document.getElementById('vc-prog-fill');
  const time = document.getElementById('vc-time');
  v.addEventListener('play',  () => pp.textContent = '⏸');
  v.addEventListener('pause', () => pp.textContent = '▶');
  let rafId;
  function rafUpdate() {
    if (!v.duration) { rafId = requestAnimationFrame(rafUpdate); return; }
    fill.style.width = (v.currentTime / v.duration * 100) + '%';
    time.textContent = `${fmt(v.currentTime)} / ${fmt(v.duration)}`;
    rafId = requestAnimationFrame(rafUpdate);
  }
  v.addEventListener('play',  () => { cancelAnimationFrame(rafId); rafUpdate(); });
  v.addEventListener('pause', () => cancelAnimationFrame(rafId));
  v.addEventListener('ended', () => cancelAnimationFrame(rafId));
  v.addEventListener('seeking', () => {
    if (!v.duration) return;
    fill.style.width = (v.currentTime / v.duration * 100) + '%';
    time.textContent = `${fmt(v.currentTime)} / ${fmt(v.duration)}`;
  });
  let flashTimer;
  v.addEventListener('play',  () => flashIcon('▶'));
  v.addEventListener('pause', () => flashIcon('⏸'));
}
function flashIcon(sym) {
  const icon = document.getElementById('play-icon');
  icon.textContent = sym;
  icon.classList.add('show');
  clearTimeout(window._flashTimer);
  window._flashTimer = setTimeout(() => icon.classList.remove('show'), 600);
}

/* ─── Overlay (single mode) ─────────────────────────────── */
function toggleOverlay() {
  overlayOn = !overlayOn;
  const img = document.getElementById('overlay-img');
  const bdg = document.getElementById('overlay-badge');
  const btn = document.getElementById('btn-overlay');
  if (img) img.classList.toggle('hidden', !overlayOn);
  bdg.classList.toggle('hidden', !overlayOn);
  btn.innerHTML = overlayOn
    ? '👁 Overlay ON <span class="key">↑</span>'
    : '🙈 Overlay OFF <span class="key">↑</span>';
  btn.classList.toggle('off', !overlayOn);
}

/* ─── Load & render ─────────────────────────────────────── */
async function load() {
  const r = await fetch('/api/state');
  current = await r.json();
  overlayOn = true;
  vid = null;
  render();
}

function render() {
  if (current.mode === 'needs_config') {
    wcShow();
    return;
  }
  document.getElementById('welcome').style.display = 'none';
  document.getElementById('gallery-stage').classList.remove('show');
  document.getElementById('topbar').classList.remove('show');
  refreshFilterUI();

  document.getElementById('btn-keep-all').style.display      = 'none';
  document.getElementById('btn-trash-all').style.display     = 'none';
  document.getElementById('btn-sem-validate').style.display  = 'none';
  document.getElementById('btn-keep').style.display          = '';
  document.getElementById('btn-trash').style.display         = '';
  document.getElementById('btn-overlay').style.display       = 'none';
  document.getElementById('video-ctrl').classList.remove('on');

  if (current.mode === 'trial_limit') {
    document.getElementById('stage').style.display       = 'flex';
    document.getElementById('group-stage').style.display = 'none';
    document.getElementById('sem-stage').style.display   = 'none';
    document.getElementById('media-wrap').style.display  = 'none';
    document.getElementById('done').style.display        = 'none';
    document.getElementById('trial-wall').style.display  = 'flex';
    document.getElementById('bar').style.display         = 'none';
    document.getElementById('progress-wrap').style.display = 'none';
    return;
  }

  document.getElementById('trial-wall').style.display = 'none';

  if (current.done) {
    renderDone();
    return;
  }

  // Mode galerie : alternative au tri un-par-un (grille clavier, lot).
  if (triageMode === 'gallery') {
    renderGalleryMode();
    return;
  }

  document.getElementById('bar').style.display           = 'flex';
  document.getElementById('topbar').classList.add('show');
  document.getElementById('progress-wrap').style.display = '';
  document.getElementById('progress-bar').style.width =
    (current.index / current.total * 100) + '%';
  document.getElementById('btn-back').disabled = !current.can_back;

  if (current.mode === 'group') {
    renderGroup();
  } else if (current.mode === 'semantic_group') {
    renderSemantic();
  } else {
    renderSingle();
  }
}

/* ─── Écran « tri terminé » + CTA ───────────────────────── */
function renderDone() {
  document.getElementById('stage').style.display       = 'flex';
  document.getElementById('group-stage').style.display = 'none';
  document.getElementById('sem-stage').style.display   = 'none';
  document.getElementById('media-wrap').style.display  = 'none';
  document.getElementById('gallery-stage').classList.remove('show');
  document.getElementById('topbar').classList.remove('show');
  document.getElementById('done').style.display          = 'flex';
  document.getElementById('bar').style.display           = 'none';
  document.getElementById('progress-wrap').style.display = 'none';

  const pending   = current.pending || 0;
  const filtered  = (current.filter && current.filter !== 'all');
  const allBtn    = document.getElementById('done-allfilter');
  const sub       = document.getElementById('done-sub');

  if (filtered && pending > 0) {
    // File non vide globalement, mais plus rien pour le filtre actif.
    const label = current.filter === 'video' ? 'vidéo' : 'photo';
    const other = current.filter === 'video' ? current.pending_photo : current.pending_video;
    const otherLabel = current.filter === 'video' ? 'photo(s)' : 'vidéo(s)';
    document.getElementById('done-icon').textContent  = '🎞';
    document.getElementById('done-title').textContent = `Plus aucune ${label} à trier`;
    document.getElementById('done-msg').textContent   =
      `Il reste ${other} ${otherLabel} dans la file.`;
    sub.textContent = 'Change de filtre pour continuer le tri.';
    allBtn.style.display = '';
  } else {
    document.getElementById('done-icon').textContent  = '✅';
    document.getElementById('done-title').textContent = 'Tri terminé !';
    document.getElementById('done-msg').textContent   =
      `${current.total} fichier(s) traité(s) : dossier _a_supprimer prêt à vider.`;
    sub.textContent = 'De nouveaux médias dans tes dossiers ? Lance une recherche.';
    allBtn.style.display = 'none';
  }
}

/* ─── Single mode render ────────────────────────────────── */
function renderSingle() {
  document.getElementById('stage').style.display       = 'flex';
  document.getElementById('group-stage').style.display = 'none';
  document.getElementById('sem-stage').style.display   = 'none';
  document.getElementById('done').style.display        = 'none';

  document.getElementById('fname').textContent = current.name;
  document.getElementById('fmeta').textContent =
    `${current.index + 1} / ${current.total}  ·  ${current.year}`;

  const btnOv = document.getElementById('btn-overlay');
  if (current.overlay_url) {
    btnOv.style.display = '';
    btnOv.innerHTML = '👁 Overlay ON <span class="key">↑</span>';
    btnOv.classList.remove('off');
  } else {
    btnOv.style.display = 'none';
  }
  document.getElementById('overlay-badge').classList.toggle('hidden', !current.overlay_url);

  const wrap = document.getElementById('media-wrap');
  wrap.querySelectorAll('img, video').forEach(e => e.remove());
  wrap.style.display = 'inline-flex';

  const ts = '?t=' + Date.now();
  // Vidéo lisible nativement uniquement si pas de preview_url (mp4/mov/m4v).
  // Sinon (AVI/MKV/RAW…) on affiche l'aperçu JPEG généré côté serveur.
  if (current.is_video && !current.preview_url) {
    const v    = document.createElement('video');
    v.id       = 'main-video';
    v.src      = current.url + ts;
    v.autoplay = true; v.loop = true; v.controls = false;
    v.volume   = parseFloat(document.getElementById('vc-vol').value);
    wrap.insertBefore(v, wrap.firstChild);
    v.play().catch(() => { v.muted = true; v.play(); });
    bindVideoEvents(v);
    vid = v;
    document.getElementById('video-ctrl').classList.add('on');
    document.getElementById('vc-pp').textContent        = '⏸';
    document.getElementById('vc-prog-fill').style.width = '0';
    document.getElementById('vc-time').textContent      = '0:00 / 0:00';
    document.getElementById('vc-mute').textContent      = '🔊';
  } else {
    const img = document.createElement('img');
    img.id    = 'main-img'; img.src = (current.preview_url || current.url) + ts;
    img.alt   = current.name || 'Média à trier';
    wrap.insertBefore(img, wrap.firstChild);
  }

  if (current.overlay_url) {
    const ov     = document.createElement('img');
    ov.id        = 'overlay-img';
    ov.src       = current.overlay_url + ts;
    ov.className = overlayOn ? '' : 'hidden';
    wrap.insertBefore(ov, document.getElementById('overlay-badge'));
  }

  // Contrôle contextuel : rogner (photo) vs découper (vidéo).
  const cbtn = document.getElementById('btn-crop');
  if (cbtn) {
    cbtn.style.display = '';
    cbtn.innerHTML = current.is_video
      ? '✂ Découper <span class="key">R</span>'
      : '✂ Rogner <span class="key">R</span>';
  }
}

/* ─── Group mode render ─────────────────────────────────── */
function renderGroup() {
  document.getElementById('stage').style.display       = 'none';
  document.getElementById('group-stage').style.display = 'flex';
  document.getElementById('sem-stage').style.display   = 'none';
  document.getElementById('done').style.display        = 'none';

  const n = current.group_files.length;
  document.getElementById('fname').textContent = `Groupe de ${n} doublons`;
  document.getElementById('fmeta').textContent =
    `${current.index + 1} / ${current.total}`;

  // Similarity badge
  const sim  = Math.round(current.similarity * 100);
  const badge = document.getElementById('sim-badge');
  badge.textContent = `Similarité ${sim}%`;
  badge.className   = sim >= 90 ? 'sim-green' : sim >= 75 ? 'sim-orange' : 'sim-red';
  document.getElementById('group-count').textContent = `${n} copies`;

  // Grid columns
  const cols  = Math.min(n, 3);
  const grid  = document.getElementById('group-grid');
  grid.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
  grid.innerHTML = '';

  const ts = '?t=' + Date.now();
  current.group_files.forEach((f, i) => {
    const cell = document.createElement('div');
    cell.className = 'g-cell';
    cell.title     = 'Garder cette copie';
    cell.onclick   = () => keepFromGroup(f.rel, current.group_id);

    const hint = document.createElement('div');
    hint.className   = 'g-keep-hint';
    hint.textContent = '✓ Garder';
    cell.appendChild(hint);

    const mwrap = document.createElement('div');
    mwrap.className = 'g-media-wrap';

    if (f.is_video && !f.preview_url) {
      const v      = document.createElement('video');
      v.className  = 'g-video';
      v.src        = f.url + ts;
      v.autoplay   = true; v.loop = true; v.muted = true; v.controls = false;
      v.play().catch(() => {});
      mwrap.appendChild(v);
    } else {
      const img    = document.createElement('img');
      img.className = 'g-img';
      img.src       = (f.preview_url || f.url) + ts;
      mwrap.appendChild(img);
      if (f.overlay_url) {
        const ov     = document.createElement('img');
        ov.className = 'g-overlay';
        ov.src       = f.overlay_url + ts;
        mwrap.appendChild(ov);
      }
    }
    cell.appendChild(mwrap);

    const meta = document.createElement('div');
    meta.className   = 'g-meta';
    meta.innerHTML   = `<span>${f.size_kb} Ko</span><span>${f.resolution}</span><span>${f.date}</span>${i === 0 ? '<span style="color:#4ade80">Meilleure qualité</span>' : ''}`;
    cell.appendChild(meta);

    grid.appendChild(cell);
  });

  document.getElementById('btn-keep-all').style.display  = '';
  document.getElementById('btn-trash-all').style.display = '';
  document.getElementById('btn-keep').style.display      = 'none';
  document.getElementById('btn-trash').style.display     = 'none';
}

/* ─── Semantic group render (CLIP IA) ───────────────────── */
function renderSemantic() {
  document.getElementById('stage').style.display       = 'none';
  document.getElementById('group-stage').style.display = 'none';
  document.getElementById('sem-stage').style.display   = 'flex';
  document.getElementById('done').style.display        = 'none';

  document.getElementById('btn-keep-all').style.display     = 'none';
  document.getElementById('btn-trash-all').style.display    = 'none';
  document.getElementById('btn-keep').style.display         = 'none';
  document.getElementById('btn-trash').style.display        = 'none';
  document.getElementById('btn-sem-validate').style.display = '';

  const n = current.group_files.length;
  document.getElementById('fname').textContent =
    `${current.label_icon} ${current.label_text} · ${n} photos similaires`;
  document.getElementById('fmeta').textContent =
    `${current.index + 1} / ${current.total}  ·  Sélectionnez ce que vous gardez`;

  document.getElementById('sem-count').textContent = `${n} photos`;
  const badge = document.getElementById('sem-label-badge');
  badge.textContent = `${current.label_icon} ${current.label_text}`;
  const sim = Math.round(current.similarity * 100);
  badge.title = `Similarité ${sim}%`;

  semDecisions = {};
  // Pré-cocher "keep" la meilleure (index 0 = meilleure qualité triée côté Python)
  if (current.group_files.length > 0) {
    semDecisions[current.group_files[0].rel] = 'keep';
  }

  const cols = Math.min(n, 3);
  const grid = document.getElementById('sem-grid');
  grid.style.gridTemplateColumns = `repeat(${cols}, 1fr)`;
  grid.innerHTML = '';

  const ts = '?t=' + Date.now();
  current.group_files.forEach((f, i) => {
    const cell = document.createElement('div');
    cell.className   = 's-cell' + (i === 0 ? ' keep' : '');
    cell.dataset.rel = f.rel;

    const badge2 = document.createElement('div');
    badge2.className   = 's-cell-badge';
    badge2.textContent = i === 0 ? '✓ Garder' : '';
    cell.appendChild(badge2);

    const mwrap = document.createElement('div');
    mwrap.className = 'g-media-wrap';
    if (f.is_video && !f.preview_url) {
      const v     = document.createElement('video');
      v.className = 'g-video';
      v.src       = f.url + ts;
      v.autoplay  = true; v.loop = true; v.muted = true; v.controls = false;
      v.play().catch(() => {});
      mwrap.appendChild(v);
    } else {
      const img     = document.createElement('img');
      img.className = 'g-img';
      img.src       = (f.preview_url || f.url) + ts;
      mwrap.appendChild(img);
    }
    cell.appendChild(mwrap);

    const meta = document.createElement('div');
    meta.className = 'g-meta';
    meta.innerHTML = `<span>${f.size_kb} Ko</span><span>${f.resolution}</span><span>${f.date}</span>${i === 0 ? '<span class="s-best-badge">⭐ Meilleure qualité</span>' : ''}`;
    cell.appendChild(meta);

    // Clic gauche = keep, clic droit = trash, 2e clic sur même état = neutre
    cell.addEventListener('click', () => toggleSemCell(cell, f.rel, 'keep'));
    cell.addEventListener('contextmenu', e => { e.preventDefault(); toggleSemCell(cell, f.rel, 'trash'); });

    grid.appendChild(cell);
  });

  updateSemCounts();
}

function toggleSemCell(cell, rel, action) {
  if (semDecisions[rel] === action) {
    // Deuxième clic sur même état → neutre (sauf si c'est la dernière "keep")
    const keeps = Object.values(semDecisions).filter(v => v === 'keep').length;
    if (action === 'keep' && keeps <= 1) return; // garder au moins 1
    delete semDecisions[rel];
    cell.className = 's-cell';
    cell.querySelector('.s-cell-badge').textContent = '';
  } else {
    semDecisions[rel] = action;
    cell.className = 's-cell ' + action;
    cell.querySelector('.s-cell-badge').textContent = action === 'keep' ? '✓ Garder' : '✗ Supprimer';
  }
  updateSemCounts();
}

function updateSemCounts() {
  const vals = Object.values(semDecisions);
  const kept    = vals.filter(v => v === 'keep').length;
  const trashed = vals.filter(v => v === 'trash').length;
  const undecided = current.group_files.length - kept - trashed;
  const el = document.getElementById('sem-counts');
  el.innerHTML =
    `<span style="color:#4ade80">${kept} ✓</span> · ` +
    `<span style="color:#f87171">${trashed} ✗</span>` +
    (undecided > 0 ? ` · <span style="color:#666">${undecided} ?</span>` : '');
  document.getElementById('btn-sem-validate').disabled = (undecided > 0);
}

async function validateSemantic() {
  if (busy) return;
  // Vérifier que toutes ont une décision
  const undecided = current.group_files.filter(f => !semDecisions[f.rel]);
  if (undecided.length > 0) return;
  busy = true;
  try {
    await fetch('/api/action', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({
        action:    'decide_semantic_group',
        group_id:  current.group_id,
        decisions: semDecisions,
      }),
    });
    await load();
  } finally { busy = false; }
}

/* ─── Actions ────────────────────────────────────────────── */
async function act(type, extra = {}) {
  if (busy) return;
  busy = true;
  try {
    const map = { keep: 'btn-keep', trash: 'btn-trash', back: 'btn-back' };
    const btn = document.getElementById(map[type]);
    if (btn && !btn.disabled) {
      btn.classList.add('flash');
      btn.addEventListener('animationend', () => btn.classList.remove('flash'), { once: true });
    }
    await fetch('/api/action', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({
        action:          type,
        overlay_rel:     current.overlay_rel  ?? null,
        overlay_visible: current.overlay_url ? overlayOn : false,
        ...extra,
      }),
    });
    await load();
  } finally { busy = false; }
}

async function keepFromGroup(keepRel, groupId) {
  if (busy) return;
  busy = true;
  try {
    await fetch('/api/action', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ action: 'keep_from_group', keep_rel: keepRel, group_id: groupId }),
    });
    await load();
  } finally { busy = false; }
}

async function actGroupAll() {
  if (busy) return;
  busy = true;
  try {
    await fetch('/api/action', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ action: 'keep_all_group', group_id: current.group_id }),
    });
    await load();
  } finally { busy = false; }
}

async function actGroupTrashAll() {
  if (busy) return;
  busy = true;
  try {
    await fetch('/api/action', {
      method:  'POST',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify({ action: 'trash_all_group', group_id: current.group_id }),
    });
    await load();
  } finally { busy = false; }
}


/* ─── Clavier ────────────────────────────────────────────── */
document.addEventListener('keydown', e => {
  if (e.target.tagName === 'INPUT') return;
  // Bloquer toute navigation pendant un traitement (ne pas quitter l'écran).
  if (document.getElementById('processing-overlay').classList.contains('show')) {
    e.preventDefault(); return;
  }
  // En mode galerie, les touches sont gérées par le handler galerie dédié.
  if (triageMode === 'gallery' && !current.done) return;
  if (current.mode === 'group') {
    if      (e.key === 'ArrowLeft' || e.key === 'Backspace') { e.preventDefault(); act('back');        }
    else if (e.key === 'ArrowRight')                          { e.preventDefault(); actGroupAll();      }
    else if (e.key === 'ArrowDown' || e.key === 'd' || e.key === 'D') { e.preventDefault(); actGroupTrashAll(); }
    return;
  }
  if (current.mode === 'semantic_group') {
    if      (e.key === 'ArrowLeft' || e.key === 'Backspace') { e.preventDefault(); act('back'); }
    else if (e.key === 'ArrowRight' || e.key === 'k' || e.key === 'K') {
      // Tout marquer "keep" et valider
      e.preventDefault();
      current.group_files.forEach(f => { semDecisions[f.rel] = 'keep'; });
      document.querySelectorAll('.s-cell').forEach(c => {
        c.className = 's-cell keep';
        c.querySelector('.s-cell-badge').textContent = '✓ Garder';
      });
      updateSemCounts();
      validateSemantic();
    }
    else if (e.key === 'ArrowDown' || e.key === 'd' || e.key === 'D') {
      // Tout marquer "trash" et valider
      e.preventDefault();
      current.group_files.forEach(f => { semDecisions[f.rel] = 'trash'; });
      document.querySelectorAll('.s-cell').forEach(c => {
        c.className = 's-cell trash';
        c.querySelector('.s-cell-badge').textContent = '✗ Supprimer';
      });
      updateSemCounts();
      validateSemantic();
    }
    return;
  }
  if      (e.key === 'ArrowRight')                               { e.preventDefault(); act('keep');      }
  else if (e.key === 'ArrowLeft' || e.key === 'Backspace')      { e.preventDefault(); act('back');      }
  else if (e.key === 'ArrowDown' || e.key === 'Delete'
           || e.key === 'd'      || e.key === 'D')              { e.preventDefault(); act('trash');     }
  else if (e.key === 'ArrowUp')                                  { e.preventDefault(); toggleOverlay();  }
  else if (e.key === ' ')                                        { e.preventDefault(); videoClick();     }
  else if (e.key === 'm' || e.key === 'M')                      { vcMute();                              }
});

/* ─── Licence ────────────────────────────────────────────── */
function twKeyInput() {
  const v = document.getElementById('tw-key-input').value.trim();
  document.getElementById('tw-activate').disabled = v.length < 10;
  document.getElementById('tw-error').textContent = '';
}

async function activateLicense() {
  const key = document.getElementById('tw-key-input').value.trim();
  if (!key) return;
  const btn = document.getElementById('tw-activate');
  btn.disabled  = true;
  btn.textContent = '⏳';
  document.getElementById('tw-error').textContent = '';
  try {
    const r   = await fetch('/api/license/activate', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key }),
    });
    const res = await r.json();
    if (res.ok) {
      btn.textContent = '✓';
      btn.style.background = '#16a34a';
      setTimeout(() => load(), 800);
    } else {
      document.getElementById('tw-error').textContent = res.error || 'Clé invalide';
      btn.disabled    = false;
      btn.textContent = 'Activer';
    }
  } catch(_) {
    document.getElementById('tw-error').textContent = 'Erreur réseau — réessayez';
    btn.disabled    = false;
    btn.textContent = 'Activer';
  }
}

/* ─── Welcome view (v0.2.0) ─────────────────────────────── */
let wcState = { sources: [], options: { by_year: true, by_month: false, split_media: false, rename: false, order: 'default' } };

function wcUpdatePreview() {
  const parts = ['<source>', 'Tri', 'Gardées'];
  if (wcState.options.split_media) parts.push('images');
  if (wcState.options.by_year)     parts.push('2024');
  if (wcState.options.by_month)    parts.push('05');
  parts.push(wcState.options.rename ? '2024-05-22_a1b2c3d4.jpg' : 'photo.jpg');
  document.getElementById('wc-preview-path').textContent = parts.join('/');
}

function wcRenderSources() {
  const ul = document.getElementById('wc-sources');
  ul.innerHTML = '';
  wcState.sources.forEach((src, i) => {
    const li    = document.createElement('li');
    const segs  = src.split('/').filter(Boolean);
    const name  = segs[segs.length - 1] || src;
    li.innerHTML = `
      <span style="opacity:.5">📁</span>
      <span class="wc-src-name"></span>
      <span class="wc-src-path"></span>
      <button class="wc-src-rm" onclick="wcRemoveSource(${i})" title="Retirer">×</button>
    `;
    li.querySelector('.wc-src-name').textContent = name;
    li.querySelector('.wc-src-path').textContent = src;
    ul.appendChild(li);
  });
  document.getElementById('wc-start').disabled = wcState.sources.length === 0;
  const galBtn = document.getElementById('wc-gallery-btn');
  if (galBtn) galBtn.disabled = wcState.sources.length === 0;
  wcUpdateConvertInfo();
}

/* ─── Galerie triable (lecture seule) ─────────────────────── */
async function openGallery() {
  document.getElementById('gallery-modal').classList.add('show');
  await loadGallery(document.getElementById('gal-sort').value);
}
function closeGallery() {
  document.getElementById('gallery-modal').classList.remove('show');
}
async function loadGallery(sort) {
  const grid = document.getElementById('gal-grid');
  grid.innerHTML = '<p style="color:var(--txt-3);grid-column:1/-1;padding:20px">Chargement…</p>';
  try {
    const r = await fetch('/api/gallery?sort=' + encodeURIComponent(sort));
    const d = await r.json();
    if (!d.ok) { grid.innerHTML = '<p style="color:var(--del);grid-column:1/-1;padding:20px">' + (d.error || 'Erreur') + '</p>'; return; }
    document.getElementById('gal-count').textContent =
      `· ${d.count} fichier(s) · ${d.total_mb} Mo`;
    grid.innerHTML = '';
    d.items.forEach(it => {
      const cell = document.createElement('div');
      cell.style.cssText = 'background:#141414;border:1px solid #262626;border-radius:8px;overflow:hidden;display:flex;flex-direction:column';
      const thumb = (it.preview_url || it.url);
      const sizeStr = it.size_kb >= 1024 ? (it.size_kb / 1024).toFixed(1) + ' Mo' : it.size_kb + ' Ko';
      cell.innerHTML =
        `<div style="aspect-ratio:1;background:#000;display:flex;align-items:center;justify-content:center;overflow:hidden">
           <img loading="lazy" src="${thumb}" style="width:100%;height:100%;object-fit:cover">
         </div>
         <div style="padding:6px 8px;font-size:11px;color:#bbb;display:flex;justify-content:space-between;gap:6px">
           <span style="font-weight:600;color:#fb923c">${sizeStr}</span><span>${it.year}</span>
         </div>
         <div style="padding:0 8px 6px;font-size:10px;color:#666;white-space:nowrap;overflow:hidden;text-overflow:ellipsis" title="${it.name}">${it.is_video ? '🎬 ' : ''}${it.name}</div>`;
      grid.appendChild(cell);
    });
    if (!d.items.length) grid.innerHTML = '<p style="color:var(--txt-3);grid-column:1/-1;padding:20px">Aucun média.</p>';
  } catch (e) {
    grid.innerHTML = '<p style="color:var(--del);grid-column:1/-1;padding:20px">Erreur réseau.</p>';
  }
}

function wcRemoveSource(i) {
  wcState.sources.splice(i, 1);
  wcRenderSources();
}

function wcSyncOptions() {
  wcState.options.by_year     = document.getElementById('opt-by_year').checked;
  wcState.options.by_month    = document.getElementById('opt-by_month').checked;
  wcState.options.split_media = document.getElementById('opt-split_media').checked;
  wcState.options.rename      = document.getElementById('opt-rename').checked;
  wcState.options.order       = document.getElementById('opt-order_largest').checked ? 'largest' : 'default';
  wcUpdatePreview();
}

async function wcAddSource() {
  const errEl  = document.getElementById('wc-source-err');
  const input  = document.getElementById('wc-path-fallback');
  errEl.textContent = '';
  try {
    const r = await fetch('/api/pick_folder', { method: 'POST' });
    const j = await r.json();
    if (j.ok && j.path) {
      if (!wcState.sources.includes(j.path)) {
        wcState.sources.push(j.path);
        wcRenderSources();
      }
      input.style.display = 'none';
      return;
    }
    if (j.cancelled) return;
    if (j.error)    errEl.textContent = j.error;
  } catch(_) {
    errEl.textContent = 'Dialog indisponible — colle un chemin ci-dessous puis Entrée.';
  }
  // Fallback : champ texte
  input.style.display = 'block';
  input.focus();
  input.onkeydown = (e) => {
    if (e.key === 'Enter' && input.value.trim()) {
      const path = input.value.trim();
      if (!wcState.sources.includes(path)) wcState.sources.push(path);
      input.value = '';
      input.style.display = 'none';
      errEl.textContent = '';
      wcRenderSources();
    }
  };
}

async function wcStart() {
  const btn = document.getElementById('wc-start');
  const err = document.getElementById('wc-error');
  err.textContent = '';
  if (!wcState.sources.length) { err.textContent = 'Ajoute au moins un dossier source.'; return; }
  btn.disabled = true; btn.textContent = 'Démarrage…';
  try {
    const r = await fetch('/api/config', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(wcState),
    });
    const j = await r.json();
    if (!j.ok) {
      err.textContent = j.error || 'Erreur de configuration';
      btn.disabled = false; btn.textContent = 'Démarrer le triage →';
      return;
    }
    if (j.files_count === 0) {
      err.textContent = 'Aucun média trouvé (extensions : jpg, jpeg, png, gif, webp, mp4, mov).';
      btn.disabled = false; btn.textContent = 'Démarrer le triage →';
      return;
    }
    document.getElementById('welcome').style.display = 'none';
    load();
  } catch(e) {
    err.textContent = e.message || 'Erreur réseau';
    btn.disabled = false; btn.textContent = 'Démarrer le triage →';
  }
}

async function wcReset() {
  if (!confirm("Retour à l'accueil ? La progression actuelle est conservée — tu pourras la reprendre en re-sélectionnant les mêmes dossiers + options.")) return;
  await fetch('/api/reset', { method: 'POST' });
  wcState.sources = [];
  load();
}

/* ─── Conversion (welcome) ──────────────────────────────── */
let _convertPollTimer = null;
let _convertSelectedPreset = 'none';

function wcGetPreset() {
  const r = document.querySelector('input[name="wc-preset"]:checked');
  return r ? r.value : 'none';
}

function wcUpdateConvertInfo() {
  _convertSelectedPreset = wcGetPreset();
  const info = document.getElementById('wc-convert-info');
  if (!info) return;
  if (_convertSelectedPreset === 'none' || wcState.sources.length === 0) {
    info.innerHTML = '';
    return;
  }
  info.innerHTML = `<span>${wcState.sources.length} dossier(s) sélectionné(s) — prêt à compresser</span>` +
                   `<button onclick="wcStartConvert()">⚡ Compresser maintenant</button>`;
}

async function wcStartConvert() {
  const preset = wcGetPreset();
  if (preset === 'none' || wcState.sources.length === 0) return;
  const presetLabel = { lossless: 'Sans perte', balanced: 'Équilibré', compact: 'Compact' }[preset];

  openConfirm(
    `Compression "${presetLabel}" — irréversible`,
    `Tous les médias des dossiers source vont être remplacés par leur version compressée. Les originaux ne pourront pas être restaurés. Le processus peut prendre plusieurs minutes (vidéos surtout).`,
    'Lancer la compression',
    async () => {
      // Configure la session côté backend just-in-time pour exposer les sources à _collect_convertible_files
      const cfg = await fetch('/api/config', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ sources: wcState.sources, options: wcState.options }),
      });
      const cj = await cfg.json();
      if (!cj.ok) { showToast(cj.error || 'Erreur config', 'error', 4000); return; }

      const r = await fetch('/api/convert/start', {
        method: 'POST', headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ preset }),
      });
      const j = await r.json();
      if (!j.ok) { showToast(j.error || 'Erreur de démarrage', 'error', 4000); return; }
      openConvertModal();
      pollConvert();
    },
    true,
  );
}

function openConvertModal() {
  document.getElementById('cvm-title').textContent  = 'Conversion en cours…';
  document.getElementById('cvm-summary').style.display = 'none';
  document.getElementById('cvm-cancel').style.display = '';
  document.getElementById('cvm-close').style.display  = 'none';
  document.getElementById('cvm-bar').style.width      = '0%';
  document.getElementById('cvm-pct').textContent      = '0%';
  document.getElementById('cvm-progress').textContent = '0 / 0';
  document.getElementById('cvm-saved').textContent    = '0 B';
  document.getElementById('cvm-current').textContent  = '—';
  document.getElementById('convert-modal').classList.add('show');
}

function closeConvertModal() {
  document.getElementById('convert-modal').classList.remove('show');
  if (_convertPollTimer) { clearInterval(_convertPollTimer); _convertPollTimer = null; }
  // Refresh preview en cas où l'utilisateur reste sur welcome
  wcUpdateConvertInfo();
}

async function pollConvert() {
  if (_convertPollTimer) clearInterval(_convertPollTimer);
  _convertPollTimer = setInterval(async () => {
    try {
      const r = await fetch('/api/convert/status');
      const s = await r.json();
      document.getElementById('cvm-pct').textContent      = s.percent + '%';
      document.getElementById('cvm-bar').style.width      = s.percent + '%';
      document.getElementById('cvm-progress').textContent = `${s.current} / ${s.total}`;
      document.getElementById('cvm-saved').textContent    = s.bytes_saved_h;
      document.getElementById('cvm-current').textContent  = s.current_file || '—';
      if (s.done || !s.running) {
        clearInterval(_convertPollTimer); _convertPollTimer = null;
        const errCount = s.errors ? s.errors.length : 0;
        const verb = s.cancelled ? 'annulée' : 'terminée';
        document.getElementById('cvm-title').textContent = `Conversion ${verb}`;
        document.getElementById('cvm-summary').style.display = 'block';
        document.getElementById('cvm-summary').innerHTML =
          `<div>Convertis : <b>${s.converted}</b></div>` +
          `<div>Passés (pas de gain / déjà au format) : ${s.skipped}</div>` +
          (errCount ? `<div style="color:#f87171">Erreurs : ${errCount}</div>` : '') +
          `<div style="margin-top:6px">Espace économisé : <b>${s.bytes_saved_h}</b> (sur ${s.bytes_before_h} initialement)</div>`;
        document.getElementById('cvm-cancel').style.display = 'none';
        document.getElementById('cvm-close').style.display  = '';
      }
    } catch(_) {}
  }, 1000);
}

async function cancelConvert() {
  if (!confirm("Annuler la conversion en cours ? Les fichiers déjà convertis sont conservés.")) return;
  await fetch('/api/convert/cancel', { method: 'POST' });
}

/* Quand l'utilisateur change le preset ou ajoute/retire un source, refresh l'info */
document.addEventListener('change', (e) => {
  if (e.target && e.target.name === 'wc-preset') wcUpdateConvertInfo();
});

function wcShow() {
  document.getElementById('welcome').style.display       = 'flex';
  document.getElementById('stage').style.display         = 'none';
  document.getElementById('group-stage').style.display   = 'none';
  document.getElementById('sem-stage').style.display     = 'none';
  document.getElementById('bar').style.display           = 'none';
  document.getElementById('video-ctrl').classList.remove('on');
  document.getElementById('progress-wrap').style.display = 'none';

  // Bind onchange une seule fois
  ['by_year','by_month','split_media','rename'].forEach(k => {
    const el = document.getElementById('opt-' + k);
    if (el && !el._bound) {
      el.checked   = wcState.options[k];
      el.onchange  = wcSyncOptions;
      el._bound    = true;
    }
  });
  const elOrder = document.getElementById('opt-order_largest');
  if (elOrder && !elOrder._bound) {
    elOrder.checked = wcState.options.order === 'largest';
    elOrder.onchange = wcSyncOptions;
    elOrder._bound  = true;
  }
  wcRenderSources();
  wcUpdatePreview();
}

/* ─── Toast notifications ───────────────────────────────── */
let _toastTimer = null;
function showToast(msg, kind = 'info', ms = 2500) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.className = '';
  if (kind === 'error')   t.classList.add('error');
  if (kind === 'success') t.classList.add('success');
  t.classList.add('show');
  clearTimeout(_toastTimer);
  _toastTimer = setTimeout(() => t.classList.remove('show'), ms);
}

/* ─── Modal de confirmation générique ────────────────────── */
let _confirmCallback = null;
function openConfirm(title, message, confirmText, onConfirm, danger = false) {
  document.getElementById('cm-title').textContent     = title;
  document.getElementById('cm-message').textContent   = message;
  const btn = document.getElementById('cm-confirm-btn');
  btn.textContent = confirmText;
  btn.className   = danger ? 'modal-btn-danger' : 'modal-btn-confirm';
  _confirmCallback = onConfirm;
  document.getElementById('confirm-modal').classList.add('show');
}
function closeConfirm() {
  document.getElementById('confirm-modal').classList.remove('show');
  _confirmCallback = null;
}
async function confirmAction() {
  const cb = _confirmCallback;
  closeConfirm();
  if (cb) await cb();
}

/* ─── Empty trash ────────────────────────────────────────── */
async function updateTrashBadge() {
  try {
    const r = await fetch('/api/trash_info');
    const j = await r.json();
    const btn   = document.getElementById('btn-empty-trash');
    const label = document.getElementById('empty-trash-label');
    if (!btn || !label) return;
    if (j.count > 0) {
      label.textContent = `Vider (${j.count} • ${j.size_human})`;
      btn.dataset.active = '1';
    } else {
      label.textContent = 'Vider';
      delete btn.dataset.active;
    }
  } catch(_) {}
}
async function confirmEmptyTrash() {
  const r = await fetch('/api/trash_info');
  const j = await r.json();
  if (j.count === 0) { showToast('Aucun fichier à supprimer.', 'info'); return; }
  openConfirm(
    'Vider la corbeille',
    `${j.count} fichier(s) (${j.size_human}) seront envoyés à la Corbeille macOS. Tu pourras les restaurer depuis le Finder si erreur.`,
    'Envoyer à la Corbeille',
    async () => {
      const rr = await fetch('/api/empty_trash', { method: 'POST' });
      const jj = await rr.json();
      if (jj.ok) {
        showToast(jj.message, 'success', 3500);
        updateTrashBadge();
      } else {
        showToast('Erreur : ' + (jj.error || 'inconnue'), 'error', 4000);
      }
    },
    false,
  );
}
setInterval(updateTrashBadge, 5000);

/* ─── Transform : rotate ─────────────────────────────────── */
async function transformRotate() {
  if (!current || !current.url) return;
  const entry = current.url.replace('/media/', '').replace(/\?t=.*$/, '');
  showToast('Rotation en cours…', 'info', 1500);
  try {
    const r = await fetch('/api/transform', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ action: 'rotate', entry, angle: 90 }),
    });
    const j = await r.json();
    if (!j.ok) { showToast('Échec rotation : ' + (j.error || ''), 'error', 4000); return; }
    showToast(j.kind === 'video' ? 'Vidéo pivotée ✓' : 'Image pivotée ✓', 'success', 1500);
    bustMediaCache();
  } catch(e) {
    showToast('Erreur : ' + e.message, 'error', 4000);
  }
}

function bustMediaCache() {
  // Recharger l'élément media courant avec un cache-bust
  const t = '?t=' + Date.now();
  const img = document.querySelector('#media-wrap img');
  const v   = document.querySelector('#media-wrap video');
  if (img) img.src = current.url + t;
  if (v) {
    const cur = v.currentTime;
    v.src = current.url + t;
    v.addEventListener('loadedmetadata', () => { v.currentTime = cur; }, { once: true });
  }
}

/* ─── Crop image (drag rectangle) ────────────────────────── */
let cropState = null;

function openCrop() {
  if (!current || current.is_video) { showToast('Crop indisponible pour les vidéos — utilise R pour le trim.', 'error'); return; }
  const overlay = document.getElementById('crop-overlay');
  const img     = document.getElementById('crop-img');
  const rect    = document.getElementById('crop-rect');
  const valBtn  = document.getElementById('crop-validate');
  cropState = { active: false, startX: 0, startY: 0, x: 0, y: 0, w: 0, h: 0, naturalW: 0, naturalH: 0, dispW: 0, dispH: 0 };
  rect.style.display = 'none';
  valBtn.disabled = true;
  img.onload = () => {
    cropState.naturalW = img.naturalWidth;
    cropState.naturalH = img.naturalHeight;
    cropState.dispW    = img.clientWidth;
    cropState.dispH    = img.clientHeight;
    document.getElementById('crop-info').textContent =
      `Image ${img.naturalWidth}×${img.naturalHeight} — cliquer-glisser pour sélectionner`;
  };
  img.src = current.url + '?t=' + Date.now();
  overlay.classList.add('show');
}

function closeCrop() {
  document.getElementById('crop-overlay').classList.remove('show');
  cropState = null;
}

(function bindCropHandlers() {
  const stage = document.getElementById('crop-stage');
  const rect  = document.getElementById('crop-rect');
  const valBtn = document.getElementById('crop-validate');
  if (!stage) return;
  stage.addEventListener('mousedown', (e) => {
    if (!cropState) return;
    const r = stage.getBoundingClientRect();
    cropState.active = true;
    cropState.startX = e.clientX - r.left;
    cropState.startY = e.clientY - r.top;
    cropState.x = cropState.startX;
    cropState.y = cropState.startY;
    cropState.w = 0;
    cropState.h = 0;
    rect.style.left = cropState.x + 'px';
    rect.style.top  = cropState.y + 'px';
    rect.style.width  = '0px';
    rect.style.height = '0px';
    rect.style.display = 'block';
    valBtn.disabled = true;
  });
  stage.addEventListener('mousemove', (e) => {
    if (!cropState || !cropState.active) return;
    const r = stage.getBoundingClientRect();
    const cx = Math.max(0, Math.min(cropState.dispW, e.clientX - r.left));
    const cy = Math.max(0, Math.min(cropState.dispH, e.clientY - r.top));
    cropState.x = Math.min(cropState.startX, cx);
    cropState.y = Math.min(cropState.startY, cy);
    cropState.w = Math.abs(cx - cropState.startX);
    cropState.h = Math.abs(cy - cropState.startY);
    rect.style.left   = cropState.x + 'px';
    rect.style.top    = cropState.y + 'px';
    rect.style.width  = cropState.w + 'px';
    rect.style.height = cropState.h + 'px';
  });
  stage.addEventListener('mouseup', () => {
    if (!cropState) return;
    cropState.active = false;
    if (cropState.w > 8 && cropState.h > 8) {
      valBtn.disabled = false;
      document.getElementById('crop-info').textContent =
        `Zone : ${Math.round(cropState.w * cropState.naturalW / cropState.dispW)}×${Math.round(cropState.h * cropState.naturalH / cropState.dispH)} px`;
    } else {
      valBtn.disabled = true;
    }
  });
})();

async function validateCrop() {
  if (!cropState || cropState.w < 8 || cropState.h < 8) return;
  // Convertir coords display → coords image source
  const scaleX = cropState.naturalW / cropState.dispW;
  const scaleY = cropState.naturalH / cropState.dispH;
  const payload = {
    action: 'crop',
    entry:  current.url.replace('/media/', '').replace(/\?t=.*$/, ''),
    x: Math.round(cropState.x * scaleX),
    y: Math.round(cropState.y * scaleY),
    w: Math.round(cropState.w * scaleX),
    h: Math.round(cropState.h * scaleY),
  };
  document.getElementById('crop-validate').disabled = true;
  document.getElementById('crop-validate').textContent = 'Application…';
  try {
    const r = await fetch('/api/transform', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(payload),
    });
    const j = await r.json();
    if (!j.ok) { showToast('Échec crop : ' + (j.error || ''), 'error', 4000); return; }
    showToast(`Image recadrée (${j.crop.w}×${j.crop.h}) ✓`, 'success');
    closeCrop();
    bustMediaCache();
  } catch(e) {
    showToast('Erreur : ' + e.message, 'error', 4000);
  } finally {
    document.getElementById('crop-validate').textContent = '✓ Valider';
  }
}

/* ─── Trim vidéo (double-handle timeline) ────────────────── */
let trimState = null;

function openTrim() {
  if (!current || !current.is_video) { showToast('Trim disponible uniquement pour les vidéos.', 'error'); return; }
  const overlay = document.getElementById('trim-overlay');
  const v       = document.getElementById('trim-video');
  trimState = { start: 0, end: 0, duration: 0 };
  v.src = current.url + '?t=' + Date.now();
  v.addEventListener('loadedmetadata', () => {
    trimState.duration = v.duration;
    trimState.start    = 0;
    trimState.end      = v.duration;
    document.getElementById('trim-total-time').textContent = fmtSec(v.duration);
    updateTrimUI();
  }, { once: true });
  overlay.classList.add('show');
}

function closeTrim() {
  document.getElementById('trim-overlay').classList.remove('show');
  const v = document.getElementById('trim-video');
  v.pause();
  v.removeAttribute('src');
  v.load();
  trimState = null;
}

function fmtSec(s) {
  if (!s || isNaN(s)) return '0:00';
  const m   = Math.floor(s / 60);
  const sec = Math.floor(s % 60);
  return `${m}:${sec.toString().padStart(2,'0')}`;
}

function updateTrimUI() {
  if (!trimState) return;
  const pctStart = trimState.duration ? (trimState.start / trimState.duration) * 100 : 0;
  const pctEnd   = trimState.duration ? (trimState.end   / trimState.duration) * 100 : 100;
  const sel = document.getElementById('trim-selection');
  sel.style.left  = pctStart + '%';
  sel.style.right = (100 - pctEnd) + '%';
  document.getElementById('trim-start-time').textContent = fmtSec(trimState.start);
  document.getElementById('trim-end-time').textContent   = fmtSec(trimState.end);
}

(function bindTrimHandlers() {
  const track = document.getElementById('trim-track');
  const hStart = document.getElementById('trim-handle-start');
  const hEnd   = document.getElementById('trim-handle-end');
  if (!track) return;
  let dragging = null;

  const onMove = (e) => {
    if (!trimState || !dragging) return;
    const r   = track.getBoundingClientRect();
    const pct = Math.max(0, Math.min(1, (e.clientX - r.left) / r.width));
    const t   = pct * trimState.duration;
    const v   = document.getElementById('trim-video');
    if (dragging === 'start') {
      trimState.start = Math.max(0, Math.min(trimState.end - 0.1, t));
      v.currentTime = trimState.start;
    } else if (dragging === 'end') {
      trimState.end = Math.max(trimState.start + 0.1, Math.min(trimState.duration, t));
      v.currentTime = trimState.end;
    }
    updateTrimUI();
  };

  hStart.addEventListener('mousedown', (e) => { dragging = 'start'; e.preventDefault(); });
  hEnd.addEventListener('mousedown',   (e) => { dragging = 'end';   e.preventDefault(); });
  window.addEventListener('mousemove', onMove);
  window.addEventListener('mouseup',   () => { dragging = null; });
})();

async function validateTrim() {
  if (!trimState || trimState.end <= trimState.start + 0.1) {
    showToast('Sélection trop courte.', 'error'); return;
  }
  const entry = current.url.replace('/media/', '').replace(/\?t=.*$/, '');
  const startedAt = Date.now();
  // Lance le worker async côté serveur (ré-encodage ffmpeg avec progression).
  let r, j;
  try {
    r = await fetch('/api/trim/start', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ entry, start_s: trimState.start, end_s: trimState.end }),
    });
    j = await r.json();
  } catch (e) {
    showToast('Erreur réseau : ' + e.message, 'error', 5000); return;
  }
  if (!j.ok) { showToast('Échec trim : ' + (j.error || ''), 'error', 5000); return; }

  closeTrim();
  showProcessing('Découpage de la vidéo…', true);

  // Polling de la progression toutes les 500 ms.
  await new Promise((resolve) => {
    procTimer = setInterval(async () => {
      let s;
      try {
        const sr = await fetch('/api/trim/status');
        s = await sr.json();
      } catch (_) { return; }
      const elapsed = Math.round((Date.now() - startedAt) / 1000);
      updateProcessing(s.percent || 0, elapsed);
      if (s.done) {
        clearInterval(procTimer); procTimer = null;
        if (s.ok) {
          hideProcessing();
          showToast('Vidéo coupée ✓', 'success');
          bustMediaCache();
        } else {
          showProcessingError(s.error || 'Échec du découpage.');
        }
        resolve();
      }
    }, 500);
  });
}

function openCropOrTrim() {
  if (!current) return;
  if (current.is_video) openTrim();
  else                  openCrop();
}

/* ════════ Mode galerie + filtre type + overlay traitement (v0.7.0) ════════ */

let triageMode   = localStorage.getItem('triageMode') || 'single';
let galleryItems = [];
let galleryMarks = {};
let galleryFocus = 0;
let procTimer    = null;

const FILTER_LABELS = [['all','Tout'], ['photo','Photos'], ['video','Vidéos']];

function currentFilter() {
  return (current && current.filter) || window._filter || 'all';
}

function refreshFilterUI() {
  const f = currentFilter();
  window._filter = f;
  ['triage-filter', 'triage-filter-gal'].forEach((id) => {
    const host = document.getElementById(id);
    if (!host) return;
    host.innerHTML = FILTER_LABELS.map(([v, l]) =>
      `<button class="tf-btn ${v === f ? 'active' : ''}" onclick="setTriageFilter('${v}')">${l}</button>`
    ).join('');
  });
}

async function setTriageFilter(value) {
  try {
    await fetch('/api/triage_filter', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ value }),
    });
  } catch (_) {}
  window._filter = value;
  if (triageMode === 'gallery') { await renderGalleryMode(); refreshFilterUI(); }
  else load();
}

function setTriageMode(mode) {
  triageMode = mode;
  localStorage.setItem('triageMode', mode);
  load();
}

async function renderGalleryMode() {
  document.getElementById('stage').style.display         = 'none';
  document.getElementById('group-stage').style.display   = 'none';
  document.getElementById('sem-stage').style.display     = 'none';
  document.getElementById('bar').style.display           = 'none';
  document.getElementById('topbar').classList.remove('show');
  document.getElementById('progress-wrap').style.display = 'none';
  document.getElementById('gallery-stage').classList.add('show');
  refreshFilterUI();

  const grid = document.getElementById('gallery-grid');
  grid.innerHTML = '<p style="color:var(--txt-3);grid-column:1/-1;padding:20px">Chargement…</p>';
  galleryMarks = {}; galleryFocus = 0;
  updateGalleryMarks();

  const f = currentFilter();
  let d;
  try {
    const r = await fetch(`/api/gallery?scope=queue&type=${f}&sort=date_asc`);
    d = await r.json();
  } catch (e) {
    grid.innerHTML = '<p style="color:var(--del);grid-column:1/-1;padding:20px">Erreur réseau.</p>'; return;
  }
  if (!d.ok) {
    grid.innerHTML = `<p style="color:var(--del);grid-column:1/-1;padding:20px">${d.error || 'Erreur'}</p>`; return;
  }

  galleryItems = d.items;
  document.getElementById('gallery-count').textContent = `· ${d.count} à trier · ${d.total_mb} Mo`;
  if (!galleryItems.length) {
    grid.innerHTML = '<p style="color:var(--txt-3);grid-column:1/-1;padding:30px;text-align:center">Aucun média à trier pour ce filtre.<br>Change de filtre ou reviens au tri un-par-un.</p>';
    return;
  }
  grid.innerHTML = '';
  galleryItems.forEach((it, i) => {
    const cell = document.createElement('div');
    cell.className = 'g-cell'; cell.tabIndex = -1; cell.dataset.i = i;
    // Poster JPEG pour les vidéos (un <img> ne peut pas afficher un mp4).
    const thumb   = it.is_video ? ('/preview/' + it.rel) : (it.preview_url || it.url);
    const sizeStr = it.size_kb >= 1024 ? (it.size_kb / 1024).toFixed(1) + ' Mo' : it.size_kb + ' Ko';
    cell.innerHTML =
      `<div class="g-mark"></div>
       <div class="g-thumb"><img loading="lazy" src="${thumb}" alt="${it.name}"></div>
       <div class="g-info"><span style="font-weight:600;color:#fb923c">${sizeStr}</span><span>${it.year}</span></div>
       <div class="g-name" title="${it.name}">${it.is_video ? '🎬 ' : '🖼 '}${it.name}</div>`;
    cell.onclick       = () => cycleMark(i);
    cell.oncontextmenu = (ev) => { ev.preventDefault(); markCell(i, 'trash'); };
    grid.appendChild(cell);
  });
  focusCell(0);
}

function colCount() {
  const cells = document.querySelectorAll('#gallery-grid .g-cell');
  if (cells.length < 2) return 1;
  const top0 = cells[0].offsetTop;
  let c = 0;
  for (const el of cells) { if (el.offsetTop === top0) c++; else break; }
  return Math.max(1, c);
}

function focusCell(i) {
  const cells = document.querySelectorAll('#gallery-grid .g-cell');
  if (!cells.length) return;
  galleryFocus = Math.max(0, Math.min(cells.length - 1, i));
  const el = cells[galleryFocus];
  el.focus({ preventScroll: false });
  el.scrollIntoView({ block: 'nearest' });
}

function _paintCell(i, mark) {
  const cell = document.querySelector(`#gallery-grid .g-cell[data-i="${i}"]`);
  if (!cell) return;
  cell.classList.toggle('keep',  mark === 'keep');
  cell.classList.toggle('trash', mark === 'trash');
  const m = cell.querySelector('.g-mark');
  if (m) m.textContent = mark === 'keep' ? '✓' : (mark === 'trash' ? '✕' : '');
}

function markCell(i, mark) {
  const it = galleryItems[i]; if (!it) return;
  if (galleryMarks[it.rel] === mark) delete galleryMarks[it.rel];
  else galleryMarks[it.rel] = mark;
  _paintCell(i, galleryMarks[it.rel]);
  galleryFocus = i;
  updateGalleryMarks();
}

function cycleMark(i) {
  const it = galleryItems[i]; if (!it) return;
  const cur  = galleryMarks[it.rel];
  const next = cur === undefined ? 'keep' : (cur === 'keep' ? 'trash' : undefined);
  if (next === undefined) delete galleryMarks[it.rel]; else galleryMarks[it.rel] = next;
  _paintCell(i, next);
  galleryFocus = i;
  updateGalleryMarks();
}

function clearMark(i) {
  const it = galleryItems[i]; if (!it) return;
  delete galleryMarks[it.rel];
  _paintCell(i, undefined);
  updateGalleryMarks();
}

function updateGalleryMarks() {
  const vals = Object.values(galleryMarks);
  const nk = vals.filter(v => v === 'keep').length;
  const nt = vals.filter(v => v === 'trash').length;
  const marksEl = document.getElementById('gallery-marks');
  if (marksEl) marksEl.textContent = `${nk} gardé · ${nt} supprimé`;
  const vbtn = document.getElementById('gallery-validate');
  if (vbtn) vbtn.disabled = (nk + nt) === 0;
}

async function validateGalleryBatch() {
  const keep = [], trash = [];
  Object.entries(galleryMarks).forEach(([rel, m]) => { (m === 'keep' ? keep : trash).push(rel); });
  if (!keep.length && !trash.length) return;
  const btn = document.getElementById('gallery-validate');
  btn.disabled = true; btn.textContent = 'Application…';
  try {
    const r = await fetch('/api/gallery_action', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ keep, trash }),
    });
    const j = await r.json();
    if (!j.ok) {
      showToast('Erreur : ' + (j.error || ''), 'error', 4000);
      btn.disabled = false; btn.textContent = 'Valider le lot →'; return;
    }
    showToast(`${j.processed} média(s) traité(s) ✓`, 'success');
    btn.textContent = 'Valider le lot →';
    load();   // reste en galerie sur les restants, ou bascule sur l'écran terminé si vide
  } catch (e) {
    showToast('Erreur réseau : ' + e.message, 'error', 4000);
    btn.disabled = false; btn.textContent = 'Valider le lot →';
  }
}

/* Clavier galerie : navigation + marquage + validation */
document.addEventListener('keydown', (e) => {
  if (triageMode !== 'gallery') return;
  if (!document.getElementById('gallery-stage').classList.contains('show')) return;
  if (document.getElementById('processing-overlay').classList.contains('show')) { e.preventDefault(); return; }
  if (e.target.matches('input, textarea, select')) return;
  if (!galleryItems.length) {
    if (e.key === 'Escape') { e.preventDefault(); setTriageMode('single'); }
    return;
  }
  const cols = colCount();
  switch (e.key) {
    case 'ArrowRight': e.preventDefault(); focusCell(galleryFocus + 1); break;
    case 'ArrowLeft':  e.preventDefault(); focusCell(galleryFocus - 1); break;
    case 'ArrowDown':  e.preventDefault(); focusCell(galleryFocus + cols); break;
    case 'ArrowUp':    e.preventDefault(); focusCell(galleryFocus - cols); break;
    case ' ':
    case 'k': case 'K': e.preventDefault(); markCell(galleryFocus, 'keep');  break;
    case 'd': case 'D':
    case 'Delete':      e.preventDefault(); markCell(galleryFocus, 'trash'); break;
    case 'u': case 'U': e.preventDefault(); clearMark(galleryFocus); break;
    case 'Enter':       e.preventDefault(); validateGalleryBatch(); break;
    case 'Escape':      e.preventDefault(); setTriageMode('single'); break;
  }
});

/* Overlay de traitement (trim async, avec progression réelle) */
function showProcessing(title, determinate) {
  document.getElementById('proc-title').textContent   = title || 'Traitement en cours…';
  document.getElementById('proc-error').style.display = 'none';
  document.getElementById('proc-close').style.display = 'none';
  document.getElementById('proc-warn').style.display    = '';
  document.getElementById('proc-spinner').style.display = '';
  const wrap = document.getElementById('proc-bar-wrap');
  wrap.style.display = '';
  wrap.classList.toggle('indeterminate', !determinate);
  document.getElementById('proc-bar').style.width  = determinate ? '0%' : '35%';
  document.getElementById('proc-meta').textContent = '';
  document.getElementById('processing-overlay').classList.add('show');
}

function updateProcessing(percent, elapsedSec) {
  document.getElementById('proc-bar-wrap').classList.remove('indeterminate');
  document.getElementById('proc-bar').style.width = Math.max(0, Math.min(100, percent)) + '%';
  document.getElementById('proc-meta').textContent = `${percent}%  ·  ${elapsedSec}s écoulées`;
}

function hideProcessing() {
  if (procTimer) { clearInterval(procTimer); procTimer = null; }
  document.getElementById('processing-overlay').classList.remove('show');
}

function showProcessingError(msg) {
  document.getElementById('proc-spinner').style.display  = 'none';
  document.getElementById('proc-warn').style.display     = 'none';
  document.getElementById('proc-bar-wrap').style.display = 'none';
  document.getElementById('proc-meta').textContent       = '';
  const err = document.getElementById('proc-error');
  err.style.display = ''; err.textContent = msg;
  document.getElementById('proc-close').style.display = '';
}

/* CTA écran terminé : recherche manuelle de nouveaux médias */
async function doRefreshQueue() {
  showToast('Recherche de nouveaux médias…', 'info', 1500);
  try {
    const r = await fetch('/api/refresh_queue', { method: 'POST' });
    const j = await r.json();
    if (j.ok && j.added > 0) showToast(`${j.added} nouveau(x) média(s) ajouté(s) ✓`, 'success');
    else if (j.ok)           showToast('Aucun nouveau média trouvé.', 'info', 2000);
  } catch (_) {}
  load();
}

/* ─── Keyboard : T (rotation) + R (crop/trim) + Escape ──── */
document.addEventListener('keydown', (e) => {
  // Pendant un traitement, on bloque tout (y compris Échap) pour éviter de quitter.
  if (document.getElementById('processing-overlay').classList.contains('show')) {
    e.preventDefault(); return;
  }
  // Si une modal ou overlay est ouvert, gérer Echap d'abord
  if (e.key === 'Escape') {
    if (document.getElementById('crop-overlay').classList.contains('show'))    { closeCrop();    e.preventDefault(); return; }
    if (document.getElementById('trim-overlay').classList.contains('show'))    { closeTrim();    e.preventDefault(); return; }
    if (document.getElementById('confirm-modal').classList.contains('show'))   { closeConfirm(); e.preventDefault(); return; }
    return;
  }
  // Bloquer si dans un input/textarea ou welcome view actif
  if (e.target.matches('input, textarea, select')) return;
  if (getComputedStyle(document.getElementById('welcome')).display !== 'none') return;
  // Bloquer si overlay actif (crop/trim handlers gérés dans la modale)
  if (document.getElementById('crop-overlay').classList.contains('show')) return;
  if (document.getElementById('trim-overlay').classList.contains('show')) return;

  if (e.key === 't' || e.key === 'T') { transformRotate(); e.preventDefault(); return; }
  if (e.key === 'r' || e.key === 'R') { openCropOrTrim(); e.preventDefault(); return; }
});

/* Mise à jour conditionnelle du bouton crop/trim selon le type */
(function updateCropButtonVisibility() {
  const obs = setInterval(() => {
    const btn = document.getElementById('btn-crop');
    if (!btn) return;
    if (!current || current.mode === 'needs_config' || current.mode === 'trial_limit' || current.done) {
      btn.style.display = 'none';
      return;
    }
    btn.style.display = '';
    btn.innerHTML = current.is_video
      ? '✂ Découper <span class="key">R</span>'
      : '✂ Rogner <span class="key">R</span>';
  }, 800);
})();

/* ─── Auto-update (v0.5.0+) ─────────────────────────────── */

function fmtMB(bytes) {
  if (!bytes) return '0 MB';
  return (bytes / (1024 * 1024)).toFixed(1) + ' MB';
}

async function initUpdateUI() {
  // Affiche la version courante sans attendre le check
  try {
    const r = await fetch('/api/update/status');
    const s = await r.json();
    const v = document.getElementById('wc-current-version');
    if (v && s.current) v.textContent = 'v' + s.current;
  } catch (e) { /* silent */ }

  // Check silencieux au démarrage : 1s après load pour ne pas bloquer le boot
  setTimeout(() => checkForUpdate(false), 1000);
}

async function checkForUpdate(verbose) {
  const btn  = document.getElementById('wc-check-update');
  const msg  = document.getElementById('wc-update-msg');
  if (btn) { btn.disabled = true; btn.textContent = 'Vérification…'; }
  if (verbose && msg) msg.textContent = '';

  try {
    await fetch('/api/update/check', { method: 'POST' });
  } catch (e) {
    if (msg) msg.textContent = 'Connexion impossible.';
    if (btn) { btn.disabled = false; btn.textContent = 'Vérifier maintenant'; }
    return;
  }

  // Poll status jusqu'à fin du check
  let tries = 0;
  const poll = async () => {
    tries++;
    let s;
    try {
      const r = await fetch('/api/update/status');
      s = await r.json();
    } catch (e) {
      if (btn) { btn.disabled = false; btn.textContent = 'Vérifier maintenant'; }
      return;
    }
    if (s.checking && tries < 30) {
      setTimeout(poll, 500);
      return;
    }
    renderUpdateState(s, verbose);
  };
  poll();
}

function renderUpdateState(s, verbose) {
  const btn    = document.getElementById('wc-check-update');
  const msg    = document.getElementById('wc-update-msg');
  const banner = document.getElementById('wc-update-banner');
  const latest = document.getElementById('wc-update-latest');
  const size   = document.getElementById('wc-update-size');

  if (btn) { btn.disabled = false; btn.textContent = 'Vérifier maintenant'; }

  if (!s.ok) {
    if (msg) msg.textContent = s.error || 'Vérification impossible.';
    if (banner) banner.style.display = 'none';
    return;
  }

  if (s.available) {
    if (latest) latest.textContent = s.latest || '';
    if (size)   size.textContent   = s.size ? `Taille : ${fmtMB(s.size)}` : '';
    if (banner) banner.style.display = 'flex';
    if (msg) msg.textContent = '';
  } else {
    if (banner) banner.style.display = 'none';
    if (msg && verbose) msg.textContent = `Vous avez la dernière version (v${s.current}).`;
  }
}

async function installUpdate() {
  const installBtn = document.getElementById('wc-install-update');
  if (installBtn) installBtn.disabled = true;

  // Ouvre le modal
  document.getElementById('update-modal').classList.add('on');
  document.getElementById('um-error').style.display = 'none';
  document.getElementById('um-cancel').style.display = 'none';
  document.getElementById('um-bar').style.width = '0%';
  document.getElementById('um-pct').textContent = '0%';
  document.getElementById('um-bytes').textContent = '0 / 0 MB';
  document.getElementById('um-title').textContent = 'Téléchargement en cours…';

  try {
    const r = await fetch('/api/update/install', { method: 'POST' });
    const j = await r.json();
    if (!j.ok) {
      showUpdateError(j.error || 'Erreur de déclenchement');
      return;
    }
  } catch (e) {
    showUpdateError('Connexion impossible.');
    return;
  }

  // Poll download progress
  const poll = async () => {
    let s;
    try {
      const r = await fetch('/api/update/status');
      s = await r.json();
    } catch (e) {
      showUpdateError('Connexion perdue pendant le téléchargement.');
      return;
    }

    if (s.install_err) {
      showUpdateError(s.install_err);
      return;
    }

    if (s.downloading || s.installing) {
      const total = s.dl_total || 0;
      const done  = s.dl_done  || 0;
      const pct   = total > 0 ? Math.round((done / total) * 100) : 0;
      document.getElementById('um-bar').style.width = pct + '%';
      document.getElementById('um-pct').textContent = pct + '%';
      document.getElementById('um-bytes').textContent = fmtMB(done) + ' / ' + fmtMB(total);
      if (!s.downloading && s.installing) {
        document.getElementById('um-title').textContent = 'Installation en cours…';
        document.getElementById('um-detail').textContent = 'Sort Memories va se relancer dans un instant.';
      }
      setTimeout(poll, 400);
      return;
    }

    // Si on arrive ici sans erreur, c'est que l'install s'est faite (l'app va quitter)
    // mais comme l'app peut prendre 1-2s avant os._exit, on continue à poll
    setTimeout(poll, 500);
  };
  poll();
}

function showUpdateError(msg) {
  document.getElementById('um-title').textContent = 'Échec de la mise à jour';
  const err = document.getElementById('um-error');
  err.textContent = msg;
  err.style.display = '';
  document.getElementById('um-cancel').style.display = '';
  const installBtn = document.getElementById('wc-install-update');
  if (installBtn) installBtn.disabled = false;
}

function closeUpdateModal() {
  document.getElementById('update-modal').classList.remove('on');
}

load();
updateTrashBadge();
initUpdateUI();
</script>
</body>
</html>"""

# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def run_server(port: int = 7777, open_browser: bool = True):
    """Lance le serveur Flask. Utilisé par app.py (open_browser=False) et CLI dev."""
    url = f"http://127.0.0.1:{port}"
    _load_session_config()
    print(f"\n📁  Sort Memories  —  {url}")
    print(f"   State folder : {STATE_DIR}")
    if _is_configured():
        print(f"   Sources      : {', '.join(_session_config['sources'])}")
        print(f"   Options      : {_session_config['options']}")
    else:
        print("   Session      : non configurée (UI affichera l'accueil)")
    print(f"   CLIP         : {'enabled' if CLIP_AVAILABLE else 'disabled (install torch + open_clip_torch)'}")
    print("   Raccourcis   : → Garder | ← Retour | D Supprimer | O Overlay\n")
    _init_mem()
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    run_server()
