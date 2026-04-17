"""
main.py — Orchestrateur principal du pipeline.

Appelé automatiquement par le cron tous les jours à midi (heure française).
Peut également être lancé manuellement pour tester.

FLUX COMPLET (workflow Pinterest V1) :
  1. Génération concept créatif (location / outfit / pose / mood / lighting)
  2. Récupération de l'étape calendrier courante (format, hashtags)
  3. Workflow Pinterest :
     a. Scraping Pinterest → image d'inspiration avec personnage
     b. Gemini Vision → JSON de scène
     c. Gemini → image finale avec influenceuse
  4. Claude → caption + hashtags
  5. Sauvegarde pending_state + envoi Telegram pour validation humaine
  → L'utilisateur valide via /validate dans Telegram
  → instagram_publisher.publish_post() est appelé depuis le bot

USAGE :
  python main.py                        → pipeline Pinterest (défaut)
  python main.py --workflow generatif   → pipeline génératif V2
  python main.py --dry-run              → exécute sans envoyer sur Telegram ni sauvegarder l'historique
  python main.py --override-params /path/to/params.json  → concept manuel depuis /run Telegram
  python main.py --no-persist           → ne modifie pas history.json (utilisé par /run)
"""

import argparse
import asyncio
import os
import sys

from caption_generator import generate_caption
from concept_generator import (
    build_caption_prompt,
    generate_concept,
    get_current_calendar_step,
)
from logger import log, log_section, setup_logger
from telegram_bot import save_pending_state, send_for_validation, send_video_for_validation


async def _send_telegram_info(message: str) -> None:
    """Envoie une notification texte simple sur Telegram si configuré."""
    from telegram import Bot
    from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return

    async with Bot(token=TELEGRAM_BOT_TOKEN) as bot:
        await bot.send_message(chat_id=TELEGRAM_CHAT_ID, text=message)


def _has_local_videos() -> bool:
    """Vérifie si /data/videos/ contient des fichiers .mp4."""
    from video_batch_manager import has_local_videos
    return has_local_videos()


def _select_workflow(step: dict) -> str:
    """
    Sélectionne le workflow en fonction du type de contenu demandé par le calendrier
    ou par le content planner.

    Mapping :
    - feed            → "pinterest" (image)
    - story           → "video_pinterest" (pool story — fallback générique)
    - story_faceless  → "video_pinterest" (pool story)
    - story_character → "video_pinterest" (pool reel)
    - reel            → 50/50 aléatoire "video_local" / "video_pinterest" (pool reel)
              Si video_local sélectionné mais pas de vidéos locales → fallback video_pinterest
    """
    import random as _rng

    step_type = step.get("type", "feed")
    workflow_hint = step.get("workflow", "")

    if step_type == "feed":
        return "pinterest"

    if step_type in ("story", "story_faceless", "story_character"):
        return "video_pinterest"

    if step_type == "reel":
        # "auto_video" dans le calendrier = 50/50 entre video_local et video_pinterest
        if workflow_hint == "auto_video":
            choice = _rng.choice(["video_local", "video_pinterest"])
            if choice == "video_local":
                # Vérifier qu'on a des vidéos locales, sinon fallback
                if not _has_local_videos():
                    from video_batch_manager import auto_refill_if_empty
                    refilled = auto_refill_if_empty()
                    if refilled:
                        log("info", "main", "Auto-refill : nouvelle vague de vidéos transférée")
                if _has_local_videos():
                    log("info", "main", "Reel : video_local sélectionné (50/50)")
                    return "video_local"
                else:
                    log("info", "main", "Reel : video_local sélectionné mais pas de vidéos → fallback video_pinterest")
                    return "video_pinterest"
            else:
                log("info", "main", "Reel : video_pinterest sélectionné (50/50)")
                return "video_pinterest"
        elif workflow_hint == "video_local":
            return "video_local"
        elif workflow_hint == "video_pinterest":
            return "video_pinterest"
        return "video_pinterest"  # fallback reel

    return "pinterest"  # fallback sécurité


def _load_relevant_pool(category: str | None = None) -> list[str]:
    """
    Charge le pool de keywords depuis relevant_keywords dans variables.json.

    Args:
        category : nom de catégorie ("lifestyle", "beach", "outfit").
                   Si None, retourne tous les keywords aplatis.

    Returns:
        liste aplatie de keywords
    """
    import json
    from pathlib import Path

    variables_path = Path(__file__).parent / "data" / "variables.json"
    with open(variables_path, encoding="utf-8") as f:
        variables = json.load(f)
    relevant = variables.get("relevant_keywords", {})

    if category:
        pool = relevant.get(category)
        if pool is None:
            available = list(relevant.keys())
            raise ValueError(
                f"Catégorie --relevant '{category}' inconnue. "
                f"Disponibles : {available}"
            )
        return pool
    # Aplatir toutes les catégories
    return [kw for kws in relevant.values() for kw in kws]


