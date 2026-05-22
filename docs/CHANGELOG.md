# Changelog

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
