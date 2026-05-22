# État actuel du projet

**Dernière mise à jour** : 2026-05-22

## En cours

- [x] Phase 1 — scaffold du repo public sur `github.com/Lyot7/sort-memories`

## À faire (prochaines priorités)

- [ ] **Phase 2** — Portage `~/Pictures/Snapchat/triage.py` → `sort_memories/core.py`
  - [ ] Remplacer `BASE = Path(__file__).parent` par `appdirs.user_data_dir("SortMemories")`
  - [ ] Ajouter sélecteur de dossier au démarrage (input utilisateur, plus de dossier hardcodé)
  - [ ] Bundler ou prompt-install pour `ffmpeg` / `ffprobe`
  - [ ] Renommer le module / les routes pour cohérence avec le branding `Sort Memories`
- [ ] **Phase 3** — Wrapper pywebview + build `.app`
  - [ ] `app.py` lance Flask en thread + `webview.create_window(...)`
  - [ ] `build/pyinstaller.spec` complet (icône, bundle_id, entitlements)
  - [ ] `scripts/build-macos.sh` : pyinstaller → codesign → create-dmg → notarytool
  - [ ] Icône `.icns` 1024×1024
- [ ] **Phase 4** — CI/CD GitHub Actions
  - [ ] Secrets repo : `APPLE_CERT_P12`, `APPLE_ID`, `TEAM_ID`, `APP_SPECIFIC_PASSWORD`
  - [ ] Workflow `.github/workflows/release.yml` sur tag `v*`
- [ ] **Phase 5** — Landing dans le README
  - [ ] Screenshots / GIF démo
  - [ ] Pricing finalisé
  - [ ] Backend de licence en prod (`triage.eliottbouquerel.fr/api/verify`)

## Fait récemment (7 derniers jours)

- [x] Création repo + structure de dossiers + fichiers de base (cf. CHANGELOG)

## Bugs connus

Aucun (pas encore de code utilisateur).

## Dette technique

- `app.py` n'est qu'un stub — vrai wrapper pywebview en Phase 3
- Pas de `requirements.txt` figé — sera généré via `uv pip compile pyproject.toml` quand les deps stabilisées
- Pas de tests pytest — à ajouter en Phase 2 sur les fonctions pures (hash, similarity scoring)
