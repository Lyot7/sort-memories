# Changelog

## [2026-05-24] v0.2.0 — vue d'accueil + multi-source + options de tri

**Type** : Feature majeure
**Story** : UX configurable — l'utilisateur choisit ses dossiers et la structure de sortie depuis l'app
**Fichiers modifiés** : `sort_memories/core.py` (refactor backend + UI), `app.py` (suppression du picker initial), `build/SortMemories.spec` (v0.2.0), `pyproject.toml`, `sort_memories/__init__.py`
**Description** :
- **Écran d'accueil** : carte centrée avec liste de dossiers sources éditable, options de tri en cases à cocher, aperçu live de la structure de sortie, bouton Démarrer
- **Multi-source** : N dossiers en entrée agrégés dans une seule session. Chaque source garde son propre `<source>/Tri/Gardées/` et `<source>/Tri/Supprimées/`
- **Options de tri combinables** : grouper par année / aussi par mois / séparer images-vidéos / renommer en `YYYY-MM-DD_<hash>.ext`
- **Bouton retour accueil** : `⚙ Accueil` dans la barre du bas pour reconfigurer en cours de session
- **Vue CLIP/IA masquée** : `#clip-indicator` et `#sem-stage` cachés via `display:none !important` (le code backend reste en place pour réactivation future)
- **Folder picker natif** : `/api/pick_folder` appelle `pywebview.create_file_dialog(FOLDER)` en réponse à un clic utilisateur, plus de Finder intrusif au démarrage
- **State namespacé par config** : un même STATE_DIR sert plusieurs sessions distinctes (sources × options) sans collision
- **Format entry interne** : `<src_idx>::<rel>` permet de retrouver la source d'un fichier dans le state multi-source
**Alternatives considérées** :
- Tri en-place vs dossier de sortie unique → Eliott a choisi tri en-place (clarté + isolation par source)
- IA CLIP réactivée → reportée à v0.3.0 (bundle léger reste prioritaire)
- Refactor structurel `keep_file()` en redesign complet → préféré l'approche helpers + aliasage `BASE` pour minimiser le diff
**Lecons apprises** :
- Le format `<idx>::<rel>` survit dans une URL Flask `<path:entry>` sans encoding spécifique
- `webview.create_file_dialog` doit être appelé depuis le thread main cocoa — pywebview gère ça correctement quand la window est créée depuis le main thread
- L'undo doit utiliser des paths ABSOLUS pour `kept_path` / `trash_path` (impossible de reconstruire depuis un entry relatif après un move)
**Impact** : UX complètement repensée. L'utilisateur configure tout depuis l'app, pas de finder qui s'ouvre, pas de dossier hardcodé. Release : https://github.com/Lyot7/sort-memories/releases/tag/v0.2.0

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
