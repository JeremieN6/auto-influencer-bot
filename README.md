# AI Influencer Automation

Système d'automatisation Instagram pour une influenceuse IA.
Pipeline complet : scraping Pinterest → génération image (Gemini) → caption (Claude) → validation Telegram → publication Instagram.

**Stack** : Python 3.11 · Playwright · Gemini API · Claude API · Telegram Bot · Meta Graph API

---

## Architecture rapide

```
main.py                    ← Orchestrateur (cron)
├── concept_generator.py   ← Tirage aléatoire + anti-répétition
├── workflows/
│   ├── workflow_pinterest.py             ← V1 — Pinterest → JSON → image (actif, défaut)
│   ├── workflow_pinterest_inpainting.py  ← Inpainting — Pinterest → rembg → Gemini inpainting
│   ├── workflow_generatif.py             ← V2 scaffold (non implémenté)
│   └── workflow_backup.py                ← Manuel (dormant)
├── pinterest_scraper.py   ← Playwright
├── image_generator.py     ← Gemini API (workflow JSON)
├── inpainting.py          ← Gemini inpainting natif (workflow inpainting)
├── caption_generator.py   ← Claude API
├── instagram_publisher.py ← Meta Graph API
└── telegram_bot.py        ← Bot Telegram (systemd)
```

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

Ajouter la ligne (pipeline toutes les 4 jours à 9h00) :
```bash
0 9 */4 * * /home/user/influencer-bot/.venv/bin/python /home/user/influencer-bot/main.py >> /home/user/influencer-bot/logs/cron.log 2>&1
```

Vérifier que le cron est actif :
```bash
crontab -l
```

---

## Test rapide

```bash
# Test pipeline JSON (défaut)
python main.py --dry-run

# Pipeline Pinterest → JSON → image (workflow défaut)
python main.py

# Pipeline Pinterest → inpainting direct Gemini
python main.py --workflow pinterest_inpainting

# Démarrer le bot Telegram manuellement (développement)
python telegram_bot.py
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
   PINTEREST_KEYWORDS = ["parisian style", "french elegance", ...]
   ```

4. **Adapter `data/variables.json`** selon la niche (locations, outfits, poses...)

5. **Adapter les hashtags** dans `prompts.py` → `HASHTAG_BLOCK_*`

6. **Adapter `NGINX_BASE_URL`** si nouveau domaine

C'est tout.

---

## Commandes Telegram disponibles (V1)

| Commande | Description |
|----------|-------------|
| `/start` | Message d'accueil |
| `/status` | État du système + prochain post schedulé |
| `/validate` | Publier sur Instagram |
| `/modify [instruction]` | Régénérer l'image avec instruction |
| `/generate` | Déclencher un nouveau concept aléatoire |
| `/schedule` | Calendrier des 4 prochains posts |
| `/run` | *(V2 — scaffold, non implémenté)* |

---

## Structure des fichiers de données

| Fichier | Rôle | Commité |
|---------|------|---------|
| `data/variables.json` | BDD créative (locations, outfits...) | ✅ Oui |
| `data/calendar.json` | Cycle éditorial (format, hashtags) | ✅ Oui |
| `data/history.json` | Historique des posts (anti-répétition) | ❌ Non (runtime) |
| `data/pending_state.json` | État partagé entre main.py et bot | ❌ Non (runtime) |
| `data/ref_*_face.jpg` | Référence visage influenceuse | ❌ Non (propriétaire) |
| `data/ref_*_body.jpg` | Référence corps influenceuse | ❌ Non (propriétaire) |

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
| Workflow Génératif (Claude → scène) | V2 | 🔜 À implémenter |
| Commande /run (paramètres manuels) | V2 | 🔜 À implémenter |
| Publication autonome sans /validate | V2 | 🔜 À implémenter (lorsque le process + rendu est validé) |
| Carrousel 1:1 | V2 | 🔜 À valider manuellement |
| Génération de vidéo avec Kling Motion Control | V3 | 🔜 À implémenter |
| TikTok publisher | V3 | 📋 Prévu |