def run_pipeline(
    workflow: str = "pinterest",
    override_params: dict | None = None,
    dry_run: bool = False,
    relevant: str | None = None,
    persist: bool = True,
    pool: str | None = None,
    content_type: str | None = None,
) -> dict:
    """
    Exécute le pipeline complet de génération de contenu.

    Args:
        workflow        : "pinterest" (V1), "generatif" (V2), ou "pinterest_inpainting"
        override_params : paramètres manuels depuis /run Telegram
        dry_run         : si True, pipeline sans envoi Telegram ni sauvegarde historique
        persist         : si False, ne modifie pas history.json (utilisé par /run manuel)
        relevant        : si fourni, active le mode relevant keywords.
                          Valeur = catégorie ("lifestyle", "beach", "outfit")
                          ou "all" pour toutes les catégories.
        content_type    : type de contenu (story/reel/feed) — pour le scheduler multi-fréquence.

    Returns:
        dict avec les clés : image_path, public_url, filename, caption, concept, step
    """
    log_section("main", f"PIPELINE DÉMARRÉ — workflow={workflow} | dry_run={dry_run}")

    # ── Vérification références influenceuse ───────────────────────
    if workflow == "pinterest_inpainting":
        from config import INFLUENCER_REF_BODY_PATH, INFLUENCER_REF_FACE_PATH
        for path, label in [
            (INFLUENCER_REF_FACE_PATH, "référence visage"),
            (INFLUENCER_REF_BODY_PATH, "référence corps"),
        ]:
            if not os.path.exists(path):
                raise FileNotFoundError(
                    f"❌ Fichier manquant : {path} ({label})\n"
                    f"   Dépose le fichier dans /data avant de lancer le pipeline inpainting."
                )

    # ── Étape 1 : Concept créatif ────────────────────────────────
    log("info", "main", "=== Étape 1/4 : Génération concept ===")
    concept = generate_concept(
        override_params=override_params,
        persist=persist and not dry_run,
        content_type=content_type,
        pool_type=pool,
    )
    log("info", "main", (
        f"Concept : {concept['mood']} | {concept['outfit']} | "
        f"{concept['location']} | {concept['lighting']}"
    ))

    # ── Étape 2 : Étape calendrier ───────────────────────────────
    log("info", "main", "=== Étape 2/4 : Calendrier éditorial ===")
    step = get_current_calendar_step()
    log("info", "main", f"Format : {step['format']} | Type : {step['type']} | Hashtags : {step['hashtags']}")

    # ── Étape 3 : Workflow image ─────────────────────────────────
    log("info", "main", f"=== Étape 3/4 : Workflow {workflow} ===")

    if workflow in ("pinterest", "pinterest_inpainting", "generatif", "manual_image", "manual_gen", "manual_inpaint"):
        from image_generator import ImageSafetyError, disable_safety_fallback, enable_safety_fallback

        _MAX_SAFETY_CONCEPTS = 3   # 3 concepts différents avant fallback sanitisé
        _safety_count        = 0
        _use_safety_fallback = False

        while True:
            if _use_safety_fallback:
                enable_safety_fallback()
            try:
                if workflow == "pinterest":
                    from workflows.workflow_pinterest import run as run_workflow
                    keyword_pool = None
                    if relevant:
                        category     = None if relevant == "all" else relevant
                        keyword_pool = _load_relevant_pool(category)
                        log("info", "main", f"Mode relevant : {relevant} — {len(keyword_pool)} keywords chargés")
                    local_path, public_url, filename, source_url, search_query, scene_json = run_workflow(concept, keyword_pool=keyword_pool)
                    wildcard_used = None
                elif workflow == "pinterest_inpainting":
                    from workflows.workflow_pinterest_inpainting import run as run_workflow  # type: ignore[assignment]
                    local_path, public_url, filename = run_workflow(concept)
                    source_url, search_query, scene_json = None, None, None
                    wildcard_used = None
                elif workflow == "generatif":
                    from workflows.workflow_generatif import run as run_workflow  # type: ignore[assignment]
                    local_path, public_url, filename, wildcard_used = run_workflow(concept)
                    source_url, search_query, scene_json = None, None, None
                elif workflow == "manual_image":
                    source_path = (override_params or {}).get("source_path")
                    is_url      = (override_params or {}).get("is_url", False)
                    if not source_path:
                        raise ValueError("manual_image requiert 'source_path' dans override_params")
                    if is_url and "pinterest" in source_path.lower():
                        from pinterest_scraper import scrape_image_from_pin_url
                        log("info", "main", f"URL Pinterest — scraping image source : {source_path}")
                        source_path = scrape_image_from_pin_url(source_path)
                        log("info", "main", f"Image Pinterest téléchargée : {source_path}")
                    from workflows.workflow_backup import run as run_backup
                    local_path, public_url, filename, scene_json = run_backup(source_path)
                    source_url, search_query = None, None
                    wildcard_used = None
                elif workflow == "manual_gen":
                    source_path = (override_params or {}).get("source_path")
                    prompt_text = (override_params or {}).get("prompt")
                    if not source_path:
                        raise ValueError("manual_gen requiert 'source_path' dans override_params")
                    if not prompt_text:
                        raise ValueError("manual_gen requiert 'prompt' dans override_params")
                    from image_generator import generate_image_from_source
                    local_path, public_url = generate_image_from_source(prompt_text, source_path)
                    filename = os.path.basename(local_path)
                    source_url, search_query, scene_json = None, None, None
                    wildcard_used = None
                elif workflow == "manual_inpaint":
                    source_path   = (override_params or {}).get("source_path")
                    custom_prompt = (override_params or {}).get("prompt", "")
                    if not source_path:
                        raise ValueError("manual_inpaint requiert 'source_path' dans override_params")
                    from config import INFLUENCER_NAME as _INFL_NAME
                    from config import INFLUENCER_REF_FACE_PATH as _REF_FACE
                    from config import INFLUENCER_REF_BODY_PATH as _REF_BODY
                    for _ref_path in [_REF_FACE, _REF_BODY]:
                        if not os.path.exists(_ref_path):
                            raise FileNotFoundError(
                                f"❌ Référence manquante : {_ref_path}\n"
                                f"   Génère-la avec scripts/generate_body_ref.py"
                            )
                    from inpainting import replace_person
                    local_path, public_url, filename = replace_person(
                        source_image_path=source_path,
                        influencer_name=_INFL_NAME,
                        ref_face_path=_REF_FACE,
                        ref_body_path=_REF_BODY,
                        custom_prompt=custom_prompt,
                    )
                    source_url, search_query, scene_json = None, None, None
                    wildcard_used = None
                break  # succès — sortir de la boucle

            except ImageSafetyError:
                if _use_safety_fallback:
                    # Fallback sanitisé activé mais Gemini bloque encore — abandon
                    disable_safety_fallback()
                    raise ValueError(
                        "IMAGE_SAFETY persistant après 3 concepts différents + prompt sanitisé — "
                        "abandon du run."
                    )
                _safety_count += 1
                if _safety_count < _MAX_SAFETY_CONCEPTS:
                    log("warning", "main",
                        f"IMAGE_SAFETY — concept {_safety_count}/{_MAX_SAFETY_CONCEPTS} refusé "
                        f"— nouveau concept...")
                    concept = generate_concept(persist=False)
                else:
                    log("warning", "main",
                        f"IMAGE_SAFETY — {_MAX_SAFETY_CONCEPTS} concepts refusés "
                        f"— dernier essai avec prompt sanitisé...")
                    _use_safety_fallback = True
            finally:
                if _use_safety_fallback:
                    disable_safety_fallback()

    elif workflow in ("video_local", "video_pinterest", "manual_video"):
        # Workflows vidéo — la caption est générée DANS le workflow
        log("info", "main", f"=== Workflow vidéo : {workflow} ===")
        search_query_used = ""  # sera renseigné uniquement par video_pinterest

        if workflow == "video_local":
            from workflows.workflow_video_local import run as run_video_workflow
            from image_generator import ImageSafetyError as _ImgSafetyErr, enable_safety_fallback as _esf, disable_safety_fallback as _dsf
            # Pré-activer le fallback pour que generate_image() gère IMAGE_SAFETY en interne
            # (sanitisation du prompt sur la même vidéo — évite de brûler une 2ème vidéo)
            _esf()
            try:
                video_path, video_public_url, video_filename, caption, video_type, madison_image_path, source_video_path, body_status = run_video_workflow(concept, dry_run=dry_run)
            except _ImgSafetyErr:
                raise ValueError("IMAGE_SAFETY/OTHER persistant sur video_local — abandon")
            finally:
                _dsf()

            # Auto-refill : si /data/videos/ vide après traitement, transférer le prochain batch
            from video_batch_manager import auto_refill_if_empty
            refilled = auto_refill_if_empty()
            if refilled:
                log("info", "main", "Nouveau batch vidéo transféré dans /data/videos/")
            elif not _has_local_videos():
                log("info", "main", "Plus de vidéos locales — prochains runs via calendrier")
        else:
            from workflows.workflow_video_pinterest import run as run_video_workflow  # type: ignore[assignment]
            if pool in ("reel", "story"):
                # Pool explicitement fourni via --pool → priorité absolue
                keyword_pool_type = pool
                log("info", "main", f"Pool keywords vidéo : {keyword_pool_type} (--pool explicite)")
            else:
                # Dériver du calendrier, avec fallback reel si step incompatible (ex: feed)
                step_type = step.get("type", "reel")
                if step_type in ("story", "reel"):
                    keyword_pool_type = step_type
                else:
                    log(
                        "warning",
                        "main",
                        f"Step calendrier '{step_type}' incompatible pour video_pinterest — fallback pool 'reel'",
                    )
                    keyword_pool_type = "reel"
                log("info", "main", f"Pool keywords vidéo : {keyword_pool_type} (calendrier step={step_type})")

            relevant_theme = None if relevant in (None, "all") else relevant
            if relevant_theme:
                log("info", "main", f"Mode relevant vidéo : thème '{relevant_theme}' (pool {keyword_pool_type})")
            elif relevant == "all":
                log("info", "main", "Mode relevant vidéo : all — pool complet conservé")

            video_path, video_public_url, video_filename, caption, video_type, madison_image_path, source_video_path, body_status, _search_queries = run_video_workflow(
                concept,
                pool_type=keyword_pool_type,
                relevant_theme=relevant_theme,
            )
            search_query_used = " | ".join(_search_queries) if _search_queries else ""
        if workflow == "manual_video":
            source_path = (override_params or {}).get("source_path")
            if not source_path:
                raise ValueError("manual_video requiert 'source_path' dans override_params")
            from workflows.workflow_video_local import run_from_path as _run_video_manual
            from image_generator import ImageSafetyError as _ImgSafetyErr2, enable_safety_fallback as _esf2, disable_safety_fallback as _dsf2
            try:
                video_path, video_public_url, video_filename, caption, video_type, madison_image_path, source_video_path, body_status = _run_video_manual(source_path, concept)
            except _ImgSafetyErr2:
                log("warning", "main", "IMAGE_SAFETY/OTHER sur manual_video — activation fallback sanitisé")
                _esf2()
                try:
                    video_path, video_public_url, video_filename, caption, video_type, madison_image_path, source_video_path, body_status = _run_video_manual(source_path, concept)
                except _ImgSafetyErr2:
                    raise ValueError("IMAGE_SAFETY/OTHER persistant sur manual_video — abandon")
                finally:
                    _dsf2()
        log("info", "main", f"Vidéo générée : {video_path} | type={video_type}")
        log("info", "main", f"Caption : {caption[:100]}...")

        motion_control_meta = {
            "trim_applied": False,
            "original_duration_s": None,
            "trimmed_duration_s": None,
        }
        if video_type == "reel":
            from kling_generator import get_last_motion_control_metadata
            motion_control_meta = get_last_motion_control_metadata()
            if motion_control_meta.get("trim_applied"):
                log(
                    "info",
                    "main",
                    "Motion Control : trim auto appliqué "
                    f"({motion_control_meta.get('original_duration_s')}s → "
                    f"{motion_control_meta.get('trimmed_duration_s')}s)",
                )

        video_state = {
            "media_type":         "video",
            "video_path":         video_path,
            "video_public_url":   video_public_url,
            "video_filename":     video_filename,
            "caption":            caption,
            "video_type":         video_type,
            "_intermediate":      False,
            "madison_image_path": madison_image_path,
            "source_video_path":  source_video_path,
            # Champs image — None pour les workflows vidéo
            "image_path":     None,
            "public_url":     None,
            "image_filename": None,
            "concept":        concept,
            "step":           step,
            "last_prompt":    None,
            "motion_control_trim_applied": motion_control_meta.get("trim_applied", False),
            "motion_control_trim_original_duration_s": motion_control_meta.get("original_duration_s"),
            "motion_control_trimmed_duration_s": motion_control_meta.get("trimmed_duration_s"),
        }

        if dry_run:
            log("info", "main", "[DRY RUN] — Pas d'envoi Telegram, pas de sauvegarde historique")
            sep = "═" * 60
            madison_line = f"  Image Madison   : {madison_image_path}\n" if madison_image_path else ""
            source_line  = f"  Vidéo source    : {source_video_path}\n" if source_video_path else ""
            keywords_line = f"  Mots-clés       : {search_query_used}\n" if search_query_used else ""
            log("info", "main",
                f"\n{sep}\n"
                f"  DRY RUN — WORKFLOW VIDÉO\n"
                f"{sep}\n"
                f"{keywords_line}"
                f"{source_line}"
                f"{madison_line}"
                f"  Vidéo générée   : {video_path}\n"
                f"  Type            : {video_type}\n"
                f"  Corps Madison   : {body_status}\n"
                f"{sep}"
            )
            log("info", "main", f"[DRY RUN] Caption :\n{caption}")
        else:
            save_pending_state(video_state)
            log("info", "main", "pending_state vidéo sauvegardé")
            asyncio.run(send_video_for_validation(video_path, caption, video_type))
            log("info", "main", "Vidéo envoyée sur Telegram — en attente de validation")

        log_section("main", "PIPELINE VIDÉO TERMINÉ")
        return video_state

    elif workflow == "video_i2v":
        # Image-to-Video : image influenceuse + prompt → Kling anime directement
        log("info", "main", "=== Workflow vidéo : video_i2v (Image-to-Video) ===")
        first_frame_path = (override_params or {}).get("source_path")
        prompt_text      = (override_params or {}).get("prompt")
        aspect_ratio     = (override_params or {}).get("aspect_ratio", "9:16")

        if not first_frame_path:
            raise ValueError("video_i2v requiert 'source_path' dans override_params")
        if not prompt_text:
            raise ValueError("video_i2v requiert 'prompt' dans override_params")

        from kling_generator import generate_video_image2video as _gen_i2v
        video_path = _gen_i2v(
            first_frame_path=first_frame_path,
            prompt=prompt_text,
            aspect_ratio=aspect_ratio,
        )
        video_filename = os.path.basename(video_path)

        # Exposer via nginx si configuré, sinon URL vide (sera gérée à la publication)
        from config import NGINX_BASE_URL as _nginx_url
        _nginx_placeholder = "ton-domaine.com"
        if _nginx_placeholder not in _nginx_url:
            from kling_generator import _expose_file_via_nginx as _expose
            video_public_url = _expose(video_path)
        else:
            video_public_url = ""

        # Caption via Claude
        from caption_generator import generate_caption as _gen_caption
        from concept_generator import build_caption_prompt as _build_prompt
        caption_prompt = _build_prompt(concept, step)
        caption = _gen_caption(caption_prompt)

        video_type = "reel"  # i2v → format vertical par défaut
        log("info", "main", f"Vidéo i2v générée : {video_path} | ratio={aspect_ratio}")
        log("info", "main", f"Caption : {caption[:100]}...")

        video_state = {
            "media_type":         "video",
            "video_path":         video_path,
            "video_public_url":   video_public_url,
            "video_filename":     video_filename,
            "caption":            caption,
            "video_type":         video_type,
            "_intermediate":      False,
            "madison_image_path": first_frame_path,
            "source_video_path":  None,
            "image_path":     None,
            "public_url":     None,
            "image_filename": None,
            "concept":        concept,
            "step":           step,
            "last_prompt":    None,
        }

        if dry_run:
            log("info", "main", "[DRY RUN] — Pas d'envoi Telegram, pas de sauvegarde historique")
            log("info", "main",
                f"\n{'═'*60}\n"
                f"  DRY RUN — WORKFLOW VIDEO I2V\n"
                f"{'═'*60}\n"
                f"  Image source    : {first_frame_path}\n"
                f"  Vidéo générée   : {video_path}\n"
                f"  Ratio           : {aspect_ratio}\n"
                f"{'═'*60}"
            )
            log("info", "main", f"[DRY RUN] Caption :\n{caption}")
        else:
            save_pending_state(video_state)
            log("info", "main", "pending_state video_i2v sauvegardé")
            asyncio.run(send_video_for_validation(video_path, caption, video_type))
            log("info", "main", "Vidéo i2v envoyée sur Telegram — en attente de validation")

        log_section("main", "PIPELINE VIDEO I2V TERMINÉ")
        return video_state

    elif workflow == "video_mc":
        # Motion Control avec image Madison fournie manuellement (saute l'étape Gemini)
        log("info", "main", "=== Workflow vidéo : video_mc (Motion Control + image manuelle) ===")
        madison_image_path = (override_params or {}).get("source_image")
        source_video_path  = (override_params or {}).get("source_path")

        if not madison_image_path:
            raise ValueError("video_mc requiert 'source_image' dans override_params")
        if not source_video_path:
            raise ValueError("video_mc requiert 'source_path' dans override_params")

        from kling_generator import generate_video_motion_control as _gen_mc
        video_path = _gen_mc(
            character_image_path=madison_image_path,
            source_video_path=source_video_path,
        )
        video_filename = os.path.basename(video_path)

        from config import NGINX_BASE_URL as _nginx_url2
        if "ton-domaine.com" not in _nginx_url2:
            from kling_generator import _expose_file_via_nginx as _expose2
            video_public_url = _expose2(video_path)
        else:
            video_public_url = ""

        from caption_generator import generate_caption as _gen_caption2
        from concept_generator import build_caption_prompt as _build_prompt2
        caption_prompt = _build_prompt2(concept, step)
        caption = _gen_caption2(caption_prompt)

        video_type = "reel"
        log("info", "main", f"Vidéo mc générée : {video_path}")
        log("info", "main", f"Caption : {caption[:100]}...")

        video_state = {
            "media_type":         "video",
            "video_path":         video_path,
            "video_public_url":   video_public_url,
            "video_filename":     video_filename,
            "caption":            caption,
            "video_type":         video_type,
            "_intermediate":      False,
            "madison_image_path": madison_image_path,
            "source_video_path":  source_video_path,
            "image_path":     None,
            "public_url":     None,
            "image_filename": None,
            "concept":        concept,
            "step":           step,
            "last_prompt":    None,
        }

        if dry_run:
            log("info", "main", "[DRY RUN] — Pas d'envoi Telegram, pas de sauvegarde historique")
            log("info", "main",
                f"\n{'═'*60}\n"
                f"  DRY RUN — WORKFLOW VIDEO MC\n"
                f"{'═'*60}\n"
                f"  Image Madison   : {madison_image_path}\n"
                f"  Vidéo source    : {source_video_path}\n"
                f"  Vidéo générée   : {video_path}\n"
                f"{'═'*60}"
            )
            log("info", "main", f"[DRY RUN] Caption :\n{caption}")
        else:
            save_pending_state(video_state)
            log("info", "main", "pending_state video_mc sauvegardé")
            asyncio.run(send_video_for_validation(video_path, caption, video_type))
            log("info", "main", "Vidéo mc envoyée sur Telegram — en attente de validation")

        log_section("main", "PIPELINE VIDEO MC TERMINÉ")
        return video_state

    else:
        raise ValueError(
            f"Workflow inconnu : '{workflow}'. "
            "Valeurs acceptées : 'pinterest', 'pinterest_inpainting', 'generatif', "
            "'video_local', 'video_pinterest', 'manual_image', 'manual_video', "
            "'manual_gen', 'manual_inpaint', 'video_i2v', 'video_mc'"
        )
    log("info", "main", f"Image générée : {local_path}")

    # ── Étape 4 : Caption ────────────────────────────────────────
    log("info", "main", "=== Étape 4/4 : Génération caption (Claude) ===")
    
    # Utiliser la caption contextualisée depuis le JSON de scène si disponible
    if scene_json:
        from caption_generator import generate_caption_from_scene
        caption = generate_caption_from_scene(scene_json, content_type=step["type"])
        caption_prompt = "[Caption générée depuis JSON de scène]"
        log("info", "main", f"Caption contextualisée générée : {caption[:100]}...")
    else:
        # Fallback sur l'ancien système pour les workflows sans scene_json
        caption_prompt = build_caption_prompt(concept, step)
        caption        = generate_caption(caption_prompt)
        log("info", "main", f"Caption (mode legacy) : {caption[:100]}...")

    # ── Sauvegarde état + envoi Telegram ─────────────────────────
    state = {
        "image_path":     local_path,
        "public_url":     public_url,
        "caption":        caption,
        "concept":        concept,
        "step":           step,
        "last_prompt":    caption_prompt,
        "image_filename": filename,
        "wildcard_used":  wildcard_used,
    }

    if dry_run:
        log("info", "main", "[DRY RUN] — Pas d'envoi Telegram, pas de sauvegarde historique")
        sep = "═" * 60
        pinterest_info = (
            f"  Requête Pinterest : {search_query}\n"
            f"  Image source      : {source_url}\n"
        ) if source_url else ""
        log("info", "main",
            f"\n{sep}\n"
            f"  DRY RUN — RÉSUMÉ\n"
            f"{sep}\n"
            f"{pinterest_info}"
            f"  Image générée     : {local_path}\n"
            f"{sep}"
        )
        log("info", "main", f"[DRY RUN] Caption :\n{caption}")
    else:
        save_pending_state(state)
        log("info", "main", "pending_state sauvegardé")

        asyncio.run(send_for_validation(
            local_path,
            caption,
            wildcard_used=wildcard_used,
            content_type=step.get("type", "feed"),
            destination="instagram",
        ))
        log("info", "main", "Image envoyée sur Telegram — en attente de /validate")

    log_section("main", "PIPELINE TERMINÉ")
    return state


