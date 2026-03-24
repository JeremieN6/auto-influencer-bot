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

# Extensions vidéo acceptées par Kling (mp4 et mov uniquement)
VIDEO_EXTENSIONS = {".mp4", ".mov"}
TOTAL_STEPS      = 5


# ================================================================
# État intermédiaire
# ================================================================

def _save_intermediate_state(
    madison_image_path: str,
    source_video_path: str,
    scene_json: dict,
    step: dict,
) -> None:
    """
    Persiste l'état intermédiaire dans pending_state.json avant d'appeler Kling.

    Si Kling échoue, /status détecte cet état et propose /retryKling.
    Seule l'étape Kling sera relucée — l'image Madison n'est PAS regénérée.
    """
    import json as _json
    from config import PENDING_STATE_PATH

    state = {
        "_intermediate":       True,
        "media_type":          "video",
        "madison_image_path":  madison_image_path,
        "source_video_path":   source_video_path,
        "scene_json":          scene_json,
        "step":                step,
        # Champs attendus par _empty_state (compatibilité)
        "image_path":          None,
        "public_url":          None,
        "caption":             None,
        "concept":             None,
        "last_prompt":         None,
        "image_filename":      None,
        "wildcard_used":       None,
        "video_path":          None,
        "video_public_url":    None,
        "video_filename":      None,
        "video_type":          None,
    }
    os.makedirs(os.path.dirname(PENDING_STATE_PATH), exist_ok=True)
    with open(PENDING_STATE_PATH, "w", encoding="utf-8") as f:
        _json.dump(state, f, indent=2, ensure_ascii=False)
    logger.info("[état intermédiaire] Madison OK, Kling en attente — saved to pending_state.json")


# ================================================================
# Helpers
# ================================================================

def _load_video_history() -> dict:
    """
    Charge l'historique des vidéos utilisées depuis video_history.json.

    Structure :
    {
      "cycle": 1,
      "used": [
        {"name": "video_01.mp4", "selected_at": "2026-03-24T09:45:00"}
      ]
    }
    """
    from config import VIDEO_HISTORY_PATH
    try:
        with open(VIDEO_HISTORY_PATH, encoding="utf-8") as f:
            data = json.load(f)
        # Validation minimale
        if "used" not in data:
            data["used"] = []
        if "cycle" not in data:
            data["cycle"] = 1
        return data
    except (FileNotFoundError, json.JSONDecodeError):
        return {"cycle": 1, "used": []}


def _save_video_history(history: dict) -> None:
    """Persiste l'historique dans video_history.json."""
    from config import VIDEO_HISTORY_PATH
    os.makedirs(os.path.dirname(VIDEO_HISTORY_PATH) or ".", exist_ok=True)
    with open(VIDEO_HISTORY_PATH, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


def _mark_video_used(video_name: str) -> None:
    """
    Enregistre la vidéo dans l'historique comme utilisée.
    Appelé juste avant le pipeline — même si Kling échoue, la vidéo
    n'est plus repiochée (évite les boucles d'échec sur vidéo incompatible).
    """
    from datetime import datetime
    history = _load_video_history()
    history["used"].append({
        "name":        video_name,
        "selected_at": datetime.now().isoformat(timespec="seconds"),
    })
    _save_video_history(history)
    logger.info(f"[video_history] '{video_name}' marquée comme utilisée (cycle {history['cycle']})")


def _pick_random_video(videos_dir: str) -> str:
    """
    Sélectionne aléatoirement une vidéo non encore utilisée dans ce cycle.

    Comportement :
    - Charge video_history.json pour connaître les vidéos déjà utilisées
    - Filtre le pool pour n'offrir que les vidéos encore vierges
    - Si toutes les vidéos ont été utilisées → réinitialise l'historique
      (nouveau cycle) et repart du pool complet   → rotation automatique,
      aucune intervention manuelle requise
    - Enregistre la vidéo choisie dans l'historique AVANT de lancer le pipeline
    """
    vdir = Path(videos_dir)

    if not vdir.exists():
        raise FileNotFoundError(
            f"Dossier vidéos introuvable : {vdir.absolute()}\n"
            "Créer le dossier et y déposer des vidéos (.mp4, .mov, .webm)."
        )

    all_videos = [
        f for f in vdir.iterdir()
        if f.is_file() and f.suffix.lower() in VIDEO_EXTENSIONS
    ]

    if not all_videos:
        raise FileNotFoundError(
            f"Aucune vidéo trouvée dans {videos_dir}. "
            "Y déposer des fichiers .mp4 / .mov / .webm pour tester le pipeline."
        )

    history  = _load_video_history()
    used_set = {entry["name"] for entry in history.get("used", [])}

    unused = [f for f in all_videos if f.name not in used_set]

    if not unused:
        # Toutes les vidéos ont été utilisées → nouveau cycle
        new_cycle = history.get("cycle", 1) + 1
        logger.info(
            f"[video_history] Toutes les vidéos utilisées — "
            f"réinitialisation (cycle {new_cycle}). "
            f"{len(all_videos)} vidéos remises dans le pool."
        )
        history = {"cycle": new_cycle, "used": []}
        _save_video_history(history)
        unused = all_videos

    chosen = random.choice(unused)
    logger.info(
        f"Vidéo sélectionnée : {chosen.name} "
        f"({len(unused) - 1} restantes dans le cycle courant)"
    )

    # Marquer immédiatement comme utilisée (avant le pipeline)
    _mark_video_used(chosen.name)

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

    # Sauvegarder l'état intermédiaire avant Kling (recovery si erreur)
    _save_intermediate_state(madison_image_path, video_path, scene_json, step)

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
    return final_video_path, public_url, filename, caption, "reel", madison_image_path, video_path


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


# ================================================================
# Point d'entrée manuel (depuis Telegram /manualGeneration ou --workflow manual_video)
# ================================================================

def run_from_path(source_path: str, concept: dict | None = None) -> tuple:
    """
    Workflow vidéo depuis un chemin de vidéo fourni manuellement.

    Identique à run() mais avec une vidéo source spécifiée explicitement
    (plutôt que piocher aléatoirement dans data/videos/).

    Args:
        source_path : chemin local vers la vidéo source
        concept     : dict optionnel (pour contexte calendrier)

    Returns:
        (local_video_path, public_url, filename, caption, video_type, madison_image_path, source_video_path)

    Raises:
        FileNotFoundError : si source_path n'existe pas
        ValueError        : si Gemini ou Kling échoue
    """
    if not os.path.exists(source_path):
        raise FileNotFoundError(f"Vidéo source introuvable : {source_path}")

    log_section(__name__, "WORKFLOW VIDÉO MANUEL (run_from_path)")
    step = get_current_calendar_step()

    log_step(__name__, 1, TOTAL_STEPS, f"Vidéo source : {source_path}")

    log_step(__name__, 2, TOTAL_STEPS, "Extraction frame intelligente")
    frame_path = extract_best_frame(source_path)

    log_step(__name__, 3, TOTAL_STEPS, "Détection personnage (Gemini Vision)")
    from pinterest_scraper import _detect_person_in_image
    has_person = _detect_person_in_image(frame_path)
    logger.info(f"Personnage détecté : {has_person}")

    if has_person:
        return _run_person_branch(source_path, frame_path, step)
    else:
        return _run_ambiance_branch(source_path, frame_path, step)
