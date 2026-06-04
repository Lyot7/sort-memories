# Décisions techniques

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
