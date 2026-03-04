"""
image_generator.py — Génération et analyse d'images via Gemini API (google-genai SDK).

Responsabilités :
- generate_image()       : génère une image depuis un prompt + image de référence
- image_to_json()        : analyse une image Pinterest → JSON de scène (PROMPT_IMAGE_TO_JSON)
- cleanup_nginx()        : supprime l'image du dossier nginx après publication Instagram

SDK : google-genai (remplace google-generativeai déprécié)
https://github.com/googleapis/python-genai

Formats supportés pour l'image de référence : .jpg, .jpeg, .png, .webp, .avif
⚠️  Les noms de modèles Gemini dans config.py sont des previews.
    Vérifier leur disponibilité sur https://ai.google.dev/models
"""

import io
import json
import os
import shutil
from datetime import datetime
from pathlib import Path

from google import genai
from google.genai import types
from PIL import Image

from config import (
    GEMINI_API_KEY,
    GEMINI_MODEL_IMAGE_PRO2,
    GEMINI_MODEL_VISION,
    INFLUENCER_REF_IMAGE_PATH,
    NGINX_BASE_URL,
    NGINX_OUTPUT_DIR,
    OUTPUTS_DIR,
)
from logger import get_logger

logger = get_logger(__name__)

# Extensions supportées pour l'image de référence
REF_IMAGE_EXTS = [".jpg", ".jpeg", ".png", ".webp", ".avif"]

# Client Gemini singleton
_client = genai.Client(api_key=GEMINI_API_KEY)


# ================================================================
# Helpers internes
# ================================================================

def _find_ref_image_path() -> str:
    """
    Cherche l'image de référence en testant les extensions supportées.
    Supporte : .jpg, .jpeg, .png, .webp, .avif
    """
    base = Path(INFLUENCER_REF_IMAGE_PATH)
    stem = base.with_suffix("") if base.suffix.lower() in REF_IMAGE_EXTS else base

    for ext in REF_IMAGE_EXTS:
        candidate = str(stem) + ext
        if os.path.exists(candidate):
            logger.debug(f"Image de référence trouvée : {candidate}")
            return candidate

    tried = [str(stem) + ext for ext in REF_IMAGE_EXTS]
    msg = (
        f"\n{'='*60}\n"
        f"  ERREUR : Image de référence introuvable\n"
        f"  Chemins testés :\n"
        + "\n".join(f"    - {p}" for p in tried)
        + f"\n\n"
        f"  Pour créer l'image de référence :\n"
        f"  1. Utilisez PROMPTS/CHARACTER_CONSISTENCY_FACE_REFERENCE_IMAGE.md\n"
        f"     ou la variable PROMPT_REF_SHEET dans prompts.py\n"
        f"  2. Placez le résultat dans data/ref_{{prenom_lowercase}}.[jpg|png|webp|avif]\n"
        f"{'='*60}\n"
    )
    logger.error(msg)
    raise FileNotFoundError(msg)


def _pil_to_bytes(img: Image.Image) -> tuple[bytes, str]:
    """
    Convertit une image PIL en bytes JPEG pour l'API Gemini.
    Gère les images avec transparence (RGBA/P) → conversion RGB automatique.
    """
    if img.mode in ("RGBA", "P", "LA"):
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=95)
    return buf.getvalue(), "image/jpeg"


def _load_ref_image_part() -> types.Part:
    """
    Charge l'image de référence et la prépare comme Part Gemini.
    """
    path = _find_ref_image_path()
    # .copy() libère immédiatement le handle fichier (Windows — évite WinError 32)
    img  = Image.open(path).copy()
    data, mime = _pil_to_bytes(img)
    logger.debug(f"Image de référence chargée : {path} ({len(data)} bytes)")
    return types.Part.from_bytes(data=data, mime_type=mime)


def _save_image_from_response(response, filename: str) -> str:
    """
    Extrait les bytes d'image de la réponse Gemini (nouveau SDK) et sauve dans outputs/.
    Retourne le chemin local.
    """
    os.makedirs(OUTPUTS_DIR, exist_ok=True)
    local_path = os.path.join(OUTPUTS_DIR, filename)

    for part in response.candidates[0].content.parts:
        if hasattr(part, "inline_data") and part.inline_data:
            with open(local_path, "wb") as f:
                f.write(part.inline_data.data)
            logger.debug(f"Image sauvegardée : {local_path} ({len(part.inline_data.data)} bytes)")
            return local_path

    raise ValueError(
        "Gemini n'a retourné aucune image dans la réponse. "
        "Vérifier le modèle et les paramètres de génération.\n"
        f"Réponse : {response}"
    )


