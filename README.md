# YouTube Clipper

Application web pour extraire des séquences de vidéos YouTube.

## Fonctionnalités

*   Coller une URL YouTube
*   Choisir un début et une fin
*   Téléchargement immédiat de la séquence découpée (MP4)

## 📈 Comment passer à 100+ utilisateurs ? (Scaling)

Si votre application devient très populaire, un seul serveur ne suffira plus. Voici comment passer à la vitesse supérieure :

### 1. Activer le stockage Cloud (Obligatoire pour le Scaling)
Quand vous avez plusieurs serveurs, ils ne partagent pas leurs fichiers.
Si le Serveur A télécharge une vidéo, le Serveur B ne pourra pas l'envoyer à l'utilisateur.
**Solution :** Stocker les fichiers extraits dans **Google Cloud Storage** (ou AWS S3).

1. Créez un Bucket sur Google Cloud.
2. Téléchargez votre fichier de clé `credentials.json`.
3. Configurez les variables d'environnement sur Render/Cloud Run :
   * `BUCKET_NAME`: `nom-de-votre-bucket`
   * `GOOGLE_APPLICATION_CREDENTIALS`: (Contenu du fichier JSON ou chemin)

### 2. Augmenter le nombre d'instances
Une fois le stockage activé, vous pouvez dire à Render ou Google Cloud Run :
* **"Mets-moi 5 serveurs !"**
* Ou **"Auto-Scaling : de 1 à 10 serveurs selon le trafic"**.

Votre application pourra alors gérer des milliers d'utilisateurs simultanés sans planter.
C'est la magie du Cloud ! ✨

---
### 3. Quelle machine choisir ? (Recommandations)

Le traitement vidéo (surtout 4K) consomme beaucoup de processeur (CPU).
Voici ce qu'il vous faut selon votre nombre d'utilisateurs :

| Utilisateurs simultanés | Type de Serveur | CPU Recommandé | RAM | Coût estimé |
|-------------------------|-----------------|----------------|-----|-------------|
| **1 - 5** (Amis/Test) | VPS "Cloud" (Hetzner/OVH) | 2 vCPU | 4 GB | ~5€ / mois |
| **5 - 20** (Production) | VPS "Pro" | 4 vCPU (Dédié) | 8 GB | ~15-20€ / mois |
| **20 - 100+** (Startup) | Serveur Dédié / Cloud Run | 8+ vCPU ou Auto-scale | 16 GB+ | ~50€+ / mois |

**Mon conseil :** Commencez petit avec un **VPS à 5€ (Hetzner CPX11 ou CX21)**.
C'est 10x moins cher que Google/AWS pour ce genre de travail gourmand.

---
## 📞 Contact & Supporte le stockage sur Google Cloud Storage (optionnel)

## 🚀 Démarrage Rapide (Local avec Docker)

1.  **Construire l'image Docker** :
    ```bash
    docker build -t yt-clipper .
    ```

2.  **Lancer le conteneur** :
    ```bash
    docker run -p 8080:8080 yt-clipper
    ```

3.  **Accéder à l'application** :
    Ouvrez votre navigateur sur [http://localhost:8080](http://localhost:8080)

## ☁️ Déploiement sur Google Cloud Run

### Prérequis
*   Avoir un projet Google Cloud
*   Activer les API Cloud Run et Cloud Build
*   Installer `gcloud` SDK

### Étapes

1.  **Définir votre projet** :
    ```bash
    gcloud config set project VOTRE_PROJET_ID
    ```

2.  **Créer un Bucket GCS (Optionnel pour le stockage)** :
    ```bash
    gsutil mb -l EU gs://VOTRE_BUCKET_NAME
    # Rendre le bucket public en lecture (ATTENTION à la sécurité)
    gsutil iam ch allUsers:objectViewer gs://VOTRE_BUCKET_NAME
    ```

3.  **Déployer sur Cloud Run** :
    
    Remplacez `VOTRE_BUCKET_NAME` par le nom de votre bucket (si utilisé).
    
    ```bash
    gcloud run deploy yt-clipper \
      --source . \
      --platform managed \
      --region europe-west1 \
      --allow-unauthenticated \
      --set-env-vars BUCKET_NAME=VOTRE_BUCKET_NAME
    ```

    *Note : Si vous utilisez GCS, assurez-vous que le compte de service de Cloud Run a les droits d'écriture sur le bucket (`Storage Object Admin`).*

### Configuration GCS (Google Cloud Storage)

Pour que l'upload fonctionne :
1.  Le service Cloud Run utilise par défaut le compte de service Compute Engine.
2.  Allez dans IAM et donnez le rôle **Administrateur des objets de stockage** (Storage Object Admin) à ce compte de service pour votre bucket.

## 🛠️ Développement Local (Sans Docker)

1.  **Installer ffmpeg** :
    *   Windows : via `choco install ffmpeg` ou télécharger sur le site officiel.
    *   Mac : `brew install ffmpeg`
    *   Linux : `apt install ffmpeg`

2.  **Installer les dépendances Python** :
    ```bash
    pip install -r requirements.txt
    ```

3.  **Lancer le serveur** :
    ```bash
    uvicorn main:app --reload
    ```

## 🌍 Publier pour toujours (Accessible 24h/24)
**Étapes à suivre :**

1.  **Mettre votre code sur GitHub** :
    *   Créez un compte sur [GitHub.com](https://github.com).
    *   Créez un "New Repository" (nommez-le `yt-clipper`).
    *   Sur votre PC, initialisez git et envoyez le code :
        ```bash
        git init
        git add .
        git commit -m "Premier déploiement"
        git branch -M main
        git remote add origin https://github.com/VOTRE_USER/yt-clipper.git
        git push -u origin main
        ```

2.  **Déployer sur Render** :
    *   Créez un compte sur [Render.com](https://render.com) (Log in with GitHub).
    *   Cliquez sur **"New +"** -> **"Blueprint"**.
    *   Connectez votre nouveau dépôt GitHub `yt-clipper`.
    *   Cliquez sur **"Apply"**.

Render va lire le fichier `render.yaml` que j'ai créé et tout installer automatiquement. Dans 2-3 minutes, votre site sera en ligne avec une URL `https://...` !