# ================================================================
# Reprise depuis état intermédiaire (--resume-kling)
# ================================================================

def run_resume_kling(params: dict, dry_run: bool = False) -> dict:
    """
    Relance uniquement l'étape Kling depuis un état intermédiaire.

    Utilisé par /retryKling quand le pipeline vidéo a échoué APRES la génération
    de l'image Madison mais AVANT la fin de Kling.
    L'image Madison n'est PAS regénérée — on repart exactement de là où c'était tombé.

    Args:
        params dict keys:
            madison_image_path : chemin de l'image Madison générée
            source_video_path  : chemin de la vidéo source originale
            scene_json         : JSON de scène (motion prompt + caption)
            step               : étape calendrier (caption)
    """
    from caption_generator import generate_caption, generate_caption_from_scene
    from kling_generator import build_motion_prompt, generate_video_motion_control, get_last_motion_control_metadata
    from workflows.workflow_video_local import _build_video_caption_prompt, _expose_video_via_nginx

    madison_image_path = params["madison_image_path"]
    source_video_path  = params["source_video_path"]
    scene_json         = params.get("scene_json") or {}
    step               = params.get("step") or get_current_calendar_step()

    if not os.path.exists(madison_image_path):
        raise FileNotFoundError(f"Image Madison introuvable : {madison_image_path}")
    if not os.path.exists(source_video_path):
        raise FileNotFoundError(f"Vidéo source introuvable : {source_video_path}")

    log_section("main", "RESUME KLING — reprise depuis état intermédiaire")
    log("info", "main", f"Image Madison  : {madison_image_path}")
    log("info", "main", f"Vidéo source   : {source_video_path}")

    motion_prompt    = build_motion_prompt(scene_json)
    final_video_path = generate_video_motion_control(
        character_image_path=madison_image_path,
        source_video_path=source_video_path,
        motion_prompt=motion_prompt,
    )
    motion_control_meta = get_last_motion_control_metadata()
    if motion_control_meta.get("trim_applied"):
        log(
            "info",
            "main",
            "RESUME KLING : trim auto appliqué "
            f"({motion_control_meta.get('original_duration_s')}s → "
            f"{motion_control_meta.get('trimmed_duration_s')}s)",
        )

    filename, public_url = _expose_video_via_nginx(final_video_path)

    if scene_json:
        caption = generate_caption_from_scene(scene_json, content_type="reel")
        caption_prompt = "[Caption générée depuis JSON de scène]"
    else:
        caption_prompt = _build_video_caption_prompt(scene_json, step, "reel")
        caption        = generate_caption(caption_prompt)

    video_state = {
        "media_type":         "video",
        "video_path":         final_video_path,
        "video_public_url":   public_url,
        "video_filename":     filename,
        "caption":            caption,
        "video_type":         "reel",
        "_intermediate":      False,
        "madison_image_path": madison_image_path,
        "source_video_path":  source_video_path,
        "image_path":         None,
        "public_url":         None,
        "image_filename":     None,
        "concept":            None,
        "step":               step,
        "last_prompt":        None,
        "motion_control_trim_applied": motion_control_meta.get("trim_applied", False),
        "motion_control_trim_original_duration_s": motion_control_meta.get("original_duration_s"),
        "motion_control_trimmed_duration_s": motion_control_meta.get("trimmed_duration_s"),
    }

    if dry_run:
        log("info", "main", f"[DRY RUN] RESUME KLING terminé : {final_video_path}")
    else:
        save_pending_state(video_state)
        asyncio.run(send_video_for_validation(final_video_path, caption, "reel"))
        log("info", "main", "Vidéo envoyée sur Telegram — en attente de validation")

    log_section("main", "RESUME KLING TERMINÉ")
    return video_state


