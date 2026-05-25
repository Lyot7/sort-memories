# État actuel du projet

**Dernière mise à jour** : 2026-05-25

## En cours

- [ ] **Rebuild + test manuel v0.5.0** — `./scripts/build-macos.sh` puis test sur dossier iPhone (HEIC + MOV)
- [ ] **Roadmap commerciale macOS** (cf. `~/.claude/plans/en-vrai-l-application-est-drifting-avalanche.md`)

## À faire (v0.5.x — commercialisation macOS)

### Phase B — Stack commercialisation (2-3 semaines)
- [ ] **Apple Developer Program** (99 €) — création compte, validation 24-48h
- [ ] **Developer ID Application + Installer certs** — génération + import keychain
- [ ] **Script signature + notarisation** — `codesign --deep --options runtime` + `xcrun notarytool submit --wait` + `xcrun stapler staple` intégrés à `build-macos.sh`
- [ ] **DMG signé/notarisé** — `create-dmg` ou `dmgbuild` au lieu du zip actuel
- [ ] **Auto-update Sparkle** (ou PyUpdater) — appcast.xml hébergé, EdDSA signing
- [ ] **Trial server-side** — 500 fichiers OU 15 jours via LemonSqueezy License API + petite couche Vercel/Edge
- [ ] **UI activation in-app** — champ clé licence + activation HWID + grace period offline

### Phase C — Landing SEO/GEO (3-4 semaines parallèle B)
- [ ] **Scaffold Next.js 15** + Tailwind + MDX
- [ ] **Pages produit** : landing, features, pricing, vs-gemini, vs-photosweeper, faq, legal
- [ ] **SEO foundations** : robots.ts, sitemap.ts, metadata, OG via next/og, favicons, manifest
- [ ] **GEO setup** : llms.txt, JSON-LD Schema.org (SoftwareApplication + FAQPage + Article)
- [ ] **10 articles SEO seed** (6 EN + 4 FR) sur longue traîne
- [ ] **Waitlist + early-bird 19 €** via LemonSqueezy pre-order
- [ ] **Hébergement Vercel** + domaine + Plausible/Umami analytics

### Phase D — Lancement (mardi 2 juin 2026)
- [ ] Product Hunt "Coming Soon" puis launch jour J
- [ ] Soumissions AlternativeTo, MacUpdate, Softpedia, MacGenStore (1 j one-shot)
- [ ] Email blast waitlist + post Reddit r/macapps (si participation organique préalable)

### Reportés (à reconsidérer mois +6 selon traction)
- [ ] **CLIP réactivé** (download on-demand, pas bundle) — gain de 2 GB sur le DMG actuel
- [ ] **Icône `.icns`** 1024×1024 branding propre
- [ ] **Tests pytest** — couverture `_year_label`, `images_similar`, `build_groups`
- [ ] **Port Windows** + cert EV (400-600 €) — décision selon KPIs Sort Memories mois +6

## Fait récemment

- [x] **v0.5.0 — support HEIC iPhone + formats vidéo étendus** (2026-05-25)
- [x] v0.4.0 — compression pré-triage WebP + H.265 (2026-05-24)
- [x] v0.3.0 — rotation T, crop/trim R, vider corbeille send2trash (2026-05-23)
- [x] v0.2.0 — vue accueil + multi-source + options de tri (2026-05-22)
- [x] v0.1.0 release — bundle .app fonctionnel
- [x] Port `triage.py` → `sort_memories/core.py` avec MEDIA_DIR / STATE_DIR séparés et state namespacé
- [x] Wrapper `app.py` pywebview + folder picker
- [x] Bundle PyInstaller `.app` 46 MB (torch/open_clip exclus)
- [x] Script `scripts/build-macos.sh` reproductible
- [x] Scaffold initial repo public

## Bugs connus

- Pas encore de signature Apple → Gatekeeper bloque la première ouverture (workaround `xattr -dr` ou Réglages Système documenté dans README) — sera résolu Phase B
- pywebview deprecation `FOLDER_DIALOG` (warning seulement, comportement OK — code utilise déjà `FileDialog.FOLDER` quand dispo)

## Dette technique

- Pas de `requirements.txt` figé — venv repose sur `pip install` direct des deps. À générer via `uv pip compile pyproject.toml` quand les versions stabilisées.
- Pas de tests pytest — à ajouter sur les fonctions pures (`_year_label`, `images_similar`, hash compute)
- UI HTML inline dans `core.py` (2000 lignes de `render_template_string`) — pas un problème immédiat mais à séparer en templates Jinja si modifs lourdes UI
- Bundle .app sans CLIP — si v0.6+ réactive CLIP en download on-demand, prévoir cache local STATE_DIR/clip/
