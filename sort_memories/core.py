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
import json, os, shutil, threading, webbrowser, subprocess, tempfile
from pathlib import Path
from itertools import combinations
from collections import defaultdict
from PIL import Image
import imagehash
from flask import Flask, request, jsonify, send_file, render_template_string

try:
    import appdirs
    _STATE_DEFAULT = Path(appdirs.user_data_dir("SortMemories", "EliottBouquerel"))
except ImportError:
    _STATE_DEFAULT = Path.home() / ".sort-memories"

try:
    import numpy as np
    import torch
    import open_clip as _open_clip
    CLIP_AVAILABLE = True
except ImportError:
    CLIP_AVAILABLE = False

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
MEDIA_EXT      = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mov"}
HASH_THRESHOLD = 10   # distance Hamming ≤ 10/64 bits (~15%) — couvre ré-encodage, resize, changement format
VIDEO_FRAMES   = 5
VIDEO_MATCH    = 3    # frames minimum correspondantes sur VIDEO_FRAMES

app = Flask(__name__)

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
    import datetime, re
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

def _init_mem():
    """Charge tout en mémoire au démarrage. Appelé une seule fois."""
    global _mem_hash_cache, _mem_groups, _mem_file2group, _mem_state
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
    # Groupes sémantiques CLIP
    if CLIP_GROUPS_PATH.exists():
        try:
            cg = json.loads(CLIP_GROUPS_PATH.read_text())
            _mem_clip_groups     = cg.get("groups", {})
            _mem_clip_file2group = cg.get("file_to_group", {})
        except Exception:
            pass

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
        img = Image.open(p)
        res = list(img.size)
        h   = str(imagehash.phash(img))
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
        1 for h1, h2 in zip(hashes1, hashes2)
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
    """Tous les médias sous MEDIA_DIR, à scanner pour pHash/CLIP (inclut Gardés/)."""
    result = []
    skip_dirs = {TRASH_DIR.name}
    for f in sorted(BASE.rglob("*")):
        if not f.is_file():
            continue
        if f.suffix.lower() not in MEDIA_EXT:
            continue
        if f.name.startswith(".") or f.name.endswith("-overlay.png"):
            continue
        if any(part in skip_dirs for part in f.relative_to(BASE).parts):
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
    p   = BASE / rel
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
        for bi, j in zip(rows, cols):
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
                    if ext in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
                        h, res              = compute_image_hash(p)
                        entry["hash"]       = h
                        entry["resolution"] = res
                    elif ext in {".mp4", ".mov"}:
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
                     if f.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp", ".gif"}]

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

def _year_label(rel: str) -> str:
    """Heuristique année pour l'affichage et le rangement Gardés/<year>/.

    Ordre : (1) premier dossier si c'est 4 chiffres, (2) regex YYYY en début de filename
    (Snapchat convention), (3) année du mtime du fichier, (4) "divers".
    """
    import re
    parts = Path(rel).parts
    if parts and parts[0].isdigit() and len(parts[0]) == 4:
        return parts[0]
    m = re.match(r"^(\d{4})[-_]", Path(rel).name)
    if m:
        return m.group(1)
    try:
        import datetime
        mtime = (BASE / rel).stat().st_mtime
        return str(datetime.datetime.fromtimestamp(mtime).year)
    except Exception:
        return "divers"


def collect_files():
    """Collecte tous les médias sous MEDIA_DIR (récursif).

    Exclusions :
    - Fichiers cachés (.*)
    - Dossier trash (_a_supprimer/) et dossier Gardés/ (résultat du triage précédent)
    - Overlays Snapchat (*-overlay.png)
    """
    files = []
    skip_dirs = {TRASH_DIR.name, "Gardés", "Gardes", ".DS_Store"}
    for f in sorted(BASE.rglob("*")):
        if not f.is_file():
            continue
        if f.suffix.lower() not in MEDIA_EXT:
            continue
        if f.name.startswith(".") or f.name.endswith("-overlay.png"):
            continue
        # Skip si dans un dossier exclus
        if any(part in skip_dirs for part in f.relative_to(BASE).parts):
            continue
        files.append(str(f.relative_to(BASE)))
    return files