# ================================================================
# Point d'entrée CLI
# ================================================================

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pipeline d'automatisation Instagram pour influenceuse IA",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples :
  python main.py                        # Pinterest V1 (défaut, cron)
  python main.py --workflow generatif   # Génératif V2 (non implémenté)
  python main.py --dry-run              # Test sans Telegram ni historique
        """,
    )
    parser.add_argument(
        "--workflow",
        choices=[
            "pinterest", "pinterest_inpainting", "generatif",
            "video_local", "video_pinterest",
            "manual_image", "manual_video",
            "manual_gen", "manual_inpaint",
            "auto",
        ],
        default="auto",
        help="Workflow à utiliser (défaut : auto — sélection selon vidéos locales + calendrier)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Exécuter sans envoyer sur Telegram ni modifier l'historique",
    )
    parser.add_argument(
        "--relevant",
        nargs="?",
        const="all",
        default=None,
        metavar="CATEGORY",
        help=(
            "Utiliser un filtre relevant pour Pinterest image et video_pinterest. "
            "Sans valeur : toutes les catégories. "
            "Avec valeur : catégorie précise (lifestyle, beach, outfit)."
        ),
    )
    parser.add_argument(
        "--override-params",
        default=None,
        metavar="PATH",
        help="Chemin vers un fichier JSON contenant les override_params (depuis /run Telegram).",
    )
    parser.add_argument(
        "--no-persist",
        action="store_true",
        default=False,
        help="Ne pas enregistrer dans history.json (utilisé par /run pour les runs manuels).",
    )
    parser.add_argument(
        "--resume-kling",
        action="store_true",
        default=False,
        help="Reprendre depuis un état intermédiaire — relancer uniquement l'étape Kling.",
    )
    parser.add_argument(
        "--pool",
        choices=["reel", "story"],
        default=None,
        metavar="POOL",
        help=(
            "Pour --workflow video_pinterest : sélectionne explicitement le pool de mots-clés. "
            "'reel' → pinterest_video_tags_reel (mots-clés avec personnage). "
            "'story' → pinterest_video_tags_story (mots-clés ambiance, sans personnage). "
            "Sans cette option, le pool est déduit automatiquement du calendrier éditorial."
        ),
    )
    parser.add_argument(
        "--force",
        action="store_true",
        default=False,
        help=(
            "Forcer le pipeline même si le dernier run date de moins de MIN_DAYS_BETWEEN_RUNS jours. "
            "Utilisé par les commandes Telegram manuelles (/run et alias /generate)."
        ),
    )
    return parser.parse_args()


if __name__ == "__main__":
    setup_logger()
    args = _parse_args()

    # ── Guard anti-run-simultané (lock file) ────────────────────
    # Vérifie si un autre pipeline est déjà en cours d'exécution.
    # En cas de conflit, envoie une notification Telegram et exit.
    # Ignoré en --dry-run (pas d'effet de bord réel).
    _LOCK_FILE = None
    if not args.dry_run:
        from pathlib import Path as _LockPath
        _LOCK_FILE = _LockPath(__file__).parent / "data" / ".pipeline.lock"
        _lock_conflict = False

        if _LOCK_FILE.exists():
            try:
                _existing_pid = int(_LOCK_FILE.read_text().strip())
                os.kill(_existing_pid, 0)  # signal 0 = vérifie existence process
                _lock_conflict = True
            except (ValueError, ProcessLookupError, PermissionError):
                # PID invalide ou process mort → lock fantôme, on nettoie
                _LOCK_FILE.unlink(missing_ok=True)

        if _lock_conflict:
            log("warning", "main",
                f"Guard lock : un pipeline est déjà en cours (PID {_existing_pid}) — abandon.")
            try:
                import asyncio as _al_lock
                from telegram import Bot as _BotLock
                from config import TELEGRAM_BOT_TOKEN as _tok_lock, TELEGRAM_CHAT_ID as _cid_lock

                async def _notify_conflict() -> None:
                    async with _BotLock(token=_tok_lock) as _bot_lock:
                        await _bot_lock.send_message(
                            chat_id=_cid_lock,
                            text=(
                                f"⚠️ *Pipeline déjà en cours* \\(PID `{_existing_pid}`\\)\n\n"
                                f"Un nouveau run a été ignoré \\(cron ou commande manuelle\\)\\.\n"
                                f"Si le pipeline semble bloqué, supprime le verrou :\n"
                                f"`rm /opt/mybots/auto\\-influencer\\-bot/data/\\.pipeline\\.lock`"
                            ),
                            parse_mode="MarkdownV2",
                        )

                _al_lock.run(_notify_conflict())
            except Exception as _lock_notify_err:
                log("warning", "main", f"Notification Telegram lock échouée : {_lock_notify_err}")
            sys.exit(0)

        # Acquérir le lock
        _LOCK_FILE.write_text(str(os.getpid()))

    try:
        # Charger les override_params depuis le fichier temp écrit par /run ou /retryKling
        override_params: dict | None = None
        if args.override_params:
            import json
            try:
                with open(args.override_params, encoding="utf-8") as f:
                    override_params = json.load(f)
                log("info", "main", f"Override params chargés : {list((override_params or {}).keys())}")
            except Exception as e:
                log("error", "main", f"Impossible de lire --override-params '{args.override_params}' : {e}")
            finally:
                import os as _os
                try:
                    _os.remove(args.override_params)
                except Exception:
                    pass

        # ── Guard anti-double-run (scheduler multi-fréquence) ──────
        # En mode auto : le scheduler détermine quels types sont dus.
        # Si rien n'est dû et pas de --force → skip.
        # En mode workflow explicite : pas de guard (l'utilisateur sait ce qu'il fait).
        # En --dry-run : pas de guard non plus.
        _due_types = []
        if not args.dry_run and not args.force and not args.resume_kling and args.workflow == "auto":
            from concept_generator import get_due_content_types
            _due_types = get_due_content_types()
            if not _due_types:
                _skip_message = (
                    "Scheduler : aucun type de contenu n'est dû actuellement — pipeline ignoré.\n"
                    "Utiliser --force pour forcer malgré tout."
                )
                log("info", "main", _skip_message)
                try:
                    asyncio.run(_send_telegram_info(
                        "Pipeline auto ignoré : scheduler — tous les types de contenu sont à jour."
                    ))
                except Exception as _skip_notify_err:
                    log("warning", "main", f"Notification Telegram skip échouée : {_skip_notify_err}")
                sys.exit(0)

        # ── Mode reprise Kling (--resume-kling) ─────────────────────
        if args.resume_kling:
            if not override_params:
                log("error", "main", "--resume-kling requiert --override-params PATH avec les données de reprise")
                sys.exit(1)
            try:
                run_resume_kling(override_params, dry_run=args.dry_run)
                sys.exit(0)
            except KeyboardInterrupt:
                sys.exit(0)
            except Exception as e:
                log("error", "main", f"Erreur reprise Kling : {e}")
                try:
                    import asyncio as _asyncio
                    from telegram import Bot
                    from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

                    async def _notify_kling_error() -> None:
                        async with Bot(token=TELEGRAM_BOT_TOKEN) as bot:
                            await bot.send_message(
                                chat_id=TELEGRAM_CHAT_ID,
                                text=f"🔴 *Erreur retry Kling*\n\n`{str(e)[:500]}`",
                                parse_mode="MarkdownV2",
                            )

                    _asyncio.run(_notify_kling_error())
                except Exception:
                    pass
                sys.exit(1)

        # Résolution du workflow "auto" → scheduler multi-fréquence
        selected_workflow = args.workflow
        if selected_workflow == "auto":
            from concept_generator import get_due_content_types, get_current_calendar_step

            # Si _due_types pas encore calculé (--force ou --dry-run), le calculer maintenant
            if not _due_types:
                _due_types = get_due_content_types()

            if not _due_types:
                # --force ou --dry-run sans contenu dû → exécuter le type le plus en retard (feed par défaut)
                _step = get_current_calendar_step()
                selected_workflow = _select_workflow(_step)
                log("info", "main", f"Mode auto (force/dry-run) — workflow unique : {selected_workflow} (type={_step.get('type')})")
                try:
                    run_pipeline(
                        workflow=selected_workflow,
                        override_params=override_params,
                        dry_run=args.dry_run,
                        relevant=args.relevant,
                        persist=not args.no_persist,
                        pool=args.pool,
                    )
                except KeyboardInterrupt:
                    log("info", "main", "Pipeline interrompu par l'utilisateur")
                except Exception as e:
                    log("error", "main", f"Erreur fatale pipeline : {e}")
                    try:
                        asyncio.run(_send_telegram_info(f"🔴 Erreur pipeline : {str(e)[:500]}"))
                    except Exception:
                        pass
            else:
                # Boucle scheduler avec content planner (conscience de l'influenceur)
                from content_planner import get_content_plan
                from concept_generator import load_calendar

                _plan = get_content_plan(_due_types)
                log("info", "main", f"Content plan : {len(_plan)} contenu(s) à produire")

                _total_produced = 0
                for _idx, _item in enumerate(_plan):
                    item_type = _item["type"]
                    # Mapper le type planner vers le type scheduler (story_faceless/story_character → story)
                    ct_name = "story" if item_type.startswith("story") else item_type

                    # Déterminer le pool_type depuis le type planner
                    if item_type == "story_faceless":
                        _pool = "story"
                    elif item_type == "story_character":
                        _pool = "reel"
                    elif item_type == "reel":
                        _pool = "reel"
                    else:
                        _pool = None

                    # Construire le step pour _select_workflow
                    _calendar_config = load_calendar().get("content_types", {}).get(ct_name, {})
                    _step = {
                        "step": 1,
                        "format": _calendar_config.get("format", "4:5"),
                        "type": item_type,
                        "hashtags": _calendar_config.get("hashtags", True),
                        "note": _item.get("reason", ""),
                        "workflow": _calendar_config.get("workflow", ""),
                    }
                    wf = _select_workflow(_step)
                    _pool_override = args.pool or _pool

                    # Injecter les choix du planner dans les override_params
                    _planner_overrides = {}
                    if _item.get("mood"):
                        _planner_overrides["mood"] = _item["mood"]
                    if _item.get("lighting"):
                        _planner_overrides["lighting"] = _item["lighting"]
                    if _item.get("location"):
                        _planner_overrides["location"] = _item["location"]
                    if _item.get("outfit"):
                        _planner_overrides["outfit"] = _item["outfit"]
                    # Fusionner avec les override_params existants (CLI)
                    _merged_overrides = {**(override_params or {}), **_planner_overrides} if _planner_overrides else override_params

                    log("info", "main",
                        f"Planner [{_idx+1}/{len(_plan)}] : {item_type} → wf={wf} pool={_pool_override} "
                        f"| {_item.get('reason', '')[:80]}")

                    try:
                        run_pipeline(
                            workflow=wf,
                            override_params=_merged_overrides,
                            dry_run=args.dry_run,
                            relevant=_item.get("tag_category") or args.relevant,
                            persist=not args.no_persist,
                            pool=_pool_override,
                            content_type=ct_name,
                        )
                        _total_produced += 1
                    except KeyboardInterrupt:
                        log("info", "main", "Pipeline interrompu par l'utilisateur")
                        sys.exit(0)
                    except Exception as e:
                        log("error", "main",
                            f"Erreur pipeline {item_type} [{_idx+1}/{len(_plan)}] : {e}")
                        try:
                            asyncio.run(_send_telegram_info(
                                f"🔴 Erreur pipeline {item_type} [{_idx+1}/{len(_plan)}] : {str(e)[:400]}"
                            ))
                        except Exception:
                            pass
                        # Continuer avec les autres items du plan

                log("info", "main", f"Scheduler terminé — {_total_produced} contenu(s) produit(s)")
            sys.exit(0)

        # Workflow explicite (pas "auto") — comportement direct
        try:
            run_pipeline(
                workflow=selected_workflow,
                override_params=override_params,
                dry_run=args.dry_run,
                relevant=args.relevant,
                persist=not args.no_persist,
                pool=args.pool,
            )
            sys.exit(0)
        except KeyboardInterrupt:
            log("info", "main", "Pipeline interrompu par l'utilisateur")
            sys.exit(0)
        except Exception as e:
            log("error", "main", f"Erreur fatale pipeline : {e}")
            # Tentative de notification Telegram en cas d'erreur fatale
            try:
                import asyncio as _asyncio
                from telegram import Bot
                from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

                async def _notify_error() -> None:
                    async with Bot(token=TELEGRAM_BOT_TOKEN) as bot:
                        await bot.send_message(
                            chat_id=TELEGRAM_CHAT_ID,
                            text=f"🔴 *Erreur pipeline main\\.py*\n\n`{str(e)[:500]}`",
                            parse_mode="MarkdownV2",
                        )

                _asyncio.run(_notify_error())
            except Exception as notify_err:
                log("error", "main", f"Notification Telegram d'erreur également échouée : {notify_err}")

            sys.exit(1)

    finally:
        # Libérer le lock dans tous les cas (succès, erreur, KeyboardInterrupt)
        if _LOCK_FILE is not None:
            try:
                _LOCK_FILE.unlink(missing_ok=True)
            except Exception:
                pass
