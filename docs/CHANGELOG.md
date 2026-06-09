# Changelog

## [2026-06-09] v0.8.2 : préserver l'EXIF lors de la rotation et du rognage

**Type** : Bugfix
**Fichiers modifiés** : `sort_memories/core.py` (`_save_image_in_place`, `_do_rotate`, `_do_crop`, nouveau `_exif_for_save`)

**Symptôme** : après une rotation (T) ou un rognage (R), l'année d'une photo redevenait fausse, même après le fix v0.8.1. `_save_image_in_place` réécrivait l'image **sans** réinjecter l'EXIF, donc `DateTimeOriginal` était perdu et `_capture_datetime` retombait sur le `mtime`.

**Correctif** : nouveau helper `_exif_for_save(p, reset_orientation)` qui récupère l'EXIF d'origine et le passe à `save(..., exif=...)`.
- **Rognage** : EXIF réinjecté verbatim (préserve date, GPS, etc.).
- **Rotation** : EXIF réinjecté avec le tag Orientation (274) remis à 1, puisque les pixels sont déjà physiquement tournés (évite une double rotation à l'affichage).
- Formats couverts : JPEG, HEIC/HEIF, WebP, PNG, TIFF. GIF/BMP n'ont pas de conteneur EXIF (sauvés tels quels, comme avant).

**Vérification** : test end-to-end JPEG + WebP + HEIC (capture 2018, mtime forcé 2024) → après rotation puis rognage, l'année reste **2018** et l'orientation est neutralisée à 1. Lint ruff vert, AST OK.

## [2026-06-09] v0.8.1 : fix année affichée (sous-IFD Exif, pas le mtime)

**Type** : Bugfix
**Fichiers modifiés** : `sort_memories/core.py` (`_exif_datetime`)

**Symptôme** : l'année affichée dans l'interface était fausse (souvent l'année de conversion/fichier au lieu de l'année de prise de vue), surtout après une compression WebP/H.265 qui réécrit la date du fichier.

**Cause racine** : `_exif_datetime` lisait `DateTimeOriginal` (tag 36867) et `DateTimeDigitized` (36868) sur l'IFD0. Or ces tags vivent dans le **sous-IFD Exif** (pointeur `0x8769`), pas dans l'IFD0. `exif.get(36867)` renvoyait donc toujours `None`, et le code retombait sur le `mtime` du fichier (modifié par la conversion). L'année de capture réelle n'était jamais lue pour les formats lus par Pillow (JPEG/HEIC/WebP).

**Correctif** : lecture du sous-IFD via `exif.get_ifd(0x8769)`, avec priorité `DateTimeOriginal > DateTimeDigitized > DateTime (306, IFD0)`. Le fallback exifread (RAW/TIFF) et ffprobe `creation_time` (vidéo) restent inchangés.

**Vérification** : test unitaire sur WebP (DateTimeOriginal 2018, mtime forcé 2024 → renvoie 2018) et JPEG (2015 vs mtime 2024 → renvoie 2015). Lint ruff vert.

## [2026-06-08] v0.8.0 : refonte UI complète « Calme & Pro »

**Type** : Refonte (design UI/UX)
**Fichiers modifiés** : `sort_memories/core.py` (bloc `<style>` réécrit, HTML restructuré, JS chrome), `sort_memories/__init__.py`, `pyproject.toml`

**Contexte** : l'interface accumulait des couleurs et des contrôles hétérogènes (violet, orange, boutons mêlés sur une seule barre du bas). Brief : penser en designer UI/UX, direction « calme & pro » type Apple Photos, priorité lisibilité et réassurance, garder/supprimer par boutons + clavier, refonte de tous les écrans. La logique et le backend (license, auto-update, CLIP, conversion, crop/trim) restent intacts.

**Ce qui a été fait** :
- **Design system from scratch** : tokens `:root` (échelle de gris neutre proche Apple dark, couleurs système garder=vert / supprimer=rouge / bleu / ambre / violet, rayons, ombres douces, easing, `prefers-reduced-motion`). Tout le CSS réécrit autour de ces tokens.
- **Nouveau chrome en deux barres** : un `#topbar` translucide (nom du média + position à gauche, filtre segmenté Tout/Photos/Vidéos + galerie + vider + accueil à droite) et une barre d'action basse zonée (Annuler + outils contextuels à gauche, gros boutons Supprimer/Garder à droite avec raccourcis lisibles).
- **Le média est la star** : il flotte au centre avec une ombre douce et des coins arrondis, marges généreuses, chrome qui s'efface.
- **Tous les écrans harmonisés** : accueil (carte centrée, dégradé radial, options en cartes), écran terminé (CTA clairs), mode galerie (cartes nettes, états garder/supprimer, focus clavier visible), overlay de traitement (spinner + barre arrondie), modales, crop/trim, toasts, paywall, doublons.
- **Réassurance** : Annuler toujours visible, libellés explicites, segmented control pour le filtre, hiérarchie typographique nette, micro-interactions sobres (hover, active, lift des cartes).

**Vérification** : inspection navigateur de chaque écran (single photo + vidéo, accueil, terminé, galerie avec marquage, overlay de progression, modale de confirmation, overlay de rognage) sans erreur console. Lint ruff : zéro nouvelle erreur en code Python. Backend et contrats JS (125 IDs) préservés.

## [2026-06-08] v0.7.0 : anti-blocage, progression trim, mode galerie, tri photos/vidéos

**Type** : Feature (4 chantiers)
**Fichiers modifiés** : `sort_memories/core.py`, `sort_memories/__init__.py`, `pyproject.toml`

**Contexte** : quatre frictions remontées à l'usage. (1) En rouvrant l'app après un tri fini, on tombait sur l'écran « Tri terminé » sans aucune action possible (cul-de-sac). (2) Le découpage vidéo (ffmpeg, parfois plusieurs minutes) ne donnait aucun retour : l'utilisateur faisait « Retour » en croyant à un plantage. (3) Une seule méthode de tri (un par un). (4) Mêmes contrôles pour photos et vidéos, interface indifférenciée.

**Ce qui a été fait** :
- **Anti-blocage + re-scan auto** : `/api/state` cherche les nouveaux médias des dossiers avant de déclarer le tri fini (`_append_new_files`) et reprend automatiquement s'il y en a. L'écran « terminé » expose désormais des CTA : rechercher de nouveaux médias (`/api/refresh_queue`), vider la corbeille, galerie, changer de dossiers.
- **Progression réelle du trim** : le découpage passe par un worker asynchrone (`/api/trim/start` + `/api/trim/status`) qui parse la progression ffmpeg (`-progress`). Un overlay plein écran affiche un pourcentage réel, le temps écoulé, un message « ne quittez pas / ne faites pas Retour », et bloque la navigation (clavier + bouton Retour) pendant l'encodage.
- **Mode galerie de tri** (photos ET vidéos) : alternative au un par un. Grille avec navigation clavier (flèches), marquage garder (Espace/K) / supprimer (D), annulation (U), validation par lot (Entrée), retour (Échap). Backend : `/api/gallery?scope=queue&type=...` + `/api/gallery_action`. L'undo standard reste fonctionnel.
- **Filtre par type + contrôles contextuels** : sélecteur Tout / Photos / Vidéos (`/api/triage_filter`, `/api/queue_stats`) qui filtre la file ; la barre n'affiche que les actions pertinentes (rogner pour une photo, lecteur + découper pour une vidéo).

**Vérification** : suite end-to-end via `app.test_client` (config, filtre, galerie batch, trim async avec progression 0 puis 100, re-scan auto au done) plus inspection visuelle du frontend (mode single photo et vidéo, mode galerie avec marquage, écran terminé avec CTA, overlay de progression) sans erreur console. Lint ruff : zéro nouvelle erreur en code Python.

**Limite connue** : le mode galerie ne surface pas les groupes de doublons pHash/CLIP (c'est une méthode de tri à plat, volontairement). La détection de doublons reste disponible en mode un par un.

## [2026-06-04] v0.6.1 — hotfix auto-update (bundle corrompu)

**Type** : Bugfix (critique)
**Fichiers modifiés** : `sort_memories/updater.py`

**Symptôme** : après une mise à jour via l'interface, l'app ne se lançait plus (« Impossible d'ouvrir l'application »).

**Cause racine** : l'auto-updater décompressait le zip avec `zipfile.ZipFile.extractall`, qui **aplatit les symlinks en fichiers** et **perd le bit exécutable**. Le `.app` macOS contient 73 symlinks (versions de dylibs libheif/libde265/libraw, framework Python) + le binaire `Contents/MacOS/SortMemories` qui doit être exécutable → bundle corrompu à l'installation. Bug présent depuis la v0.5.0 (updater inchangé), devenu fatal en v0.6.0 avec l'ajout des libs natives RAW/HEIC.

**Correctif** :
- Extraction via `ditto -x -k` (préserve symlinks + permissions + resource forks, comme `ditto -c -k` à la création du zip).
- Garde-fou : vérification de présence + `chmod +x` du binaire principal après extraction, et `chmod +x` défensif dans le script relauncher après le `ditto` de copie.

**Vérification** : round-trip prouvé — `zipfile.extractall` → 0 symlink / non-exécutable (reproduit le bug) ; `ditto -x -k` → symlinks + exécutable préservés.

**⚠️ Limite des auto-updaters** : c'est la version **installée** qui exécute l'extraction. Une app en v0.5.0/v0.6.0 corrompra toujours sa cible en auto-update. Il faut installer la v0.6.1 **manuellement une fois** ; à partir de la v0.6.1, l'auto-update fonctionne.

## [2026-06-04] v0.6.0 — tous formats + métadonnées préservées + tri par volume

> S'appuie sur la v0.5.0 (HEIC iPhone + formats vidéo + auto-update). Cette version ajoute RAW/AVIF, la préservation des métadonnées, le fix de l'année, le preview et le tri par volume.

**Type** : Feature
**Story** : Compresser n'importe quel format sans jamais perdre la date de prise de vue ; trier par volume
**Fichiers modifiés** : `sort_memories/core.py`, `pyproject.toml`, `build/SortMemories.spec`, `docs/`, `README.md`

**Problème résolu (déclencheur)** : une vidéo de bébé compressée s'affichait en **2026**. Cause : la compression v0.4.0 détruisait les métadonnées (images sauvées sans EXIF, ffmpeg sans `-map_metadata`) et réécrivait le `mtime` à la date de compression ; l'affichage de l'année tombait alors sur ce mtime. ⚠️ Les fichiers déjà compressés en v0.4.0 ont probablement perdu leur date de façon irrécupérable (sauf `YYYY` dans le nom) — cette version empêche toute perte future.

**Tous les formats (toute l'app : triage + dédup + compression)** :
- Photos : ajout HEIC/HEIF (iPhone), AVIF, TIFF, BMP, et **RAW** (DNG/CR2/CR3/NEF/ARW/RAF/ORF/RW2/SRW/PEF) via `pillow-heif` + `rawpy`.
- Vidéos : ajout M4V, AVI, MKV, WMV, FLV, 3GP, MPG/MPEG, WEBM, TS, MTS, M2TS (décodés par ffmpeg, sortie H.265 MP4).
- Endpoint `GET /preview/<entry>` : JPEG d'aperçu à la volée (cache disque) pour les formats que WKWebView ne sait pas rendre (RAW, vidéos non-MP4). Le front bascule sur `preview_url` quand nécessaire.

**Préservation des métadonnées à la compression** :
- Images → WebP : transfert `exif` + `icc_profile` + `xmp`. RAW → WebP : EXIF reconstruit (DateTimeOriginal) via exifread.
- Vidéos → H.265 : `-map_metadata 0` + `-movflags use_metadata_tags` (copie `creation_time` + tags conteneur).
- **Filet de sécurité** : `os.utime` recopie le `mtime` d'origine sur le fichier compressé → l'année reste juste même si une métadonnée interne manquait.

**Année fondée sur les vraies métadonnées** :
- Nouveau `_capture_datetime` : EXIF DateTimeOriginal/Digitized/DateTime (image) → `creation_time` ffprobe (vidéo) → `AAAA[-_]MM` du nom → mtime → "—". Mémoïsé par (chemin, mtime).
- `_year_label` réécrit par-dessus. `compute_keep_destination` range désormais par vraie date de capture (dossiers `Gardées/AAAA/MM/` + rename `AAAA-MM-JJ`).

**Tri par volume** :
- Option session `order: "largest"` (case « Traiter d'abord les fichiers les plus volumineux » dans l'accueil) → la file de triage présente les plus lourds en premier.
- Nouvelle galerie (`GET /api/gallery?sort=size_desc|size_asc|date_desc|date_asc|name`) + bouton « 📊 Galerie » : grille de vignettes triable (lecture seule).

**Vérification** : 20/20 tests sur fixtures (JPEG EXIF 2015, HEIC EXIF 2018, MOV creation_time 2019, AVI, fichier nommé 2017), tous avec mtime forcé à 2026 → année correcte affichée ; compression JPEG→WebP (424→116 Ko) et MOV→H.265 (488→56 Ko) conservent EXIF/creation_time + mtime ; preview AVI OK ; tri galerie + file OK.

**Limites assumées** : RAW→WebP est lossy/irréversible (choix produit) ; les fichiers déjà compressés en v0.4.0 ne sont pas récupérables côté fichier.

## [2026-05-25] v0.5.0 — support HEIC iPhone + formats vidéo étendus + auto-update

**Type** : Feature
**Story** : Débloquer la cible iPhone + supprimer la friction "réinstaller à chaque release"
**Fichiers modifiés** : `sort_memories/core.py`, `sort_memories/updater.py` (nouveau), `pyproject.toml`, `build/SortMemories.spec`, `scripts/build-macos.sh`, `README.md`

### Auto-update (nouveau)
- **Module `sort_memories/updater.py`** : check GitHub Releases API, download zip, génère un script bash relauncher qui swap le `.app` et relance
- **3 endpoints Flask** : `GET /api/update/status`, `POST /api/update/check`, `POST /api/update/install`
- **UI welcome view** : nouvelle section "Mises à jour" avec version actuelle + bouton "Vérifier maintenant" + banner violet si une release est dispo
- **Check silencieux au démarrage** (1s après load, non bloquant)
- **Modal progression** avec barre + bytes téléchargés
- **Sécurité** : HTTPS strict (ssl.create_default_context), User-Agent identifié, vérif que le zip contient bien Sort Memories.app à la racine, swap atomique via `ditto`, ancien .app envoyé à la corbeille (réversible)
- **Fallback** : si `trash` n'est pas dispo chez l'user final, `mv` vers `~/.Trash` manuellement

### Support HEIC + formats étendus
- **`IMAGE_EXT`** : ajout `.heic`, `.heif`, `.tiff`, `.tif`, `.bmp` (avant : jpg/jpeg/png/gif/webp uniquement)
- **`VIDEO_EXT`** : ajout `.m4v`, `.webm`, `.mkv`, `.avi` (avant : mp4/mov uniquement)
- **`MEDIA_EXT`** : dérivé `IMAGE_EXT | VIDEO_EXT` (DRY, fin des sets hardcodés à 3 endroits)
- **`pillow-heif>=0.16`** ajouté en dépendance + `register_heif_opener()` au top du module
- **`_save_image_in_place()`** : helper unique pour sauvegarde post-rotate/crop, gère JPEG, HEIC, formats natifs Pillow
- **PyInstaller spec** : `pillow_heif` ajouté à hiddenimports + binaires dynamiques (libheif/libde265 via cffi)

**Tests** :
- HEIC end-to-end : génération → rotation 90° → save HEIF → re-open : dimensions swappées OK ✓
- Auto-update endpoints (Flask test client) : status / check / install bloqué si pas d'update dispo ✓
- Bundle .app rebuild v0.5.0 + smoke test launch ✓

**Impact utilisateur** :
- Les dossiers iPhone (HEIC) sont enfin scannables → cible primaire débloquée
- Les vidéos WebM (Discord, navigateur), MKV (Plex), M4V (iTunes) ne sont plus silencieusement ignorées
- **Plus besoin de réinstaller manuellement** : un clic depuis la page d'accueil suffit
- Aucun changement breaking sur les datasets existants (cache pHash + state préservés)

**Alternatives considérées** :
- Convertir HEIC → JPG silencieusement : rejeté (change l'extension, perd la fidélité). Préféré : keep HEIC en HEIC via pillow-heif read/write.
- Sparkle framework pour l'auto-update : rejeté (overhead Objective-C/Swift pour app Python). Préféré : updater Python custom + script bash relauncher.
- Inclure RAW formats (`.cr2`, `.nef`, `.arw`) : reporté v0.6+ (workflow photographe pro, hors cible). → **Fait en v0.6.0.**

## [2026-05-24] v0.4.0 — compression pré-triage (WebP + H.265)

**Type** : Feature
**Story** : Gagner de l'espace disque + tri plus fluide
**Fichiers modifiés** : `sort_memories/core.py` (endpoints + UI welcome), `pyproject.toml`, `build/SortMemories.spec`
**Description** :
- **Section "Compression (optionnel)" dans la welcome view** : 4 radios (Aucune / Sans perte / Équilibré / Compact)
- **3 presets opinionated** :
  - Sans perte : WebP q90 + H.265 CRF 22 (gain ~30-50%, imperceptible)
  - Équilibré : WebP q82 + H.265 CRF 25 (gain ~50-70%, légère perte)
  - Compact : WebP q72 + H.265 CRF 28 (gain ~70-90%, acceptable)
- **Worker thread daemon** avec status polling + cancel
- **Skip intelligent** : fichiers déjà au format cible (WebP, H.265), fichiers < 50 KB, fichiers sans gain ≥ 5%
- **Validation state auto** dans `_init_mem` : après conversion, les entries pointant sur fichiers absents sont droppées et les nouveaux fichiers (extensions changées .jpg → .webp) sont ajoutés. Aucune perte d'état entre conversion et reprise du triage.
- **Modal progress** avec barre, fichier en cours, % done, octets économisés temps réel, bouton Annuler
- **Confirmation modale forte** avant lancement (irréversible — remplace les originaux)

**Endpoints nouveaux** :
- `GET  /api/convert/preview` : count + size_human des fichiers convertibles
- `POST /api/convert/start`   : `{preset: "lossless"|"balanced"|"compact"}` → thread daemon
- `GET  /api/convert/status`  : progression temps réel (running, current, total, bytes_saved, errors)
- `POST /api/convert/cancel`  : interrompt le worker (les fichiers déjà convertis restent)

**Tests E2E** :
- 2 fichiers JPG (1.5 MB total) → preset compact → 2 fichiers WebP (440 KB total) = 70% gain (1.1 MB économisés)
- Validation state : après conversion, le triage reprend correctement sur les .webp
- Bundle .app v0.4.0 = 46 MB / 22 MB zip

## [2026-05-24] v0.3.0 — rotation, crop, trim, vidage corbeille

**Type** : Features (édition légère + gestion espace disque)
**Fichiers modifiés** : `sort_memories/core.py` (endpoints + UI), `app.py`, `pyproject.toml`, `build/SortMemories.spec`, `scripts/build-macos.sh`
**Description** :
- **Rotation 90° horaire** (raccourci `T`) — applique in-place sur image (PIL.transpose) et vidéo (ffmpeg `-vf transpose=1`)
- **Crop image** (raccourci `R` sur image) — overlay drag-to-select, conversion coords display→source, PIL.crop in-place
- **Trim vidéo** (raccourci `R` sur vidéo) — modal timeline double-handle (start/end), ffmpeg `-ss/-to` avec re-encode H.264 frame-accurate
- **Vider la corbeille** — bouton `🗑 Vider (N • XX MB)` qui badge le total des fichiers dans `Tri/Supprimées/`, envoi à la Corbeille macOS via `send2trash` (réversible)
- **Reprise de session** : déjà fonctionnel via `SESSION_FILE` + `STATE_FILE` namespacé, conservation transparente entre fermetures de l'app
- **Toast notifications** : feedback non-bloquant pour les actions (rotation appliquée, fichiers supprimés, erreurs)
- **Modal de confirmation générique** : utilisé pour empty_trash, structure réutilisable
- **Raccourci Échap** : ferme tout overlay/modal actif
- **Dépendance** : `send2trash>=1.8` ajoutée à pyproject + bundle PyInstaller (~50 KB supplémentaires)

**Endpoints nouveaux** :
- `POST /api/transform` : `{action: "rotate"|"crop"|"trim", entry, ...params}`
- `GET /api/trash_info` : `{count, size_bytes, size_human, trash_dirs}`
- `POST /api/empty_trash` : envoie à la Corbeille macOS

**Tests E2E** :
- Rotation image (PIL) → fichier modifié, dimensions inversées, toast OK
- Rotation vidéo (ffmpeg transpose) → mp4 re-encodé H.264 CRF18
- Trash de 2 fichiers → badge passe à "Vider (2 • 697.7 KB)"
- empty_trash → "2 fichier(s) envoyé(s) à la Corbeille macOS", badge revient à "Vider"
- Bundle .app v0.3.0 = 46 MB / 21 MB zip, Flask + send2trash OK

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