def _copy_to_nginx(local_path: str, filename: str) -> str:
    """
    Copie l'image dans le dossier nginx pour l'exposer publiquement.
    Retourne l'URL publique.
    """
    try:
        os.makedirs(NGINX_OUTPUT_DIR, exist_ok=True)
        nginx_path = os.path.join(NGINX_OUTPUT_DIR, filename)
        shutil.copy(local_path, nginx_path)
        public_url = f"{NGINX_BASE_URL}/{filename}"
        logger.info(f"Image copiée vers nginx : {nginx_path} → {public_url}")
        return public_url
    except Exception as e:
        logger.error(f"Erreur copie nginx : {e}")
        logger.warning("Publication Instagram impossible sans URL publique. Vérifier la config nginx.")
        raise


# ================================================================
# Génération d'image (prompt + référence)
# ================================================================

def generate_image(prompt_text: str) -> tuple[str, str]:
    """
    Génère une image via Gemini à partir d'un prompt texte et de l'image de référence.

    Args:
        prompt_text : prompt décrivant la scène (construit par workflow_pinterest)

    Returns:
        (chemin_local, url_publique_nginx)
        L'URL publique est indispensable pour l'API Meta Instagram.

    Raises:
        FileNotFoundError : si l'image de référence est absente
        ValueError        : si Gemini ne retourne pas d'image
    """
    logger.info(f"Génération image — modèle : {GEMINI_MODEL_IMAGE_PRO2}")
    logger.debug(f"Prompt (extrait) : {prompt_text[:200]}...")

    ref_part = _load_ref_image_part()

    response = _client.models.generate_content(
        model=GEMINI_MODEL_IMAGE_PRO2,
        contents=[prompt_text, ref_part],
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE", "TEXT"],
        ),
    )
    logger.debug("Réponse Gemini reçue — extraction image...")

    filename   = f"pending_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
    local_path = _save_image_from_response(response, filename)

    public_url = _copy_to_nginx(local_path, filename)

    logger.info(f"Image générée : {local_path}")
    return local_path, public_url


# ================================================================
# Image → JSON de scène (workflow Pinterest)
# ================================================================

def image_to_json(image_path: str) -> dict:
    """
    Analyse une image via Gemini Vision et retourne un JSON de scène.
    Utilise PROMPT_IMAGE_TO_JSON — ne décrit PAS les traits du visage.

    Args:
        image_path : chemin local de l'image Pinterest (outputs/pinterest_*.jpg)

    Returns:
        dict : JSON de scène complet
    """
    from prompts import PROMPT_IMAGE_TO_JSON

    logger.info(f"Analyse image → JSON : {image_path}")
    logger.info(f"Modèle vision : {GEMINI_MODEL_VISION}")

    # .copy() libère le handle fichier immédiatement (Windows — évite WinError 32)
    img  = Image.open(image_path).copy()
    data, mime = _pil_to_bytes(img)
    img_part   = types.Part.from_bytes(data=data, mime_type=mime)

    response = _client.models.generate_content(
        model=GEMINI_MODEL_VISION,
        contents=[PROMPT_IMAGE_TO_JSON, img_part],
    )
    raw_text = response.text.strip()

    logger.debug(f"Réponse brute Gemini (extrait) : {raw_text[:300]}...")

    # Nettoyer les éventuels backticks de markdown
    if raw_text.startswith("```"):
        raw_text = raw_text.split("```")[1]
        if raw_text.startswith("json"):
            raw_text = raw_text[4:]
        raw_text = raw_text.strip()

    try:
        scene_json = json.loads(raw_text)
        logger.info("JSON de scène parsé avec succès")
        return scene_json
    except json.JSONDecodeError as e:
        logger.error(f"JSON invalide retourné par Gemini : {e}\nContenu : {raw_text[:500]}")
        raise ValueError(f"Gemini a retourné un JSON invalide : {e}") from e


# ================================================================
# Nettoyage nginx
# ================================================================

def cleanup_nginx(filename: str) -> None:
    """
    Supprime l'image du dossier nginx après publication Instagram.
    Appelé par instagram_publisher après confirmation de publication.
    """
    nginx_path = os.path.join(NGINX_OUTPUT_DIR, filename)
    if os.path.exists(nginx_path):
        os.remove(nginx_path)
        logger.info(f"Image supprimée du dossier nginx : {nginx_path}")
    else:
        logger.warning(f"Image nginx introuvable lors du nettoyage : {nginx_path}")