def find_overlay(rel):
    p    = BASE / rel
    stem = p.stem
    if not stem.endswith("-main"):
        return None
    overlay = p.parent / f"{stem[:-5]}-overlay.png"
    return str(overlay.relative_to(BASE)) if overlay.exists() else None

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

def move_to_gardes(rel: str, overlay_rel: str = None, overlay_visible: bool = True) -> dict:
    """
    Déplace immédiatement un fichier vers Gardés/YYYY/.
    Fusionne l'overlay si visible, le trash sinon.
    Retourne un dict avec les chemins pour le undo.
    """
    src    = BASE / rel
    year   = _year_label(rel)
    dst_dir = BASE / "Gardés" / year
    dst_dir.mkdir(parents=True, exist_ok=True)
    dst = dst_dir / src.name
    n   = 1
    while dst.exists():
        dst = dst_dir / f"{src.stem}_{n}{src.suffix}"
        n  += 1

    result = {"original": rel, "kept_path": str(dst.relative_to(BASE))}

    if overlay_rel:
        ov_src = BASE / overlay_rel
        if overlay_visible and ov_src.exists():
            try:
                merge_overlay(src, ov_src)
                ov_src.unlink()
                result["overlay_merged"] = overlay_rel
            except Exception:
                pass
        elif not overlay_visible and ov_src.exists():
            odst = trash_file(overlay_rel)
            result["overlay_trashed"]    = overlay_rel
            result["overlay_trash_path"] = odst

    shutil.move(str(src), str(dst))
    return result

def trash_file(rel):
    src = BASE / rel
    if not src.exists():
        return None
    TRASH_DIR.mkdir(exist_ok=True)
    dst = TRASH_DIR / src.name
    n   = 1
    while dst.exists():
        dst = TRASH_DIR / f"{src.stem}_{n}{src.suffix}"
        n  += 1
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

