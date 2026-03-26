"""
main.py — Orchestrateur principal du pipeline.

Appelé automatiquement par le cron toutes les 4 jours.
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


def _has_local_videos() -> bool:
    """Vérifie si /data/videos/ contient des fichiers .mp4."""
    from video_batch_manager import has_local_videos
    return has_local_videos()


def _select_workflow(step: dict) -> str:
    """
    Sélectionne le workflow selon la disponibilité de vidéos locales et le calendrier.

    Priorité :
    1. Vidéos locales disponibles → "video_local" (toujours)
    2. Step feed   → "pinterest" (image)
    3. Step story  → "video_pinterest" (pool story)
    4. Step reel   → "video_pinterest" (pool reel)
    """
    if _has_local_videos():
        return "video_local"
    step_type = step.get("type", "feed")
    if step_type == "feed":
        return "pinterest"
    if step_type in ("story", "reel"):
        return "video_pinterest"
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
                    local_path, public_url, filename, source_url, search_query = run_workflow(concept, keyword_pool=keyword_pool)
                    wildcard_used = None
                elif workflow == "pinterest_inpainting":
                    from workflows.workflow_pinterest_inpainting import run as run_workflow  # type: ignore[assignment]
                    local_path, public_url, filename = run_workflow(concept)
                    source_url, search_query = None, None
                    wildcard_used = None
                elif workflow == "generatif":
                    from workflows.workflow_generatif import run as run_workflow  # type: ignore[assignment]
                    local_path, public_url, filename, wildcard_used = run_workflow(concept)
                    source_url, search_query = None, None
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
                    local_path, public_url, filename = run_backup(source_path)
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
                    source_url, search_query = None, None
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
                    source_url, search_query = None, None
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

        if workflow == "video_local":
            from workflows.workflow_video_local import run as run_video_workflow
            from image_generator import ImageSafetyError as _ImgSafetyErr, enable_safety_fallback as _esf, disable_safety_fallback as _dsf
            # Pré-activer le fallback pour que generate_image() gère IMAGE_SAFETY en interne
            # (sanitisation du prompt sur la même vidéo — évite de brûler une 2ème vidéo)
            _esf()
            try:
                video_path, video_public_url, video_filename, caption, video_type, madison_image_path, source_video_path = run_video_workflow(concept, dry_run=dry_run)
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
            keyword_pool_type = step.get("type", "reel")  # "story" ou "reel"
            log("info", "main", f"Pool keywords vidéo : {keyword_pool_type}")
            video_path, video_public_url, video_filename, caption, video_type, madison_image_path, source_video_path = run_video_workflow(concept, pool_type=keyword_pool_type)
        if workflow == "manual_video":
            source_path = (override_params or {}).get("source_path")
            if not source_path:
                raise ValueError("manual_video requiert 'source_path' dans override_params")
            from workflows.workflow_video_local import run_from_path as _run_video_manual
            from image_generator import ImageSafetyError as _ImgSafetyErr2, enable_safety_fallback as _esf2, disable_safety_fallback as _dsf2
            try:
                video_path, video_public_url, video_filename, caption, video_type, madison_image_path, source_video_path = _run_video_manual(source_path, concept)
            except _ImgSafetyErr2:
                log("warning", "main", "IMAGE_SAFETY/OTHER sur manual_video — activation fallback sanitisé")
                _esf2()
                try:
                    video_path, video_public_url, video_filename, caption, video_type, madison_image_path, source_video_path = _run_video_manual(source_path, concept)
                except _ImgSafetyErr2:
                    raise ValueError("IMAGE_SAFETY/OTHER persistant sur manual_video — abandon")
                finally:
                    _dsf2()
        log("info", "main", f"Vidéo générée : {video_path} | type={video_type}")
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
            # Champs image — None pour les workflows vidéo
            "image_path":     None,
            "public_url":     None,
            "image_filename": None,
            "concept":        concept,
            "step":           step,
            "last_prompt":    None,
        }

        if dry_run:
            log("info", "main", "[DRY RUN] — Pas d'envoi Telegram, pas de sauvegarde historique")
            sep = "═" * 60
            madison_line = f"  Image Madison   : {madison_image_path}\n" if madison_image_path else ""
            source_line  = f"  Vidéo source    : {source_video_path}\n" if source_video_path else ""
            log("info", "main",
                f"\n{sep}\n"
                f"  DRY RUN — WORKFLOW VIDÉO\n"
                f"{sep}\n"
                f"{source_line}"
                f"{madison_line}"
                f"  Vidéo générée   : {video_path}\n"
                f"  Type            : {video_type}\n"
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
        caption_prompt = _build_prompt(concept)
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

    else:
        raise ValueError(
            f"Workflow inconnu : '{workflow}'. "
            "Valeurs acceptées : 'pinterest', 'pinterest_inpainting', 'generatif', "
            "'video_local', 'video_pinterest', 'manual_image', 'manual_video', "
            "'manual_gen', 'manual_inpaint', 'video_i2v'"
        )
    log("info", "main", f"Image générée : {local_path}")

    # ── Étape 4 : Caption ────────────────────────────────────────
    log("info", "main", "=== Étape 4/4 : Génération caption (Claude) ===")
    caption_prompt = build_caption_prompt(concept, step)
    caption        = generate_caption(caption_prompt)
    log("info", "main", f"Caption : {caption[:100]}...")

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
    from kling_generator import build_motion_prompt, generate_video_motion_control
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

    filename, public_url = _expose_video_via_nginx(final_video_path)

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
            "Utiliser les relevant_keywords de variables.json pour la requête Pinterest. "
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
    return parser.parse_args()


if __name__ == "__main__":
    setup_logger()
    args = _parse_args()

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

    # Résolution du workflow "auto"
    selected_workflow = args.workflow
    if selected_workflow == "auto":
        from concept_generator import get_current_calendar_step
        _step = get_current_calendar_step()
        selected_workflow = _select_workflow(_step)
        log("info", "main", f"Mode auto — workflow sélectionné : {selected_workflow} (step type={_step.get('type')})")

    try:
        run_pipeline(
            workflow=selected_workflow,
            override_params=override_params,
            dry_run=args.dry_run,
            relevant=args.relevant,
            persist=not args.no_persist,
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
