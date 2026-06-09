# Décisions techniques

## [2026-06-08] Trim asynchrone, galerie à plat, filtre transient (v0.7.0)

**Contexte** : ajout de 4 chantiers UX. Trois choix d'architecture méritaient d'être tranchés.

**1. Trim synchrone vs asynchrone**
- Options : (a) garder `/api/transform` synchrone bloquant avec un simple spinner indéterminé ; (b) worker thread async + polling de la progression `ffmpeg -progress`.
- Décision : (b). Le découpage ré-encode (libx264) et peut durer plusieurs minutes ; un spinner indéterminé n'empêche pas l'angoisse du « ça a planté ». Le pattern worker + polling existait déjà pour la conversion (`_convert_status`), donc cohérence interne et coût faible. `out_time_us` est parsé (fiable cross-build) plutôt que `out_time_ms` (microsecondes trompeuses selon les builds ffmpeg).

**2. Mode galerie : à plat vs avec groupes de doublons**
- Options : (a) réutiliser la détection pHash/CLIP dans la galerie ; (b) galerie à plat sur la file restante.
- Décision : (b). La galerie est une *méthode de tri alternative* (vue d'ensemble, marquage par lot au clavier), pas un remplacement de la détection de doublons. Mélanger les deux compliquerait l'UI et le modèle mental. Les groupes restent en mode un par un. `/api/gallery_action` journalise chaque opération en `keep`/`trash` standard, donc l'undo existant fonctionne sans code spécifique.

**3. Filtre par type : persistant vs transient sur le `current`**
- Options : (a) avancer/sauvegarder `current` en sautant les fichiers hors filtre ; (b) calcul transient de l'index d'affichage sans toucher au `current` sauvegardé.
- Décision : (b). Sauvegarder l'avance ferait « disparaître » définitivement les fichiers de l'autre type quand on change de filtre. Le calcul transient garantit qu'un retour à « Tout » réaffiche tout. Le re-scan au done (`_append_new_files`) ne tourne qu'à la frontière fin-de-file pour éviter un scan disque à chaque appel de `/api/state`.

## [2026-06-04] Préserver les métadonnées + dater par EXIF/creation_time

**Contexte** : la compression v0.4.0 effaçait les métadonnées (EXIF/creation_time) et réécrivait le `mtime`. L'affichage de l'année reposait sur `nom → mtime`, d'où une vidéo de bébé affichée en 2026.

**Options évaluées** :
1. Stamper la date dans le nom de fichier — simple mais pollue les noms, fragile, perd l'heure/GPS.
2. Préserver les métadonnées internes (EXIF/QuickTime) + lire la vraie date pour l'affichage — robuste, standard, non destructif.
3. Stocker la date dans une base annexe — sur-ingénierie pour une app locale mono-fichier.

**Décision** : option 2. Transfert EXIF/ICC/XMP au save WebP, `-map_metadata 0` sur ffmpeg, `os.utime` pour recopier le mtime d'origine (filet de sécurité). Nouveau `_capture_datetime` (EXIF → ffprobe → nom → mtime) comme source unique pour l'affichage ET le rangement.

**Conséquences** : dates fiables ; rangement `Gardées/AAAA/MM/` par vraie date ; mémo en mémoire pour éviter les relectures EXIF/ffprobe. Les fichiers déjà compressés en v0.4.0 restent non récupérables.

**Statut** : Active

## [2026-06-04] Couvrir tous les formats sur toute l'app, RAW compris

**Contexte** : seuls jpg/png/gif/webp + mp4/mov étaient pris en charge. Les HEIC iPhone et conteneurs AVI/MKV étaient invisibles.

**Options évaluées** :
1. Élargir la compression seule — incomplet (HEIC toujours invisibles au triage/dédup).
2. Élargir toute l'app (triage + dédup + compression) — cohérent, mais impose le décodage HEIC/RAW et des previews pour WKWebView.
3. Ne pas toucher aux RAW — sûr (RAW→WebP est lossy) mais l'utilisateur a explicitement demandé à les compresser aussi.

**Décision** : option 2 + compression des RAW (choix utilisateur). `pillow-heif` (HEIC/HEIF/AVIF), `rawpy` (RAW), `exifread` (EXIF RAW/TIFF). ffmpeg couvre tous les conteneurs vidéo en décode. Endpoint `/preview` JPEG pour les formats non rendus nativement.

**Conséquences** : 3 nouvelles deps natives à bundler (libheif/libraw) dans le `.app`. RAW→WebP assumé lossy/irréversible. Previews mises en cache disque.

**Statut** : Active

## [2026-06-04] Tri par volume : option de file + galerie

**Contexte** : impossible de cibler d'abord les fichiers les plus lourds pour récupérer de l'espace vite.

**Décision** : (a) option session `order: "largest"` qui réordonne la file de triage par taille décroissante ; (b) galerie triable lecture seule (`/api/gallery`) pour inspecter par taille/date/nom. `api_config_set` rendu type-aware (l'option `order` est une string, pas un booléen).

**Conséquences** : `DEFAULT_OPTIONS` gagne une clé non-booléenne (première du genre) → validation séparée via `_ORDER_VALUES`.

**Statut** : Active