@app.route("/api/state")
def api_state():
    s          = load_state()
    idx, files = s["current"], s["files"]

    if idx >= len(files):
        return jsonify({"done": True, "total": len(files)})

    # Sauter les fichiers absents du disque (déjà déplacés vers Gardés/ ou trashés)
    start_idx = idx
    while idx < len(files) and not (BASE / files[idx]).exists():
        idx += 1
    if idx != start_idx:
        s["current"] = idx
        save_state(s)

    if idx >= len(files):
        return jsonify({"done": True, "total": len(files)})

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
                p     = BASE / r
                entry = _mem_hash_cache.get(r, {})
                res   = entry.get("resolution")
                ov    = find_overlay(r)
                return {
                    "rel":         r,
                    "url":         f"/media/{r}",
                    "is_video":    Path(r).suffix.lower() in {".mp4", ".mov"},
                    "size_kb":     round(p.stat().st_size / 1024) if p.exists() else 0,
                    "resolution":  f"{res[0]}×{res[1]}" if res else "?",
                    "date":        r.split("/")[-1].split("_")[0] if "/" in r else "?",
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
                p     = BASE / r
                entry = _mem_hash_cache.get(r, {})
                res   = entry.get("resolution")
                ov    = find_overlay(r)
                return {
                    "rel":         r,
                    "url":         f"/media/{r}",
                    "is_video":    Path(r).suffix.lower() in {".mp4", ".mov"},
                    "size_kb":     round(p.stat().st_size / 1024) if p.exists() else 0,
                    "resolution":  f"{res[0]}×{res[1]}" if res else "?",
                    "date":        r.split("/")[-1].split("_")[0] if "/" in r else "?",
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
    ext     = Path(rel).suffix.lower()
    overlay = find_overlay(rel)
    return jsonify({
        "mode":        "single",
        "done":        False,
        "index":       idx,
        "total":       len(files),
        "name":        Path(rel).name,
        "year":        _year_label(rel),
        "url":         f"/media/{rel}",
        "is_video":    ext in {".mp4", ".mov"},
        "can_back":    len(s["history"]) > 0,
        "overlay_url": f"/media/{overlay}" if overlay else None,
        "overlay_rel": overlay,
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
                    shutil.move(tp, str(BASE / item["file"]))
                otp = item.get("overlay_trash_path")
                if otp and Path(otp).exists():
                    shutil.move(otp, str(BASE / item["overlay_rel"]))
            # Restore kept file(s) from Gardés/
            if last["action"] == "keep_from_group":
                kept_path = last.get("kept_path")
                if kept_path and (BASE / kept_path).exists():
                    shutil.move(str(BASE / kept_path), str(BASE / last["file"]))
            elif last["action"] in ("keep_all_group", "decide_semantic_group"):
                for item in last.get("kept_items", []):
                    kp = item.get("kept_path")
                    if kp and (BASE / kp).exists():
                        shutil.move(str(BASE / kp), str(BASE / item["file"]))
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
                shutil.move(tp, str(BASE / target_file))
            if target_file not in s["files"]:
                s["files"].insert(s["current"], target_file)
            if last.get("overlay_trash_path"):
                otp = Path(last["overlay_trash_path"])
                if otp.exists():
                    shutil.move(str(otp), str(BASE / last["overlay_rel"]))
            if target_file in s["files"]:
                s["current"] = s["files"].index(target_file)

        elif last["action"] == "keep":
            target_file = last["file"]
            kept_path   = last.get("kept_path")
            if kept_path and (BASE / kept_path).exists():
                shutil.move(str(BASE / kept_path), str(BASE / target_file))
            if last.get("overlay_trashed") and last.get("overlay_trash_path"):
                otp = Path(last["overlay_trash_path"])
                if otp.exists():
                    shutil.move(str(otp), str(BASE / last["overlay_trashed"]))
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

@app.route("/media/<path:rel>")
def serve_media(rel):
    return send_file(str(BASE / rel))

@app.route("/api/reorganize", methods=["POST"])
def api_reorganize():
    s    = load_state()
    kept = BASE / "Gardés"
    moved, merged, skipped, errors = 0, 0, 0, []

    for entry in s["history"]:
        act = entry["action"]
        if act not in ("keep", "keep_from_group", "keep_all_group"):
            continue

        if act == "keep_all_group":
            files_to_move = entry.get("group_files_at_time", [])
        else:
            files_to_move = [entry.get("file")] if entry.get("file") else []

        for rel in files_to_move:
            if not rel:
                continue
            src = BASE / rel
            if not src.exists():
                skipped += 1
                continue
            if act == "keep" and entry.get("overlay_kept"):
                ov_src = BASE / entry["overlay_kept"]
                if ov_src.exists():
                    try:
                        merge_overlay(src, ov_src)
                        ov_src.unlink()
                        merged += 1
                    except Exception as e:
                        errors.append(f"{src.name}: {e}")
            parts   = Path(rel).parts
            year    = parts[0] if len(parts) > 1 and parts[0].isdigit() else "divers"
            dst_dir = kept / year
            dst_dir.mkdir(parents=True, exist_ok=True)
            dst = dst_dir / src.name
            n   = 1
            while dst.exists():
                dst = dst_dir / f"{src.stem}_{n}{src.suffix}"
                n  += 1
            shutil.move(str(src), str(dst))
            moved += 1

    s["history"] = []
    save_state(s)
    return jsonify({"ok": True, "moved": moved, "merged": merged,
                    "skipped": skipped, "errors": errors})

# ──────────────────────────────────────────────────────────────────────────────
# PAGE HTML
# ──────────────────────────────────────────────────────────────────────────────

PAGE = r"""<!DOCTYPE html>
<html lang="fr">
<head>
<meta charset="utf-8">
<title>Triage Snapchat</title>
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  html, body {
    height: 100%; background: #0d0d0d; color: #fff;
    font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
    display: flex; flex-direction: column; overflow: hidden;
  }

  #progress-wrap { height: 3px; background: #222; flex-shrink: 0; }
  #progress-bar  { height: 100%; background: #22c55e; width: 0; transition: width .25s ease; }

  /* ── Stage (single mode) ── */
  #stage {
    flex: 1; display: flex; align-items: center; justify-content: center;
    overflow: hidden; position: relative; padding: 8px; min-height: 0;
  }
  #media-wrap {
    position: relative; display: inline-flex;
    align-items: center; justify-content: center;
    max-width: 100%; max-height: 100%; cursor: pointer;
  }
  #main-img {
    display: block; max-width: 100%; max-height: calc(100vh - 100px);
    object-fit: contain; border-radius: 4px; cursor: default;
  }
  #main-video {
    display: block; max-width: 100%; max-height: calc(100vh - 140px);
    object-fit: contain; border-radius: 4px; background: #000;
  }
  #overlay-img {
    position: absolute; top: 0; left: 0; width: 100%; height: 100%;
    object-fit: contain; pointer-events: none;
    transition: opacity .18s ease; border-radius: 4px;
  }
  #overlay-img.hidden { opacity: 0; }
  #play-icon {
    position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
    font-size: 52px; opacity: 0; pointer-events: none; transition: opacity .15s ease;
  }
  #play-icon.show { opacity: .9; }
  #overlay-badge {
    position: absolute; top: 10px; right: 10px;
    background: rgba(15,30,60,.82); color: #7dd3fc;
    font-size: 11px; font-weight: 600; padding: 3px 9px;
    border-radius: 20px; pointer-events: none; transition: opacity .2s;
  }
  #overlay-badge.hidden { opacity: 0; }

  /* ── Group mode ── */
  #group-stage {
    flex: 1; display: none; flex-direction: column;
    padding: 8px; gap: 8px; min-height: 0; overflow: hidden;
  }
  #group-header {
    flex-shrink: 0; display: flex; align-items: center; gap: 10px;
    font-size: 13px; color: #aaa; padding: 2px 4px;
  }
  #sim-badge {
    padding: 3px 10px; border-radius: 20px; font-size: 12px; font-weight: 700;
  }
  .sim-green  { background: rgba(34,197,94,.2);  color: #4ade80; }
  .sim-orange { background: rgba(251,146,60,.2); color: #fb923c; }
  .sim-red    { background: rgba(239,68,68,.2);  color: #f87171; }

  /* ── Semantic group (CLIP) ── */
  #sem-stage {
    flex: 1; display: none; flex-direction: column;
    padding: 8px; gap: 6px; min-height: 0; overflow: hidden;
  }
  #sem-header {
    flex-shrink: 0; display: flex; align-items: center; gap: 10px;
    font-size: 13px; color: #aaa; padding: 2px 4px;
  }
  #sem-label-badge {
    padding: 3px 10px; border-radius: 20px; font-size: 12px; font-weight: 700;
    background: rgba(139,92,246,.2); color: #a78bfa;
  }
  #sem-grid {
    flex: 1; display: grid; gap: 8px; overflow: auto; min-height: 0;
  }
  .s-cell {
    display: flex; flex-direction: column; align-items: center;
    border: 2px solid #2a2a2a; border-radius: 10px; padding: 6px;
    cursor: pointer; transition: border-color .15s, background .15s;
    overflow: hidden; min-height: 0; user-select: none;
  }
  .s-cell.keep  { border-color: #22c55e; background: rgba(34,197,94,.09); }
  .s-cell.trash { border-color: #ef4444; background: rgba(239,68,68,.09); }
  .s-cell:not(.keep):not(.trash):hover { border-color: #7c3aed; background: rgba(139,92,246,.07); }
  .s-cell-badge {
    position: absolute; top: 6px; left: 50%; transform: translateX(-50%);
    font-size: 11px; font-weight: 700; padding: 3px 10px;
    border-radius: 20px; pointer-events: none; white-space: nowrap;
  }
  .s-cell.keep  .s-cell-badge { background: rgba(34,197,94,.9);  color: #fff; }
  .s-cell.trash .s-cell-badge { background: rgba(239,68,68,.9);  color: #fff; }
  .s-cell:not(.keep):not(.trash) .s-cell-badge { display: none; }
  .s-best-badge {
    font-size: 10px; color: #a78bfa; background: rgba(139,92,246,.15);
    padding: 2px 6px; border-radius: 8px;
  }
  #sem-decision-bar {
    flex-shrink: 0; display: flex; align-items: center; justify-content: center;
    gap: 12px; font-size: 12px; padding: 4px 0; color: #666;
  }
  #sem-counts { font-variant-numeric: tabular-nums; }
  #btn-sem-validate {
    background: #7c3aed; color: #fff; min-width: 140px;
  }
  #btn-sem-validate:disabled { opacity: .25; }
  #clip-indicator { font-size: 11px; color: #666; flex-shrink: 0; }

  #group-grid {
    flex: 1; display: grid; gap: 8px; overflow: auto; min-height: 0;
  }
  .g-cell {
    display: flex; flex-direction: column; align-items: center;
    border: 2px solid #2a2a2a; border-radius: 10px; padding: 6px;
    cursor: pointer; transition: border-color .15s, background .15s;
    overflow: hidden; min-height: 0;
  }
  .g-cell:hover { border-color: #22c55e; background: rgba(34,197,94,.07); }
  .g-cell:hover .g-keep-hint { opacity: 1; }
  .g-media-wrap {
    flex: 1; display: flex; align-items: center; justify-content: center;
    overflow: hidden; width: 100%; min-height: 0; position: relative;
  }
  .g-img {
    max-width: 100%; max-height: 100%; object-fit: contain; border-radius: 4px;
    display: block;
  }
  .g-video {
    max-width: 100%; max-height: 100%; object-fit: contain; border-radius: 4px;
    display: block; background: #000;
  }
  .g-overlay {
    position: absolute; top: 0; left: 0; width: 100%; height: 100%;
    object-fit: contain; pointer-events: none; border-radius: 4px;
  }
  .g-meta {
    flex-shrink: 0; display: flex; gap: 6px; flex-wrap: wrap;
    justify-content: center; padding: 4px 0 2px; font-size: 10px; color: #666;
  }
  .g-meta span { background: #1a1a1a; padding: 2px 6px; border-radius: 10px; }
  .g-keep-hint {
    position: absolute; top: 6px; left: 50%; transform: translateX(-50%);
    background: rgba(34,197,94,.9); color: #fff; font-size: 11px; font-weight: 700;
    padding: 3px 10px; border-radius: 20px; opacity: 0;
    transition: opacity .15s; pointer-events: none; white-space: nowrap;
  }

  /* ── Contrôles vidéo custom ── */
  #video-ctrl {
    flex-shrink: 0; display: none; align-items: center; gap: 10px;
    background: #111; border-top: 1px solid #1c1c1c; padding: 5px 16px; height: 36px;
  }
  #video-ctrl.on { display: flex; }
  #vc-pp { background: none; border: none; color: #ccc; font-size: 17px; cursor: pointer; padding: 0; }
  #vc-prog-wrap {
    flex: 1; height: 4px; background: #333; border-radius: 2px; cursor: pointer; position: relative;
  }
  #vc-prog-fill { height: 100%; background: #aaa; border-radius: 2px; pointer-events: none; width: 0; }
  #vc-time { font-size: 11px; color: #555; min-width: 70px; text-align: right; font-variant-numeric: tabular-nums; }
  #vc-mute { background: none; border: none; color: #888; font-size: 15px; cursor: pointer; padding: 0; }
  #vc-vol  { width: 64px; accent-color: #666; cursor: pointer; }

  /* ── Trial limit / Paywall ── */
  #trial-wall {
    display: none; flex-direction: column; align-items: center;
    justify-content: center; gap: 18px; padding: 40px; text-align: center;
  }
  #trial-wall .tw-icon { font-size: 52px; }
  #trial-wall h2 { font-size: 22px; font-weight: 700; color: #fff; }
  #trial-wall p  { color: #666; font-size: 14px; max-width: 420px; line-height: 1.6; }
  #trial-wall .tw-badge {
    background: rgba(139,92,246,.15); color: #a78bfa;
    padding: 4px 14px; border-radius: 20px; font-size: 12px; font-weight: 600;
  }
  #tw-key-wrap {
    display: flex; gap: 8px; width: 100%; max-width: 420px;
  }
  #tw-key-input {
    flex: 1; background: #1a1a1a; border: 1px solid #333; border-radius: 10px;
    color: #fff; font-size: 13px; padding: 10px 14px; outline: none;
    font-family: monospace; letter-spacing: .05em;
  }
  #tw-key-input:focus { border-color: #7c3aed; }
  #tw-activate {
    background: #7c3aed; color: #fff; border-radius: 10px;
    padding: 10px 18px; font-size: 13px; font-weight: 600; cursor: pointer;
    border: none; flex-shrink: 0; transition: opacity .15s;
  }
  #tw-activate:disabled { opacity: .4; cursor: default; }
  #tw-error { color: #f87171; font-size: 12px; min-height: 16px; }
  #tw-benefits {
    display: flex; flex-direction: column; gap: 6px;
    font-size: 13px; color: #888; text-align: left; width: 100%; max-width: 420px;
  }
  #tw-benefits span { display: flex; align-items: center; gap: 8px; }
  #tw-benefits span::before { content: "✓"; color: #4ade80; font-weight: 700; }

  /* ── Done screen ── */
  #done {
    display: none; flex-direction: column; align-items: center;
    justify-content: center; gap: 14px;
  }
  #done .icon { font-size: 64px; }
  #done h2    { font-size: 26px; font-weight: 700; }
  #done p     { color: #666; font-size: 15px; }

  /* ── Barre principale ── */
  #bar {
    flex-shrink: 0; background: #161616; border-top: 1px solid #2a2a2a;
    padding: 9px 18px; display: flex; align-items: center; gap: 11px;
  }
  #info { flex: 1; min-width: 0; }
  #info .name  { font-size: 12px; font-weight: 500; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; color: #e5e5e5; }
  #info .meta  { font-size: 11px; color: #555; margin-top: 2px; }

  #scan-indicator { font-size: 11px; color: #666; flex-shrink: 0; }
  #scan-indicator.running { color: #60a5fa; }
  #scan-indicator.done    { color: #4ade80; }
  #scan-indicator.error   { color: #f87171; }

  button {
    border: none; border-radius: 10px; padding: 9px 16px;
    font-size: 12px; font-weight: 600; cursor: pointer;
    display: flex; flex-direction: column; align-items: center;
    gap: 2px; transition: opacity .12s, transform .1s; flex-shrink: 0;
  }
  button:hover:not(:disabled) { opacity: .85; }
  button:active:not(:disabled) { transform: scale(.96); }
  button:disabled { opacity: .25; cursor: default; }
  .key { font-size: 10px; font-weight: 400; opacity: .5; }

  #btn-back      { background: #2a2a2a; color: #bbb; }
#btn-overlay   { background: #1e3a5f; color: #7dd3fc; min-width: 98px; }
  #btn-overlay.off { background: #222; color: #444; }
  #btn-keep      { background: #16a34a; color: #fff; min-width: 100px; }
  #btn-trash     { background: #991b1b; color: #fff; min-width: 100px; }
  #btn-keep-all  { background: #1e3a5f; color: #7dd3fc; }
  #btn-trash-all { background: #991b1b; color: #fff; }
  #btn-rescan    { background: none; border: 1px solid #333; color: #666; padding: 4px 8px; font-size: 10px; border-radius: 6px; }

  .flash { animation: flash .18s ease; }
  @keyframes flash { 0%,100%{opacity:1} 50%{opacity:.35} }
</style>
</head>
<body>

<div id="progress-wrap"><div id="progress-bar"></div></div>

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
    <div class="icon">✅</div>
    <h2>Tri terminé !</h2>
    <p id="done-msg"></p>
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

<!-- Contrôles vidéo (single mode) -->
<div id="video-ctrl">
  <button id="vc-pp" onclick="vcPlayPause()">⏸</button>
  <div id="vc-prog-wrap" onclick="vcSeek(event)"><div id="vc-prog-fill"></div></div>
  <span id="vc-time">0:00 / 0:00</span>
  <button id="vc-mute" onclick="vcMute()">🔊</button>
  <input id="vc-vol" type="range" min="0" max="1" step="0.05" value="1" oninput="vcVolume(this.value)">
</div>

<div id="bar">
  <button id="btn-back" onclick="act('back')" disabled>← Retour <span class="key">⌫</span></button>
  <div id="info">
    <div class="name" id="fname">Chargement…</div>
    <div class="meta" id="fmeta"></div>
  </div>
  <span id="scan-indicator">
    <button id="btn-rescan" onclick="triggerRescan()" title="Relancer l'analyse des doublons pHash">↻</button>
  </span>
  <span id="clip-indicator">
    <button id="btn-clip-rescan" onclick="triggerClipRescan()" title="Lancer l'analyse IA (CLIP ViT-L/14)">🤖</button>
  </span>
<button id="btn-overlay" onclick="toggleOverlay()" style="display:none">👁 Overlay ON <span class="key">↑</span></button>
  <button id="btn-keep-all"    onclick="actGroupAll()"      style="display:none">Garder toutes <span class="key">→</span></button>
  <button id="btn-trash-all"  onclick="actGroupTrashAll()" style="display:none">🗑 Supprimer toutes <span class="key">↓</span></button>
  <button id="btn-sem-validate" onclick="validateSemantic()" style="display:none" disabled>✓ Valider <span class="key">→</span></button>
  <button id="btn-keep" onclick="act('keep')">✓ Garder <span class="key">→</span></button>
  <button id="btn-trash" onclick="act('trash')">🗑 Supprimer <span class="key">↓ / D</span></button>
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
    document.getElementById('stage').style.display       = 'flex';
    document.getElementById('group-stage').style.display = 'none';
    document.getElementById('sem-stage').style.display   = 'none';
    document.getElementById('media-wrap').style.display  = 'none';
    const done = document.getElementById('done');
    done.style.display = 'flex';
    document.getElementById('done-msg').textContent =
      `${current.total} fichiers traités — dossier _a_supprimer prêt à vider`;
    document.getElementById('bar').style.display           = 'none';
    document.getElementById('progress-wrap').style.display = 'none';
    return;
  }

  document.getElementById('bar').style.display           = 'flex';
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
  if (current.is_video) {
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
    img.id    = 'main-img'; img.src = current.url + ts;
    wrap.insertBefore(img, wrap.firstChild);
  }

  if (current.overlay_url) {
    const ov     = document.createElement('img');
    ov.id        = 'overlay-img';
    ov.src       = current.overlay_url + ts;
    ov.className = overlayOn ? '' : 'hidden';
    wrap.insertBefore(ov, document.getElementById('overlay-badge'));
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

    if (f.is_video) {
      const v      = document.createElement('video');
      v.className  = 'g-video';
      v.src        = f.url + ts;
      v.autoplay   = true; v.loop = true; v.muted = true; v.controls = false;
      v.play().catch(() => {});
      mwrap.appendChild(v);
    } else {
      const img    = document.createElement('img');
      img.className = 'g-img';
      img.src       = f.url + ts;
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
    if (f.is_video) {
      const v     = document.createElement('video');
      v.className = 'g-video';
      v.src       = f.url + ts;
      v.autoplay  = true; v.loop = true; v.muted = true; v.controls = false;
      v.play().catch(() => {});
      mwrap.appendChild(v);
    } else {
      const img     = document.createElement('img');
      img.className = 'g-img';
      img.src       = f.url + ts;
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

load();
</script>
</body>
</html>"""

# ──────────────────────────────────────────────────────────────────────────────
# MAIN
# ──────────────────────────────────────────────────────────────────────────────

def run_server(port: int = 7777, open_browser: bool = True):
    """Lance le serveur Flask. Utilisé par app.py (open_browser=False) et CLI dev."""
    url = f"http://127.0.0.1:{port}"
    print(f"\n📁  Sort Memories  —  {url}")
    print(f"   Media folder : {MEDIA_DIR}")
    print(f"   State folder : {STATE_DIR}")
    print(f"   CLIP        : {'enabled' if CLIP_AVAILABLE else 'disabled (install torch + open_clip_torch)'}")
    print("   Raccourcis  : → Garder | ← Retour | D Supprimer | O Overlay\n")
    _init_mem()
    if open_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)


if __name__ == "__main__":
    run_server()
