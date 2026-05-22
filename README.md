# Sort Memories

> Application desktop pour trier et dédoublonner les dossiers de médias locaux. Reprenez le contrôle de votre stockage.

![Status](https://img.shields.io/badge/status-WIP-orange) ![Platform](https://img.shields.io/badge/platform-macOS-black) ![License](https://img.shields.io/badge/license-source--available-blue)

## Pitch

Vos dossiers `Photos`, `Téléchargements`, `Snapchat`, `iCloud Drive` débordent ? Sort Memories ouvre un dossier local, détecte les doublons (pHash + recherche sémantique CLIP), et vous fait défiler chaque média un par un avec deux raccourcis : **garder** ou **supprimer**. À la fin, votre dossier est rangé par année et les fichiers à virer sont dans un `_a_supprimer/` que vous videz à la corbeille.

Conçu pour traiter des milliers de fichiers en une session, sans uploader quoi que ce soit dans le cloud — **tout reste sur votre machine**.

## Fonctionnalités

- 🖼️ **Triage rapide** : flèches gauche/droite pour parcourir, `D` pour supprimer, `O` pour overlay
- 🔍 **Déduplication pHash** : détecte les copies exactes ou re-encodées (resize, conversion JPG↔PNG, etc.)
- 🧠 **Recherche sémantique CLIP** : regroupe les images visuellement similaires (selfies, paysages, captures d'écran…)
- 🎬 **Support vidéo** : extraction de frames pour détection de doublons MP4/MOV
- 💾 **100% local** : aucun upload, aucune télémétrie, aucune dépendance cloud
- ⏯️ **Reprise de session** : ferme et reprends quand tu veux, l'état est sauvegardé

## Installation

> macOS 13+ requis. Multi-plateforme (Linux/Windows) prévu après commercialisation.

**Téléchargement direct** : [dernière release](https://github.com/Lyot7/sort-memories/releases/latest) → `SortMemories.dmg` → glisser dans `/Applications`.

L'app est signée Apple Developer ID et notarisée — pas de "App non vérifiée" au premier lancement.

## Tarification

- **Essai gratuit** : 1000 fichiers traités, sans limite de temps.
- **Licence à vie** : prix et lien d'achat à venir.

## Quick start

1. Lance Sort Memories
2. Sélectionne le dossier à trier (ex : `~/Pictures/Snapchat`)
3. Attends le scan initial (pHash + CLIP, ~30 s pour 1000 fichiers sur Apple Silicon)
4. Tri au clavier : `→` garder, `←` retour, `D` supprimer, `O` overlay
5. À la fin : vide `_a_supprimer/` dans la corbeille macOS

## Roadmap

- [x] Prototype Flask + UI web (utilisé en interne pour trier le dossier Snapchat de l'auteur)
- [ ] Wrapper pywebview → `.app` macOS natif
- [ ] Signature + notarisation Apple
- [ ] CI/CD GitHub Actions (build .dmg sur tag)
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
