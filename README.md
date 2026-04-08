# AI Influencer Automation

Système d'automatisation Instagram pour une influenceuse IA.
Pipeline complet : scraping Pinterest → génération image (Gemini) → caption (Claude) → validation Telegram → publication Instagram/TikTok.

**Stack** : Python 3.11 · Playwright · Gemini API · Claude API · Kling AI · Telegram Bot · Meta Graph API

---

## Architecture rapide

```
main.py                    ← Orchestrateur (cron) — routing auto
├── concept_generator.py   ← Tirage aléatoire + anti-répétition + calendrier éditorial
├── video_batch_manager.py ← Gestion des batches vidéo locaux (temp/ → data/videos/)
├── frame_extractor.py     ← Extraction intelligente de frame (ffmpeg + Gemini Vision)
├── workflows/
│   ├── workflow_video_local.py           ← Vidéos locales → Madison image → Kling Motion Control
│   ├── workflow_video_pinterest.py       ← Vidéo scraping Pinterest → Kling
│   ├── workflow_video_higgsfield.py      ← Génération vidéo via Higgsfield AI
│   ├── workflow_pinterest.py             ← Pinterest → JSON → image Gemini
│   ├── workflow_pinterest_inpainting.py  ← Pinterest → rembg → Gemini inpainting
│   ├── workflow_generatif.py             ← V2 scaffold (non implémenté)
│   └── workflow_backup.py                ← Manuel (dormant)
├── pinterest_scraper.py   ← Playwright
├── image_generator.py     ← Gemini API (génération image + IMAGE_SAFETY resilience)
├── kling_generator.py     ← Kling AI (Motion Control + génération vidéo)
├── inpainting.py          ← Gemini inpainting natif
├── caption_generator.py   ← Claude API
├── instagram_publisher.py ← Meta Graph API
├── tiktok_publisher.py    ← TikTok API
└── telegram_bot.py        ← Bot Telegram (systemd)
```

### Routing automatique (`--workflow auto`, défaut)

`main.py` sélectionne le workflow à l'exécution selon ces priorités :

1. **Vidéos locales disponibles** (`data/videos/*.mp4`) → `video_local` (toujours prioritaire)
2. **Step calendrier = feed** → `pinterest` (image fixe)
3. **Step calendrier = story ou reel** → `video_pinterest`

Le calendrier éditorial tourne sur un cycle de 4 posts :

| Step | Format | Type | Hashtags |
|------|--------|------|----------|
| 1 | 4:5 | feed | ✅ |
| 2 | 9:16 | story | ❌ |
| 3 | 4:5 | reel | ✅ |
| 4 | 9:16 | story | ❌ |

---

## Installation — étapes dans l'ordre

### 1. Cloner le dépôt

```bash
git clone https://github.com/ton-user/influencer-bot.git
cd influencer-bot
```

---

### 2. Installer les dépendances Python

Vérifier Python 3.11+ :
```bash
python3 --version
```

Créer un environnement virtuel (recommandé) :
```bash
python3 -m venv .venv
source .venv/bin/activate      # Linux / macOS
# .venv\Scripts\activate       # Windows
```

Installer les packages :
```bash
pip install --upgrade pip
pip install -r requirements.txt
```

---

### 3. Installer les navigateurs Playwright

```bash
playwright install chromium
playwright install-deps chromium   # Dépendances système (Ubuntu/Debian)
```

Vérifier :
```bash
python -c "from playwright.sync_api import sync_playwright; print('OK')"
```

---

### 4. Configurer les variables d'environnement

Copier le template :
```bash
cp .env.example .env
```

Remplir `.env` avec vos clés :

```ini
# Google Gemini — https://aistudio.google.com/app/apikey
GEMINI_API_KEY=AIza...

# Anthropic Claude — https://console.anthropic.com/account/keys
ANTHROPIC_API_KEY=sk-ant-...

# Telegram — créer un bot via @BotFather sur Telegram
TELEGRAM_BOT_TOKEN=1234567890:AAF...
# Obtenir votre CHAT_ID : envoyer un message à @userinfobot
TELEGRAM_CHAT_ID=123456789

# Meta Graph API — https://developers.facebook.com/
# Compte Instagram Professionnel/Créateur lié à une Page Facebook obligatoire
INSTAGRAM_ACCESS_TOKEN=EAABsbCS...
INSTAGRAM_ACCOUNT_ID=17841400...
```

> **Sécurité** : le fichier `.env` est dans `.gitignore`. Ne jamais le commit.

