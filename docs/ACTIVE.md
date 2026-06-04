# État actuel du projet

**Dernière mise à jour** : 2026-06-04

## En cours

v0.6.0 implémentée (tous formats + métadonnées préservées + tri par volume), au-dessus de la v0.5.0 (HEIC + auto-update). Vérifiée par tests sur fixtures. Reste : merge de `feat/v0.5.0-heic-formats` pour récupérer l'auto-update, rebuild du `.app`, test manuel de la fenêtre pywebview avant release.

## Fait récemment (v0.6.0)

- [x] **Tous formats** photo (HEIC/HEIF/AVIF/TIFF/BMP/RAW) + vidéo (AVI/MKV/M4V/WMV/3GP/MTS/WEBM…) sur toute l'app
- [x] **Métadonnées préservées** à la compression (EXIF/ICC/XMP sur WebP, `-map_metadata` sur H.265, `os.utime` mtime)
- [x] **Année fiable** via `_capture_datetime` (EXIF/creation_time/nom/mtime) — corrige le bug « tout en 2026 »
- [x] **Rangement par vraie date** de capture (`compute_keep_destination`)
- [x] **Endpoint `/preview`** JPEG pour formats non rendus par WKWebView (RAW, AVI/MKV…)
- [x] **Tri par volume** : option de file `order=largest` + galerie triable `/api/gallery`
- [x] deps `pillow-heif`/`rawpy`/`exifread` + spec PyInstaller (libheif/libraw bundlés)

## À faire (v0.2.0)

- [ ] **CLIP réactivé** — soit bundle complet (~2 GB), soit download on-demand au premier usage
- [ ] **Signature + notarisation Apple** — Developer ID Application + xcrun notarytool (supprime la friction Gatekeeper)
- [ ] **CI/CD GitHub Actions** — workflow `release.yml` sur tag `v*`, runner `macos-14`, secrets Apple
- [ ] **Icône `.icns`** — 1024×1024, branding propre (actuellement icône générique PyInstaller)
- [ ] **Backend de licence** — déploiement `triage.eliottbouquerel.fr/api/verify` + réactivation paywall
- [ ] **Landing dans le README** — screenshots / GIF démo de l'app en action
- [ ] **Tests pytest** — couverture des fonctions pures (`_year_label`, `images_similar`, `build_groups`)

## Fait récemment

- [x] v0.1.0 release (https://github.com/Lyot7/sort-memories/releases/tag/v0.1.0)
- [x] Port `triage.py` → `sort_memories/core.py` avec MEDIA_DIR / STATE_DIR séparés et state namespacé
- [x] Wrapper `app.py` pywebview + folder picker
- [x] Bundle PyInstaller `.app` 46 MB (torch/open_clip exclus)
- [x] Script `scripts/build-macos.sh` reproductible
- [x] Scaffold initial repo public

## Bugs connus

- Pas encore de signature Apple → Gatekeeper bloque la première ouverture (workaround clic droit > Ouvrir documenté dans README)
- pywebview deprecation `FOLDER_DIALOG` (warning seulement, comportement OK — code utilise déjà `FileDialog.FOLDER` quand dispo)

## Dette technique

- Pas de `requirements.txt` figé — venv repose sur `pip install` direct des deps. À générer via `uv pip compile pyproject.toml` quand les versions stabilisées.
- Pas de tests pytest — à ajouter sur les fonctions pures (`_year_label`, `images_similar`, hash compute)
- UI HTML inline dans `core.py` (2000 lignes de `render_template_string`) — pas un problème immédiat mais à séparer en templates Jinja si modifs lourdes UI
