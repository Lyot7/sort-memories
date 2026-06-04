# Sort Memories

> Application desktop pour trier et dédoublonner les dossiers de médias locaux. Reprenez le contrôle de votre stockage.

![Status](https://img.shields.io/badge/status-alpha-yellow) ![Platform](https://img.shields.io/badge/platform-macOS-black) ![License](https://img.shields.io/badge/license-source--available-blue) [![Download](https://img.shields.io/github/v/release/Lyot7/sort-memories?label=download&color=brightgreen)](https://github.com/Lyot7/sort-memories/releases/latest)

## Pitch

Vos dossiers `Photos`, `Téléchargements`, `Snapchat`, `iCloud Drive` débordent ? Sort Memories ouvre un dossier local, détecte les doublons (pHash + recherche sémantique CLIP), et vous fait défiler chaque média un par un avec deux raccourcis : **garder** ou **supprimer**. À la fin, votre dossier est rangé par année et les fichiers à virer sont dans un `_a_supprimer/` que vous videz à la corbeille.

Conçu pour traiter des milliers de fichiers en une session, sans uploader quoi que ce soit dans le cloud — **tout reste sur votre machine**.

## Fonctionnalités

- 📁 **Écran d'accueil** : ajoute N dossiers source, choisis tes options, démarre — pas de Finder intrusif au lancement
- 🗂️ **Options de tri combinables** : grouper par année / aussi par mois / séparer images-vidéos / renommer en `YYYY-MM-DD_<hash>.ext`
- 🖼️ **Triage rapide** : flèches gauche/droite pour parcourir, `D` pour supprimer, `O` pour overlay
- 🔄 **Rotation 90°** : raccourci `T` — applique in-place sur image et vidéo
- ✂ **Crop image / Trim vidéo** : raccourci `R` — drag-to-select sur image, timeline double-handle sur vidéo
- 🗑 **Vider la corbeille** : bouton qui envoie tous les `Tri/Supprimées/` à la Corbeille macOS (réversible)
- ↶ **Undo** : `←` revient en arrière sans perdre l'état (keep et delete réversibles)
- 🔍 **Déduplication pHash** : détecte les copies exactes ou re-encodées (resize, conversion JPG↔PNG, etc.)
- 🖼 **Tous les formats** : photos JPG/PNG/GIF/WebP/**HEIC**/HEIF/AVIF/TIFF/BMP/**RAW** (DNG/CR2/NEF/ARW…) et vidéos MP4/MOV/M4V/AVI/MKV/WMV/3GP/MTS/WEBM…
- 📅 **Date fiable (métadonnées)** : l'année affichée vient de l'EXIF (`DateTimeOriginal`) ou du `creation_time` vidéo, jamais d'une simple date de fichier. La compression **préserve** EXIF/GPS/date.
- 📊 **Tri par volume** : traite d'abord les fichiers les plus lourds, ou parcours une galerie triable par taille / date / nom.
- 💾 **100% local** : aucun upload, aucune télémétrie, aucune dépendance cloud
- ⏯️ **Reprise de session** : ferme et reprends quand tu veux, l'état est sauvegardé par config (sources × options)
- 📦 **Compression pré-triage** : toutes les photos → WebP, toutes les vidéos → H.265, **métadonnées conservées**. 3 presets (Sans perte / Équilibré / Compact). Gain disque ~30-90% selon preset.
- 🔜 **Recherche sémantique CLIP** : prévue pour v0.5.0 (gain de 2 GB sur le bundle actuel — désactivée pour rester téléchargeable rapidement)

## Installation

> macOS 12+ requis (Apple Silicon ou Intel). Multi-plateforme (Linux/Windows) prévu après commercialisation.

**Téléchargement direct** : [dernière release](https://github.com/Lyot7/sort-memories/releases/latest) → `SortMemories-macos-vX.Y.Z.zip` → décompresse → glisse `Sort Memories.app` dans `/Applications`.

### ⚠️ Première ouverture sur macOS (v0.1.x — non encore notarisée)

La v0.1.x n'est pas signée Apple Developer ID. Au premier lancement, macOS Gatekeeper affiche : *"« Sort Memories » ne peut pas être ouvert car Apple n'a pas pu vérifier qu'il ne contenait pas de logiciel malveillant"* avec uniquement *Placer dans la corbeille* / *Terminé*.

**Méthode rapide (Terminal, 1 commande)** :

```bash
xattr -dr com.apple.quarantine "/Applications/Sort Memories.app"
```

Cette commande retire le flag de quarantaine que macOS pose sur tout fichier téléchargé. L'app s'ouvre ensuite normalement, sans aucun warning supplémentaire.

**Méthode GUI (sans Terminal)** :

1. Ferme la popup (clic *Terminé*, **pas** *Placer dans la corbeille*)
2. Ouvre `Réglages Système` → `Confidentialité et sécurité`
3. Descends en bas, tu verras *« Sort Memories » a été bloquée…* → clic **Ouvrir quand même**
4. Entre ton mot de passe macOS pour confirmer

Sur macOS Sonoma+/Sequoia, le *clic-droit > Ouvrir* ne suffit plus pour les apps téléchargées via navigateur — passer obligatoirement par l'une des deux méthodes ci-dessus.

Signature Apple Developer ID + notarisation arrivent en v0.2.0 — ces étapes disparaîtront définitivement.

## Tarification

- **Essai gratuit** : 1000 fichiers traités, sans limite de temps.
- **Licence à vie** : prix et lien d'achat à venir.

## Quick start

1. Lance **Sort Memories** (cf. note Gatekeeper ci-dessus au premier lancement)
2. Sélectionne le dossier à trier (ex : `~/Pictures/Snapchat`, un dossier de captures, etc.)
3. Lance le scan de doublons via le bouton ↻ dans l'UI (pHash, ~5 ms/image)
4. Tri au clavier : `→` garder, `←` retour, `D` supprimer, `O` overlay
5. Les fichiers gardés sont rangés dans `<dossier>/Gardés/<année>/`
6. Les fichiers à virer atterrissent dans `<dossier>/_a_supprimer/` — videz-le dans la corbeille macOS quand tout est trié

**Note v0.1.0** : la recherche sémantique CLIP n'est PAS bundlée (gain de 2 GB sur le téléchargement). Elle arrivera en v0.2.0 — soit en bundle complet, soit téléchargée à la demande. La détection de doublons pHash, elle, est entièrement fonctionnelle.

## Roadmap

- [x] Prototype Flask + UI web (utilisé en interne pour trier le dossier Snapchat de l'auteur)
- [x] Wrapper pywebview → `.app` macOS natif (v0.1.0)
- [x] Build PyInstaller + release GitHub (v0.1.0)
- [ ] Recherche sémantique CLIP bundlée ou downloadable on-demand (v0.2.0)
- [ ] Signature + notarisation Apple (Developer ID) (v0.2.0)
- [ ] CI/CD GitHub Actions (build .dmg sur tag, runner macos-14)
- [ ] Backend de licence (vérification clé en ligne, grace period 30j)
- [ ] Landing page + screenshots/GIF démo
- [ ] Port Linux / Windows (post-commercialisation)

## Stack technique

- **Backend** : Python 3.11+, Flask (UI sur `127.0.0.1:7777`)
- **Détection doublons** : Pillow + imagehash (pHash 64-bit, seuil Hamming ≤ 10)
- **Sémantique** : open_clip_torch (ViT-L/14)
- **Vidéo** : ffmpeg / ffprobe (bundlés dans l'`.app`)
- **Desktop shell** : pywebview (WKWebView macOS natif, pas de Chromium)
- **Packaging** : PyInstaller → `.app` → codesign + notarytool

## Licence

Source-available, tous droits réservés. Voir [LICENSE](./LICENSE). Le code est public pour transparence — il n'est ni redistribuable, ni modifiable, ni utilisable à des fins commerciales sans autorisation écrite.

## Auteur

[Eliott Bouquerel](https://github.com/Lyot7) — freelance dev, France.