---

### 5. Ajouter les images de référence de l'influenceuse

Deux fichiers sont nécessaires :

| Fichier | Rôle | Workflow concerné |
|---------|------|-----------------|
| `data/ref_{prenom}_face.jpg` | Référence visage (3 angles) | Tous les workflows |
| `data/ref_{prenom}_body.jpg` | Référence corps (3 panels) | Workflow inpainting |

Pour Madison (configuration par défaut) :
```
data/ref_madison_face.jpg   ← référence visage
data/ref_madison_body.jpg   ← référence corps (générée par scripts/generate_body_ref.py)
```

**Générer la référence visage** — utiliser le prompt dans `prompts.py` → `PROMPT_REF_SHEET`
ou le fichier `PROMPTS/CHARACTER_CONSISTENCY_FACE_REFERENCE_IMAGE.md`.

**Générer la référence corps** — script dédié via Replicate (FLUX.1-pro) :
```bash
# Ajouter REPLICATE_API_KEY dans .env.local
python scripts/generate_body_ref.py
```
Le script génère plusieurs variations dans `data/`. Choisir la meilleure et la renommer en `ref_madison_body.jpg`.

> Si les fichiers sont absents lors du lancement du workflow inpainting,
> le pipeline affichera un message d'erreur explicite et s'arrêtera proprement.

---

### 6. Configurer nginx (VPS — exposition de l'image pour Instagram)

L'API Meta Instagram requiert une URL publique HTTPS pour les images.
Nginx sert le dossier `outputs/` du VPS.

Créer le dossier servi :
```bash
sudo mkdir -p /var/www/influencer-bot/outputs
sudo chown -R www-data:www-data /var/www/influencer-bot/
```

Configuration nginx (`/etc/nginx/sites-available/influencer-bot`) :
```nginx
server {
    listen 443 ssl;
    server_name ton-domaine.com;

    ssl_certificate     /etc/letsencrypt/live/ton-domaine.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/ton-domaine.com/privkey.pem;

    location /outputs/ {
        alias /var/www/influencer-bot/outputs/;
        autoindex off;
        expires 1h;
        add_header Cache-Control "public, max-age=3600";
    }
}
```

Activer et recharger :
```bash
sudo ln -s /etc/nginx/sites-available/influencer-bot /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

Mettre à jour `config.py` avec votre domaine :
```python
NGINX_OUTPUT_DIR = "/var/www/influencer-bot/outputs"
NGINX_BASE_URL   = "https://ton-domaine.com/outputs"
```

> Pour un certificat SSL gratuit : `sudo certbot --nginx -d ton-domaine.com`

---

### 7. Lancer le service Telegram (systemd)

Créer le fichier de service :
```bash
sudo nano /etc/systemd/system/influencer-telegram.service
```

Contenu :
```ini
[Unit]
Description=Influencer Telegram Bot
After=network.target

[Service]
ExecStart=/home/user/influencer-bot/.venv/bin/python /home/user/influencer-bot/telegram_bot.py
WorkingDirectory=/home/user/influencer-bot
Restart=always
RestartSec=10
User=user
EnvironmentFile=/home/user/influencer-bot/.env

[Install]
WantedBy=multi-user.target
```

Activer et démarrer :
```bash
sudo systemctl daemon-reload
sudo systemctl enable influencer-telegram
sudo systemctl start influencer-telegram
sudo systemctl status influencer-telegram
```

Vérifier les logs :
```bash
sudo journalctl -u influencer-telegram -f
```

---

### 8. Activer le cron (pipeline automatique)

Ouvrir le crontab :
```bash
crontab -e
```

Ajouter la ligne (pipeline tous les jours à midi, heure française) :
```bash
0 12 * * * cd /opt/mybots/auto-influencer-bot && ./venv/bin/python main.py >> logs/cron.log 2>&1
```

Par défaut, le garde-fou anti-double-run suit la cadence configurée dans `POSTING_INTERVAL_DAYS`.
Il peut être surchargé avec la variable d'environnement `MIN_DAYS_BETWEEN_RUNS` si besoin.

Vérifier que le cron est actif :
```bash
crontab -l
```

---

## Test rapide

```bash
# Test routing automatique (défaut, sélectionne selon vidéos locales + calendrier)
python main.py --dry-run

# Forcer un workflow spécifique
python main.py --workflow video_local --dry-run
python main.py --workflow video_pinterest --dry-run
python main.py --workflow pinterest --dry-run
python main.py --workflow pinterest_inpainting --dry-run

