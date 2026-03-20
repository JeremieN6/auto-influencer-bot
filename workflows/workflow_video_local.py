"""
workflows/workflow_video_local.py — Workflow Vidéo 1 : source locale.

DESCRIPTION :
  Source = pool de vidéos dans data/videos/ (sélectionnées manuellement).
  Phase test en local avant workflow Pinterest vidéo.
  Déplacer les vidéos sur le VPS quand le pipeline est validé.

FLUX COMPLET :
  [data/videos/ — sélection aléatoire]
        ↓
  [frame_extractor.py — Frame 1]
        ↓
  [Gemini Vision — détection personnage sur Frame 1]
        ↓
  ┌── Personnage détecté
  │     ↓ image_to_json() → JSON de scène (outfit, décor, éclairage)
  │     ↓ inject_madison_body() → JSON enrichi
  │     ↓ generate_image() → Image Madison dans le bon outfit/décor
  │     ↓ kling_generator.generate_video_motion_control(
  │           character_image=madison_image,
  │           source_video=video_locale
  │       ) → Vidéo finale Madison
  │     ↓ caption_generator → caption Reel
  │     ↓ Retourne (video_path, public_url, filename, caption, type="reel")
  │
  └── Pas de personnage
        ↓ caption_generator → caption ambiance
        ↓ Retourne (video_path, public_url, filename, caption, type="story")

Appelé par : main.py via --workflow video_local
"""

import json
import os
import random
import shutil
from pathlib import Path

from caption_generator import generate_caption
from concept_generator import build_caption_prompt, get_current_calendar_step
from frame_extractor import extract_best_frame
from image_generator import generate_image, image_to_json, inject_madison_body
from logger import get_logger, log_section, log_step
from prompts import PROMPT_JSON_TO_IMAGE

logger = get_logger(__name__)

# Extensions vidéo acceptées
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".avi"}
TOTAL_STEPS      = 5


# ================================================================
# Helpers
# ================================================================

def _pick_random_video(videos_dir: str) -> str:
    """Sélectionne aléatoirement une vidéo depuis le dossier donné."""
    vdir = Path(videos_dir)

    if not vdir.exists():
        raise FileNotFoundError(
            f"Dossier vidéos introuvable : {vdir.absolute()}\n"
            "Créer le dossier et y déposer des vidéos (.mp4, .mov, .webm)."
        )

    videos = [
        f for f in vdir.iterdir()
        if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS
    ]

    if not videos:
        raise FileNotFoundError(
            f"Aucune vidéo trouvée dans {videos_dir}. "
            "Y déposer des fichiers .mp4 / .mov / .webm pour tester le pipeline."
        )

    chosen = random.choice(videos)
    logger.info(f"Vidéo sélectionnée : {chosen.name}")
    return str(chosen)


def _expose_video_via_nginx(video_path: str) -> tuple[str, str]:
    """
    Copie la vidéo dans le dossier nginx et retourne (filename, public_url).
    Réutilise les variables de config (NGINX_OUTPUT_DIR, NGINX_BASE_URL).
    """
    from config import NGINX_BASE_URL, NGINX_OUTPUT_DIR

    filename   = Path(video_path).name
    nginx_path = os.path.join(NGINX_OUTPUT_DIR, filename)
    public_url = f"{NGINX_BASE_URL}/{filename}"

    os.makedirs(NGINX_OUTPUT_DIR, exist_ok=True)

    if os.path.abspath(video_path) != os.path.abspath(nginx_path):
        shutil.copy(video_path, nginx_path)
        logger.info(f"Vidéo exposée via nginx : {public_url}")
    else:
        logger.info(f"Vidéo déjà dans nginx : {public_url}")

    return filename, public_url


def _build_video_caption_prompt(scene_json: dict, step: dict, video_type: str) -> str:
    """
    Construit un prompt caption adapté au contenu vidéo et au type (reel/story).
    """
    location = ""
    outfit   = ""
    mood     = ""
    try:
        loc    = scene_json.get("location", {})
        location = loc.get("description") or loc.get("place") or loc.get("setting") or "aesthetic location"
        subject = scene_json.get("subject", {})
        wardrobe = subject.get("wardrobe", {})
        outfit   = wardrobe.get("top") or wardrobe.get("top_garment") or "casual outfit"
        mood     = scene_json.get("mood") or scene_json.get("atmosphere") or "confident"
    except Exception:
        pass

    concept_hint = {
        "location": location,
        "outfit":   outfit,
        "mood":     mood,
        "lighting": scene_json.get("lighting", {}).get("quality", "natural light"),
    }

    base_prompt = build_caption_prompt(concept_hint, step)
    type_hint   = "[Instagram Reel — motion content, dynamic energy]" if video_type == "reel" \
                  else "[Instagram Story — ambiance vidéo, no main character]"

    return f"{base_prompt}\n\n{type_hint}"


def _build_ambiance_caption_prompt(video_path: str, step: dict) -> str:
    """Prompt caption pour une vidéo sans personnage (ambiance / Story)."""
    concept_hint = {
        "location": "aesthetic scene",
        "mood":     "chill aesthetic",
        "outfit":   "",
        "lighting": "natural light",
    }
    base_prompt = build_caption_prompt(concept_hint, step)
    return f"{base_prompt}\n\n[Instagram Story — ambiance vidéo, pas de personnage visible]"


