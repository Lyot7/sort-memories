# État actuel du projet

**Dernière mise à jour** : 2026-05-23

## En cours

Rien — v0.1.0 livrée et téléchargeable.

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
