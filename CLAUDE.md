# Sort Memories — instructions projet

App desktop de tri/déduplication de médias locaux (macOS). Les règles globales de `~/.claude/CLAUDE.md` s'appliquent ; ce fichier ajoute le contexte et les conventions propres au projet.

## Architecture

- **Stack** : Python 3.11 + Flask (API locale sur `127.0.0.1:7777`) + pywebview (fenêtre WKWebView native macOS). Build `.app` via PyInstaller.
- **Tout le code applicatif est dans `sort_memories/core.py`** (~5600 lignes) : backend Flask ET l'UI complète (HTML + CSS + JS) embarquée dans la string `PAGE`, rendue via `render_template_string`.
  - C'est du **Jinja** : ne jamais introduire `{{`, `}}`, `{%`, `%}` dans `PAGE` (les `${...}` JS et les `}}`/`%}` isolés sont tolérés).
  - Le **design system « Calme & Pro »** (direction Apple Photos) vit dans les tokens `:root` du bloc `<style>`. Couleurs via tokens, jamais en dur.
- **Entry point** : `app.py` (lance Flask en thread daemon + crée la fenêtre pywebview).
- **État** : `~/.config/SortMemories/` par défaut. Overrides env : `SORT_MEMORIES_STATE_DIR`, `SORT_MEMORIES_MEDIA_DIR`.
- **Auto-update** : `sort_memories/updater.py` interroge `GET /repos/Lyot7/sort-memories/releases/latest` et télécharge l'asset `.zip` dont le nom contient `macos`.

## Commandes

- Python du projet : `.venv/bin/python`.
- Lint : `.venv/bin/ruff check sort_memories/` (config dans `pyproject.toml`, `line-length = 100`).
- Serveur seul (dev navigateur, sans pywebview) : `SORT_MEMORIES_MEDIA_DIR=<dossier> .venv/bin/python -m sort_memories.core` → http://127.0.0.1:7777
- App desktop complète : `.venv/bin/python app.py`.
- Pas de suite pytest pour l'instant : vérifier via `app.test_client()` (tests end-to-end en mémoire) + inspection navigateur.

## Versioning & release — RÈGLE

**À chaque PR qui modifie le comportement ou l'UI : bumper la version ET pousser le tag.**

1. **Bumper la version** dans les deux fichiers, gardés synchrones (semver `X.Y.Z`) :
   - `sort_memories/__init__.py` → `__version__`
   - `pyproject.toml` → `version`
2. **Créer et pousser un tag annoté** au format **`vX.Y.Z`** (préfixe `v`) sur le commit de la PR :
   ```bash
   git tag -a vX.Y.Z -m "Sort Memories vX.Y.Z : <résumé>"
   git push origin vX.Y.Z
   ```
   Les merges étant des merge commits (pas de squash), le commit taggé reste dans l'historique de `main`.
3. **Pour activer l'update in-app**, le tag seul ne suffit pas : après merge sur `main`, publier une **Release GitHub** `vX.Y.Z` avec le `.app` zippé en asset (l'updater cherche un `.zip` nommé `*macos*`) :
   ```bash
   ./scripts/build-macos.sh                                   # build PyInstaller .app
   ditto -c -k --keepParent dist/SortMemories.app SortMemories-macos.zip
   gh release create vX.Y.Z SortMemories-macos.zip --title "..." --notes "..."
   ```
   **Toujours `ditto`, jamais `zip`/`zipfile` brut** : `ditto` préserve les symlinks et le bit exécutable du bundle. Un zip brut corrompt le `.app` à l'installation (bug fatal corrigé en v0.6.1).
4. Synchroniser les références de version dans `README.md` et `docs/` (CHANGELOG, ACTIVE).

**Pas de CI** : aucun workflow GitHub Actions ne build/release sur push de tag. Build et release sont **manuels**. Ne jamais supposer qu'un tag déclenche une release.

## Conventions

- **Ne jamais commiter** `build/`, `dist/`, ni un `.app` (artefacts PyInstaller).
- Documentation projet dans `docs/` (CHANGELOG, ACTIVE, ARCHITECTURE, DECISIONS) : mise à jour à chaque changement significatif.
- Copy lue par l'utilisateur (UI, docs, README) : **pas de tiret cadratin `—`** (signature IA). Utiliser deux-points, virgule, parenthèses.
- `core.py` est volumineux : éditer de façon chirurgicale (Grep + lecture ciblée), ne pas tout relire.
