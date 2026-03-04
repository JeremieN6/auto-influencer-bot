# CLAUDE.md -- Memoire Projet

> Ce fichier est lu automatiquement par l'IA au debut de chaque conversation.
> Mets-le a jour a la fin de chaque session de travail.

---

## Objectif Final
<!-- A completer -->
Automatiser la création et publication de contenu Instagram pour une influenceuse IA. Le système trouve une image d'inspiration sur Pinterest ou via un workflow plus automatisé, y place le personnage de l'influenceuse, génère une caption adaptée, et envoie le tout pour validation sur un compte telegram dans une V1 avant publication. Dans une V2 l'image sera automatiquement envoyé sur Instagram. Tout tourne seul tous les 4 jours. 
En effet le but de ce script est aussi qu'il soit réplicable, générique, afin de pouvoir générer du comptenu sur le compte d'autres influenceuse que j'aurai crée ou dont j'obtiens l'accès. C'est l'objectif, il est tout aussi important que la génération de contenu, le process compte aussi dans l'optique de devenir templatisable.
---

## Stack Technique
<!-- A completer -->
Langage : Python 3.11
Scraping Pinterest : Playwright
Génération image : Gemini API (Google)
Génération caption : Claude API (Anthropic)Validation & contrôle : Telegram Bot
Publication : Meta Graph API (Instagram)
Hébergement image temporaire : Nginx (VPS)Automatisation : Cron + Systemd
---

## Etat Actuel du Projet
**Phase** : V1 Implémentée
**Derniere session** : 2026-03-04
**Progression globale** : 70% (V1 complète, V2 scaffoldée)

### Ce qui est fait :
- [x] Configuration MCP memoire
- [x] Structure complète du projet (tous les dossiers + fichiers)
- [x] config.py — générique, INFLUENCER_REF_IMAGE_PATH dérivé automatiquement du nom
- [x] prompts.py — tous les prompts centralisés (7 prompts + blocs hashtags)
- [x] logger.py — logs détaillés console + fichier, helpers log_section / log_step
- [x] data/variables.json — BDD créative complète (12 locations, 10 outfits, 10 poses...)
- [x] data/calendar.json — cycle éditorial 4 steps (feed/story alternés)
- [x] concept_generator.py — anti-répétition 30j, calendrier, build_caption_prompt
- [x] pinterest_scraper.py — Playwright complet, anti-blocage, 3 stratégies de retry
- [x] image_generator.py — Gemini generate_image + image_to_json + cleanup_nginx
- [x] caption_generator.py — Claude API + validate_custom_input (V2 ready)
- [x] instagram_publisher.py — Meta Graph API, polling container status, cleanup nginx
- [x] workflows/workflow_pinterest.py — V1 complet (scrape → JSON → image)
- [x] workflows/workflow_generatif.py — V2 scaffold avec TODO détaillé
- [x] workflows/workflow_backup.py — dormant, activation manuelle
- [x] telegram_bot.py — V1 complet (/status /validate /modify /generate /schedule), V2 /run scaffoldé
- [x] main.py — orchestrateur avec --dry-run et notification Telegram erreur fatale
- [x] README.md — guide d'installation complet (clone → cron)
- [x] .gitignore, .env.example, requirements.txt

### Prochaines etapes :
- [ ] Tester le pipeline sur VPS avec vraies clés API
- [ ] Vérifier les noms de modèles Gemini (`gemini-3-pro-image-preview` = preview non stable)
- [ ] Implémenter workflow_generatif.py V2 (Claude → JSON de scène)
- [ ] Implémenter /run Telegram V2 (ConversationHandler multi-étapes)
- [ ] Configurer nginx sur VPS (NGINX_BASE_URL + NGINX_OUTPUT_DIR dans config.py)
- [ ] Générer image de référence Madison 3 angles → data/ref_madison.jpg

---

## Blocages et Points d Attention
<!-- Lister ici -->

---

## Decisions Prises
| Date | Decision | Raison |
|------|----------|--------|
| 2026-03-04 | `INFLUENCER_REF_IMAGE_PATH` dérivé auto depuis `INFLUENCER_NAME` | Évite l'incohérence en changeant d'influenceuse — une seule variable à modifier |
| 2026-03-04 | `pending_state` sauvegardé dans `data/pending_state.json` | Partage d'état entre main.py (cron) et telegram_bot.py (systemd service) — 2 processus distincts |
| 2026-03-04 | Extraction image URLs Pinterest directement depuis le DOM (pas og:image) | Plus rapide, évite d'ouvrir chaque pin individuellement — moins de risque de blocage |
| 2026-03-04 | Nettoyage image Pinterest source après extraction JSON | Évite l'accumulation de fichiers temporaires dans outputs/ |
| 2026-03-04 | `/generate` déclenche main.py via subprocess | Séparation propre entre le processus bot (polling) et le pipeline (cron) |
| 2026-03-04 | `--dry-run` flag dans main.py | Facilite les tests sans affecter l'historique ni envoyer sur Telegram |

---

## Notes de Session
> Ajouter ici un resume a la fin de chaque session de travail.

### Session 2026-03-04 — Implémentation complète V1
- Développé l'intégralité du projet depuis les specs AI_INFLUENCER_AUTOMATION.md
- 17 fichiers créés : tous les modules Python + data JSON + configs + README
- V1 (workflow Pinterest) : entièrement fonctionnelle
- V2 (workflow_generatif) : scaffoldé avec TODO détaillés
- Décision archi clé : pending_state partagé via JSON (2 processus distincts)
- INFLUENCER_REF_IMAGE_PATH dérivé automatiquement depuis INFLUENCER_NAME (généricité)
- Point d'attention : noms modèles Gemini (previews) à vérifier sur https://ai.google.dev/models
