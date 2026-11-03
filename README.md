**EasyCap - La réalisation facile de bandes rythmo sur Cappella.**
![Logo / Bannière](https://media.discordapp.net/attachments/1567214759412105388/1567286947293040681/rectangle_reduit.png?ex=6aeba137&is=6aea4fb7&hm=0ca9ae39520db8882fbb7430168446c109a09f99b0557cf8c8a702ddb0c05a25&=&format=webp&quality=lossless)

> *Ce programme a été créé avec l'I.A, les ressources du programme tournent en local, pas de cloud.*

---

## 📖 Présentation

**EasyCap** est une solution tout-en-un pour créer automatiquement un banc de travail sur la réalisation d'une bande rythmo. Fini les prises de tête sur la compatibilité de Cappella et les lags de lecture à cause d'un codec trop récent 😉 

## ✨ Fonctionnalités

- 🎞️ **Découpage** : Réalisation d'une vidéo complète ou d'une scène spécifique.
- 📼 **Encodage** : Encodage vidéo dans la meilleure qualité possible pour Cappella (QuickTime).
- 🗣️ **Gestion Audio** :
  - Sélection de la piste/langue audio comme modèle pour Cappella.
  - Sélection de la piste audio pour la suppression des voix :
    - *Suppression du canal center d'une source audio 5.1-7.1*
    - *Filtre de suppression des voix d'une source audio 2.0*
- 📝 **Sous-titres** : Conversion des sous-titres (sauf VobSub/PGS) au format Cappella (`.dext`).
- 🎬 **Vidéos de référence** : Préparation d'une vidéo en VO / VOSTFR (sous-titres incrustés) pour la direction artistique et les comédiens.

Pour continuer à faire vivre les comédiens de doublage : https://www.change.org/p/pour-un-doublage-créé-par-des-humains-pour-des-humains-touchepasmavf-beab56eb-a759-4117-b77c-7d03bcaa35f8

---

## ⚙️ Installation

Exécutez les commandes suivantes dans votre terminal pour préparer l'environnement système (Python 3.12, FFmpeg...) et installer les dépendances :

```powershell
# Déploiement des prérequis système
winget install --id Python.Python.3.12 --exact --accept-package-agreements --accept-source-agreements
winget install --id Gyan.FFmpeg --exact --accept-package-agreements --accept-source-agreements

# Mise à jour de pip et installation des modules
py -3.12 -m pip install --upgrade pip
py -3.12 -m pip install -r requirements.txt
