# Architecture

## Vue d'ensemble

```
┌──────────────────────────────────────────────────────────────┐
│                  Sort Memories.app (macOS)                   │
│                                                              │
│  ┌────────────────────────┐    ┌────────────────────────┐   │
│  │  pywebview window      │◄──►│  Flask backend         │   │
│  │  (WKWebView native)    │    │  127.0.0.1:7777        │   │
│  │                        │    │                        │   │
│  │  - HTML inline         │    │  - Routes /api/*       │   │
│  │  - Vanilla JS UI       │    │  - Workers threadés    │   │
│  │  - Raccourcis clavier  │    │    (pHash + CLIP)      │   │
│  └────────────────────────┘    └───────────┬────────────┘   │
│                                            │                 │
│                                            ▼                 │
│                              ┌─────────────────────────┐    │
│                              │  Filesystem (user dir)  │    │
│                              │  - Scan récursif        │    │
│                              │  - Move → _a_supprimer  │    │
│                              └─────────────────────────┘    │
│                                                              │
│  ┌────────────────────────────────────────────────────────┐ │
│  │  appdirs.user_data_dir("SortMemories") :               │ │
│  │  - triage_state.json (curseur + historique)            │ │
│  │  - dedupe_cache.json (pHash cache)                     │ │
│  │  - dedupe_groups.json (groupes pHash)                  │ │
│  │  - clip_embeddings.npy + clip_index.json               │ │
│  │  - clip_groups.json                                    │ │
│  │  - license.json                                        │ │
│  └────────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────────┘
                              │
                              ▼
              ┌────────────────────────────────┐
              │  triage.eliottbouquerel.fr     │
              │  /api/verify (clé licence)     │
              │  Grace period offline : 30j    │
              └────────────────────────────────┘
```

## Stack technique

| Couche | Techno |
|---|---|
| Desktop shell | pywebview 5.x → WKWebView macOS |
| Backend | Python 3.11+, Flask 3 |
| UI | HTML inline + vanilla JS + CSS (render_template_string) |
| Détection doublons | Pillow + imagehash (pHash 64-bit) |
| Recherche sémantique | open_clip_torch (ViT-L/14, ~890 MB poids) |
| Vidéo | ffmpeg + ffprobe (binaires bundlés) |
| State | JSON sur disque (`appdirs.user_data_dir`) |
| Packaging | PyInstaller → `.app` |
| Distribution | GitHub Releases (.dmg signé + notarisé) |

## Flux de données

1. **Sélection dossier** — utilisateur choisit le dossier source via dialog macOS.
2. **Scan initial** — récursif, extensions filtrées : `.jpg .jpeg .png .gif .webp .mp4 .mov`.
3. **Indexation** — deux passes en parallèle :
   - **pHash** (rapide, ~5 ms/image) → groupes de doublons exacts ou re-encodés
   - **CLIP embeddings** (lent, ~30 ms/image sur Apple Silicon) → groupes sémantiques
4. **Triage interactif** — l'utilisateur parcourt les fichiers et les groupes :
   - `→` Garder | `←` Retour | `D` Supprimer | `O` Toggle overlay
   - Vue single, vue group (pHash), vue semantic (CLIP)
5. **Action `delete`** — déplace le fichier vers `_a_supprimer/` (réversible jusqu'à vidage manuel de la corbeille).
6. **Action `keep`** — laisse le fichier en place, incrémente le curseur dans `triage_state.json`.

## Points d'entrée

- `app.py` → entry point pywebview (production, lancé par `.app`).
- `python -m sort_memories.core` → Flask seul (dev, ouvert dans navigateur).
- Routes API (cf. `triage.py` actuel) :
  - `GET /` — UI HTML
  - `GET /api/state` — état courant
  - `POST /api/action` — keep / delete / undo
  - `GET /api/dedupe_status` `GET /api/clip_status` — avancement workers
  - `POST /api/rescan` `POST /api/clip_rescan` — relance workers
  - `GET /media/<path>` — sert le média courant
  - `POST /api/license/activate` `GET /api/license` — paywall

## Dépendances externes

- **`triage.eliottbouquerel.fr/api/verify`** : backend de vérification de licence (à monter en Phase 5).
- **`ffmpeg` / `ffprobe`** : binaires natifs, bundlés dans `.app` via PyInstaller `binaries` ou téléchargés au premier run.
- **Modèle CLIP ViT-L/14** : téléchargé par `open_clip_torch` au premier lancement (~890 MB, cache utilisateur).
