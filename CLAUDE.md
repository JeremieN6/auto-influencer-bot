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
**Phase** : V1+ Content Planner (conscience éditoriale)
**Derniere session** : 2026-04-16
**Progression globale** : 80% (V1 complète + scheduler multi-fréquence + content planner, V2 scaffoldée)

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
- [x] telegram_bot.py — commandes Telegram étendues, avec /run comme entrée principale et /generate en alias legacy
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
| 2026-03-04 | Les commandes Telegram manuelles déclenchent `main.py` via subprocess | Séparation propre entre le processus bot (polling) et le pipeline (cron) |
| 2026-03-04 | `--dry-run` flag dans main.py | Facilite les tests sans affecter l'historique ni envoyer sur Telegram |
| 2026-04-08 | Système de queue pour posts multiples au lieu de `pending_state` unique | Permet de recevoir un nouveau post chaque jour sans bloquer si le précédent n'est pas validé — validation indépendante de chaque post via boutons inline |
| 2026-04-16 | Scheduler multi-fréquence au lieu de cycle séquentiel 4 steps | Permet des fréquences indépendantes par type (story 3/2j, reel 1/4j, feed 1/3j) — compte plus vivant |
| 2026-04-16 | Suppression priorité absolue `video_local` dans `_select_workflow()` | Le calendrier dicte maintenant le workflow — `video_local` n'écrase plus les autres types |
| 2026-04-16 | Reels : 50/50 aléatoire `video_local`/`video_pinterest` | Plus de variété, `video_local` fallback `video_pinterest` si pool vide |
| 2026-04-16 | `content_type` enregistré dans chaque entrée `history.json` | Permet au scheduler de compter les contenus par type dans la fenêtre temporelle |
| 2026-04-16 | Content planner (Claude) + fallback `pool_mix` | L'influenceuse "réfléchit" à ses posts via Claude — si l'API échoue, `pool_mix` du calendrier prend le relais |
| 2026-04-16 | Profil enrichi avec `tone` et `audience` (démographiques réels) | Le planner utilise les vraies données d'audience Instagram pour décider du contenu |
| 2026-04-16 | `pool_type` enregistré dans history.json | Permet au planner de distinguer stories faceless vs character dans ses stats |

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

### Session 2026-04-08 — Système de queue pour posts multiples
- Implémenté file d'attente `data/pending_queue/` au lieu de `pending_state` unique
- Chaque post reçoit un ID unique (timestamp) et ses propres boutons de validation
- Envoi quotidien possible même si posts précédents non validés
- Modifications majeures dans `telegram_bot.py` :
  - Nouvelles fonctions : `_save_to_queue()`, `_load_from_queue()`, `_delete_from_queue()`, `_list_queue()`
  - `send_for_validation()` et `send_video_for_validation()` créent des fichiers dans la queue avec boutons inline (queue_id)
  - Nouveaux callback handlers : `handle_validate_image()`, `handle_publish_video()`, `handle_delete_from_queue()`
  - `/status` affiche tous les posts en attente (3 premiers + compteur)
  - Retrait de tous les blocages `_has_pending_content()` dans `/run`, alias `/generate`, et `/manualGeneration`
- Message garde-fou "0.0 jour(s)" : comportement **normal** — cron déclenché après un run récent, correctement bloqué par `MIN_DAYS_BETWEEN_RUNS`

### Session 2026-04-16 — Scheduler multi-fréquence
- Remplacé le cycle séquentiel 4 steps par un scheduler multi-fréquence basé sur des intervalles par type
- Nouveau format `calendar.json` : `content_types` avec `interval_days`, `batch_size`, `workflow` par type
- `concept_generator.py` : ajout de `get_due_content_types()` — analyse history.json par type et fenêtre temporelle
- `main.py` : boucle scheduler dans `__main__` — itère sur chaque type dû et produit `batch_size - count` contenus
- `_select_workflow()` réécrit : le calendrier dicte le workflow, plus de priorité absolue `video_local`
- Reels : 50/50 aléatoire entre `video_local` et `video_pinterest`, fallback si pool local vide
- Stories : `video_pinterest` pool story (avec ou sans personnage possible)
- `generate_concept()` accepte `content_type` — enregistré dans `history.json` pour le scheduler
- `/status` et `/schedule` Telegram mis à jour pour afficher le statut multi-fréquence
- Ancien guard `MIN_DAYS_BETWEEN_RUNS` remplacé par le scheduler (chaque type a son propre intervalle)
- Prochaine étape : configurer cron toutes les 12h (au lieu de 1x/jour) pour capter les types dus

### Session 2026-04-16b — Content Planner (conscience éditoriale)
- Nouveau module `content_planner.py` : Claude planifie les publications comme le ferait l'influenceuse
- Prompt `PROMPT_CONTENT_PLANNER` dans `prompts.py` : contexte complet (profil, historique, stats, audience, variables créatives)
- 4 types de contenu planner : `story_faceless`, `story_character`, `reel`, `feed`
- Fallback `pool_mix` dans `calendar.json` : si Claude échoue, répartition prédéfinie (ex: 2 faceless + 1 character pour stories)
- `_select_workflow()` étendu pour gérer `story_faceless` et `story_character`
- Scheduler main.py réécrit : appelle `get_content_plan()` → itère sur le plan plutôt que sur les types bruts
- Les choix du planner (mood, location, outfit, lighting) sont injectés comme `override_params` dans `run_pipeline()`
- `pool_type` trackée dans history.json via `generate_concept(pool_type=...)` pour distinguer faceless/character
- `madison.json` enrichi : `tone` et `audience` avec vrais démographiques Instagram (men 25-64, core 35-54)
- Prochaine étape : tester le pipeline complet avec le planner sur VPS