# Workflows manuels (source imposée, hors cron)
python main.py --workflow manual_image --override-params /tmp/params.json --no-persist
python main.py --workflow manual_video --override-params /tmp/params.json --no-persist

# Reprendre uniquement l'étape Kling (après échec)
python main.py --resume-kling --override-params /tmp/kling_state.json

# Lancement production sans dry-run
python main.py

# Démarrer le bot Telegram manuellement (développement)
python telegram_bot.py
```

### Gestion des batches vidéo

Les vidéos locales sont stockées dans `temp/videos/` avec un préfixe de batch (`v2-`, `v3-`, etc.) avant d'être transférées dans `data/videos/` :

```bash
# Transférer des vidéos depuis votre machine vers le VPS
scp "temp/videos/v2-*.mp4" root@VPS_IP:/opt/mybots/auto-influencer-bot/temp/videos/

# Le batch est automatiquement transféré dans data/videos/ lors du premier run
# ou manuellement :
python -c "from video_batch_manager import auto_refill_if_empty; auto_refill_if_empty()"
```

---

## Changer d'influenceuse

Pour utiliser ce système avec une autre influenceuse :

1. **Générer la référence visage** avec `PROMPT_REF_SHEET` (voir `prompts.py`)
   → Sauvegarder dans `data/ref_{prenom}_face.jpg`

2. **Générer la référence corps** via le script dédié :
   ```bash
   # Adapter le prompt dans scripts/generate_body_ref.py selon le personnage
   python scripts/generate_body_ref.py
   ```
   → Sauvegarder la meilleure variation dans `data/ref_{prenom}_body.jpg`

3. **Modifier `config.py`** — uniquement le bloc INFLUENCER CONFIG :
   ```python
   INFLUENCER_NAME  = "Sofia"
   INFLUENCER_STYLE = "brunette parisienne, chic minimaliste, palette bleu/blanc/gris"
   # INFLUENCER_REF_FACE_PATH et INFLUENCER_REF_BODY_PATH sont dérivés automatiquement
   ```

4. **Adapter `data/variables.json`** selon la niche :
   - `locations`, `outfits`, `poses`, `moods`, `lighting` — valeurs descriptives pour Gemini
   - `pinterest_tags` — map `location → tag court Pinterest` (ex: `"bedroom mirror": "mirror selfie girl"`)
   - `pinterest_person_keywords` — *optionnel* : surcharger `_PERSON_KEYWORDS` dans le code si la niche est différente (fitness, fashion, etc.)

5. **Adapter les hashtags** dans `prompts.py` → `HASHTAG_BLOCK_*`

6. **Adapter `NGINX_BASE_URL`** si nouveau domaine

C'est tout.

---

## Commandes Telegram disponibles

| Commande | Description |
|----------|-------------|
| `/start` | Message d'accueil |
| `/status` | État du système + prochain post schedulé |
| `/validate` | Publier sur Instagram |
| `/modify [instruction]` | Régénérer l'image avec instruction |
| `/run` | Lancement principal : choix du workflow, du mode et des paramètres |
| `/generate` | Alias legacy vers `/run` |
| `/schedule` | Calendrier des 4 prochains posts |
| `/retryKling` | Relancer Kling si la dernière vidéo a échoué |
| `/manualGeneration` | Générer depuis une image ou vidéo source donnée |

### `/status` — 3 états possibles

| État affiché | Signification |
|-------------|---------------|
| ⚪ Aucun contenu en attente | Pipeline inactif |
| ✅ Image / Vidéo en attente de validation | Contenu généré, prêt pour `/validate` |
| ⚠️ Pipeline interrompu — Kling a échoué | Image Madison générée, Kling n'a pas fini → `/retryKling` |

### `/retryKling` — récupération après échec Kling

Si le pipeline vidéo échoue lors de l'étape Kling :
1. L'image de l'influenceuse et la vidéo source sont conservées dans `pending_state.json`
2. `/retryKling` repart **directement depuis l'image déjà générée** — pas de régénération Gemini
3. Seule l'étape Kling est relancée
4. La vidéo finale est envoyée sur Telegram pour validation

Si les fichiers ont disparu (redémarrage VPS), utiliser `/run` ou `/manualGeneration`.

### `/manualGeneration` — génération depuis une source donnée

Permet de déclencher le pipeline depuis n'importe quelle source :

**Mode image :**
- Envoyer une photo en pièce jointe → pipeline image (backup workflow)
- Coller une URL d'épingle Pinterest → scraping HD automatique + pipeline image

**Mode vidéo :**
- Envoyer une vidéo en pièce jointe (.mp4 / .mov) → pipeline complet :
  1. Extraction du meilleur frame (Gemini Vision)
  2. Génération de l'image influenceuse (Gemini)
  3. Transfert de mouvement via Kling Motion Control
  4. Envoi du résultat sur Telegram pour validation

> Les runs `/manualGeneration` n'affectent pas `history.json` ni le cycle de calendrier éditorial.

---

## Structure des fichiers de données

| Fichier | Rôle | Commité |
|---------|------|---------|
| `data/variables.json` | BDD créative (locations, outfits, pinterest_tags, pools vidéo...) | ✅ Oui |
| `data/calendar.json` | Cycle éditorial 4 steps (feed/story/reel) | ✅ Oui |
| `data/history.json` | Historique des posts (anti-répétition) | ❌ Non (runtime) |
| `data/pending_state.json` | État partagé entre main.py et bot | ❌ Non (runtime) |
| `data/videos/*.mp4` | Vidéos locales prêtes à l'emploi | ❌ Non (contenu) |
| `data/ref_*_face.jpg` | Référence visage influenceuse | ❌ Non (propriétaire) |
| `data/ref_*_body.jpg` | Référence corps influenceuse | ❌ Non (propriétaire) |
| `temp/videos/` | Dépôt des batches vidéo entrants (préfixes v2-, v3-...) | ❌ Non (contenu) |

---

## Logs

```bash
# Logs du pipeline (cron + main.py)
tail -f logs/run.log

# Logs du cron spécifiquement
tail -f logs/cron.log

# Logs systemd du bot Telegram
sudo journalctl -u influencer-telegram -f --no-pager
```

---

## Roadmap

| Feature | Version | Statut |
|---------|---------|--------|
| Workflow Pinterest (JSON) | V1 | ✅ Implémenté |
| Workflow inpainting Gemini natif | V1 | ✅ Implémenté |
| Bot Telegram V1 (validate, modify...) | V1 | ✅ Implémenté |
| Script génération référence corps (Replicate FLUX.1-pro) | V1 | ✅ Implémenté |
| Routing automatique (`--workflow auto`) | V2 | ✅ Implémenté (2026-03-18) |
| Calendrier éditorial 4 steps (feed/story/reel) | V2 | ✅ Implémenté (2026-03-18) |
| Workflow vidéo local (Kling Motion Control) | V2 | ✅ Implémenté (2026-03-18) |
| Workflow vidéo Pinterest | V2 | ✅ Implémenté (2026-03-18) |
| Workflow vidéo Higgsfield | V3 | 🔜 Prévu |
| Deux pools keywords vidéo (reel/story) | V3 | ✅ Implémenté (2026-03-23) |
| Gestion batches vidéo (`video_batch_manager.py`) | V3 | ✅ Implémenté (2026-03-23) |
| Résilience IMAGE_SAFETY / IMAGE_OTHER (retry 3 concepts + fallback sanitisé) | V3 | ✅ Implémenté (2026-03-23) |
| Aperçu Telegram 24h avant publication (inline buttons) | V3 | ✅ Implémenté (2026-03-23) |
| TikTok publisher | V1 | ✅ Implémenté |
| Workflow Génératif (Claude → scène) | V2 |✅ Implémenté |
| Commande /run (paramètres manuels, ConversationHandler 7 étapes) | V3 | ✅ Implémenté (2026-03-28) |
| Conversion H.264 automatique avant Kling (fix erreur 400 format invalide) | V3 | ✅ Implémenté (2026-03-24) |
| État intermédiaire avant Kling (recovery si crash) | V3 | ✅ Implémenté (2026-03-24) |
| `/retryKling` — reprise exacte sans régénération | V3 | ✅ Implémenté (2026-03-24) |
| `/manualGeneration` — image/vidéo depuis source donnée | V3 | ✅ Implémenté (2026-03-24) |
| Scraping image depuis URL d'épingle Pinterest individuelle | V3 | ✅ Implémenté (2026-03-24) |
| `/status` aware de l'état intermédiaire Kling | V3 | ✅ Implémenté (2026-03-24) |
| Publication autonome sans `/validate` | V4 | 🔜 À implémenter |
| Carrousel 1:1 | V4 | 🔜 À implémenter |