# ================================================================
# Point d'entrée
# ================================================================

def run(concept: dict | None = None) -> tuple[str, str, str, str, str]:
    """
    Exécute le workflow vidéo local complet.

    Args:
        concept : dict optionnel (pour contexte calendrier).
                  Si None, un step calendrier est récupéré automatiquement.

    Returns:
        (local_video_path, public_url, filename, caption, video_type)
        - video_type : "reel"  (personnage → Motion Control)
                       "story" (pas de personnage → vidéo brute)

    Raises:
        FileNotFoundError : si data/videos/ est vide ou inexistant
        RuntimeError      : si ffmpeg n'est pas installé
        ValueError        : si Gemini ou Kling échoue
    """
    from config import VIDEOS_DIR

    log_section(__name__, "WORKFLOW VIDÉO LOCAL")
    step = get_current_calendar_step()

    # ── Étape 1/5 : Sélection vidéo locale ──────────────────────
    log_step(__name__, 1, TOTAL_STEPS, "Sélection vidéo locale")
    video_path = _pick_random_video(VIDEOS_DIR)

    # ── Étape 2/5 : Extraction frame intelligente ───────────────
    log_step(__name__, 2, TOTAL_STEPS, "Extraction frame intelligente (scan multi-timestamps)")
    frame_path = extract_best_frame(video_path)
    logger.info(f"Frame extraite : {frame_path}")

    # ── Étape 3/5 : Détection personnage ────────────────────────
    log_step(__name__, 3, TOTAL_STEPS, "Détection personnage (Gemini Vision)")
    from pinterest_scraper import _detect_person_in_image
    has_person = _detect_person_in_image(frame_path)
    logger.info(f"Personnage détecté : {has_person}")

    if has_person:
        return _run_person_branch(video_path, frame_path, step)
    else:
        return _run_ambiance_branch(video_path, frame_path, step)


# ================================================================
# Branche Personnage → Motion Control → Reel
# ================================================================

def _run_person_branch(
    video_path: str,
    frame_path: str,
    step: dict,
) -> tuple[str, str, str, str, str]:
    """Pipeline complet quand un personnage est détecté sur la Frame 1."""

    # ── Étape 4a/5 : Analyse scène + génération image Madison ───
    log_step(__name__, 4, TOTAL_STEPS, "Analyse scène + génération image Madison (Gemini)")

    scene_json = image_to_json(frame_path)
    logger.info(f"JSON de scène extrait — clés : {list(scene_json.keys())}")

    scene_json = inject_madison_body(scene_json)
    logger.info("Bloc corps Madison injecté")

    # Nettoyer la frame temporaire (plus utile après analyse)
    if os.path.exists(frame_path):
        os.remove(frame_path)
        logger.debug(f"Frame temporaire supprimée : {frame_path}")

    prompt_text = PROMPT_JSON_TO_IMAGE.format(
        scene_json=json.dumps(scene_json, indent=2, ensure_ascii=False)
    )
    madison_image_path, _ = generate_image(prompt_text)
    logger.info(f"Image Madison générée : {madison_image_path}")

    # ── Étape 5a/5 : Kling Motion Control ───────────────────────
    log_step(__name__, 5, TOTAL_STEPS, "Kling Motion Control")

    from kling_generator import build_motion_prompt, generate_video_motion_control

    motion_prompt      = build_motion_prompt(scene_json)
    final_video_path   = generate_video_motion_control(
        character_image_path=madison_image_path,
        source_video_path=video_path,
        motion_prompt=motion_prompt,
    )

    filename, public_url = _expose_video_via_nginx(final_video_path)

    # Caption Reel
    caption_prompt = _build_video_caption_prompt(scene_json, step, "reel")
    caption        = generate_caption(caption_prompt)

    logger.info(f"=== Workflow Vidéo Local terminé (reel) : {final_video_path} ===")
    return final_video_path, public_url, filename, caption, "reel", madison_image_path, ""


# ================================================================
# Branche Ambiance → Vidéo brute → Story
# ================================================================

def _run_ambiance_branch(
    video_path: str,
    frame_path: str,
    step: dict,
) -> tuple[str, str, str, str, str]:
    """Pipeline quand aucun personnage n'est détecté — vidéo utilisée telle quelle."""
    log_step(__name__, 4, TOTAL_STEPS, "Flux ambiance : vidéo utilisée brute")
    log_step(__name__, 5, TOTAL_STEPS, "Génération caption ambiance")

    # Nettoyer la frame temporaire
    if os.path.exists(frame_path):
        os.remove(frame_path)
        logger.debug(f"Frame temporaire supprimée : {frame_path}")

    filename, public_url = _expose_video_via_nginx(video_path)

    caption_prompt = _build_ambiance_caption_prompt(video_path, step)
    caption        = generate_caption(caption_prompt)

    logger.info(f"=== Workflow Vidéo Local terminé (story/ambiance) : {video_path} ===")
    return video_path, public_url, filename, caption, "story", "", ""
