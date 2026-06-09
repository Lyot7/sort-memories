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

**À chaque PR qui modifie le comportement ou l'UI : bumper la version. La release est automatique au merge.**

1. **Bumper la version** dans les deux fichiers, gardés synchrones (semver `X.Y.Z`) :
   - `sort_memories/__init__.py` → `__version__`
   - `pyproject.toml` → `version`
2. **Synchroniser** les références de version dans `README.md` et `docs/` (CHANGELOG, ACTIVE).
3. **Au merge dans `main`, la CI fait le reste** (`.github/workflows/release.yml`, runner macOS) : si la version n'a pas encore de Release GitHub, elle build le `.app` (PyInstaller), le zippe via `ditto` (asset `SortMemories-macos-vX.Y.Z.zip`, nom contenant `macos` que l'updater matche) et publie la **Release `vX.Y.Z`**. Idempotent : si la Release existe déjà, le job ne fait rien. L'update in-app se déclenche alors tout seul.

Donc : **plus de tag ni de release manuels**. Un simple bump de version + merge suffit. Pour re-publier sans changer de version, supprimer d'abord la Release existante (le garde-fou bloque sinon).

**`ditto` obligatoire** (encodé dans le workflow) : préserve symlinks + bit exécutable du bundle. Un `zip`/`zipfile` brut corrompt le `.app` à l'installation (bug fatal corrigé en v0.6.1).

## CI (GitHub Actions)

- **`.github/workflows/ci.yml`** (sur PR vers `main`) : lint ruff (Linux, rapide) + build sanity du `.app` (macOS). Les PR purement docs (`**.md`, `docs/**`) ne déclenchent rien.
- **`.github/workflows/release.yml`** (sur push `main`) : build + publication de la Release décrites ci-dessus.
- **Build manuel local** si besoin : `./scripts/build-macos.sh` (génère `dist/Sort Memories.app` + le zip `ditto`).
- **Lint local** : `.venv/bin/ruff check sort_memories/` doit être vert. Le gros `core.py` (UI embarquée) a des `per-file-ignores` dans `pyproject.toml` (E501/E402/SIM) ; ne pas les retirer sans raison.

## Conventions

- **Ne jamais commiter** `build/`, `dist/`, ni un `.app` (artefacts PyInstaller).
- Documentation projet dans `docs/` (CHANGELOG, ACTIVE, ARCHITECTURE, DECISIONS) : mise à jour à chaque changement significatif.
- Copy lue par l'utilisateur (UI, docs, README) : **pas de tiret cadratin `—`** (signature IA). Utiliser deux-points, virgule, parenthèses.
- `core.py` est volumineux : éditer de façon chirurgicale (Grep + lecture ciblée), ne pas tout relire.
