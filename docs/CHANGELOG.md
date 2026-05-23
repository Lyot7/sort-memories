# Changelog

## [2026-05-22] v0.1.0 — première release téléchargeable

**Type** : Feature
**Story** : Goal-mode "projet téléchargeable et utilisable"
**Fichiers modifiés** : `sort_memories/core.py` (nouveau, port de triage.py), `app.py` (wrapper pywebview), `build/SortMemories.spec`, `build/entitlements.plist`, `scripts/build-macos.sh`, `README.md`
**Description** : Bundle macOS .app fonctionnel (46 MB) publié en Release GitHub. Triage manuel + dedupe pHash opérationnels. CLIP/torch volontairement exclus du bundle (gain ~2 GB).
**Lecons apprises** :
- PyInstaller + entitlements_file : utiliser chemin absolu via `SPEC` var (sinon échoue au codesign --sign- adhoc)
- pywebview FOLDER_DIALOG : déprécié → `FileDialog.FOLDER` (avec fallback hasattr)
- State files namespacés par hash du MEDIA_DIR pour permettre plusieurs dossiers dans un même STATE_DIR
**Impact** : Release publique téléchargeable sur https://github.com/Lyot7/sort-memories/releases/tag/v0.1.0

## [2026-05-22] Init repo

**Type** : Docs / Config
**Story** : Phase 1 — scaffold du repo public
**Fichiers modifiés** : `README.md`, `LICENSE`, `.gitignore`, `pyproject.toml`, `app.py`, `sort_memories/__init__.py`, `docs/*.md`
**Description** : Création initiale du repo `sort-memories` sur GitHub. Choix de stack : pywebview + PyInstaller pour packager le prototype Flask existant (`~/Pictures/Snapchat/triage.py`, 2269 lignes) en `.app` macOS natif.
**Alternatives considérées** :
- Tauri + sidecar Python (plus lourd à setup, sur-ingénierie pour macOS-only au démarrage)
- Electron + sidecar Python (bundle ~450 MB, Chromium embarqué inutile)
- Rewrite Swift natif (semaines de travail, bénéfice marginal vu que le bottleneck CLIP reste en Python)
**Lecons apprises** : Pour un projet macOS-only à packager rapidement depuis un Flask existant, pywebview gagne sur la simplicité de portage (0 rewrite UI, WKWebView natif).
**Impact** : Aucun pour l'utilisateur final — Phase 1 = scaffolding seul. Le portage du code triage.py et le build .app suivent en Phase 2-3.
