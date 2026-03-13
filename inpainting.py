"""
inpainting.py — Remplacement de personnage via inpainting Gemini natif.

Workflow :
  1. Segmentation automatique du personnage (rembg, en local, gratuit)
     → masque binaire blanc/noir : blanc = personnage, noir = reste
  2. Appel Gemini native inpainting :
     image source + masque + ref_face + ref_body + prompt
  3. Sauvegarde locale + exposition nginx

Usage :
  from inpainting import replace_person

  local_path, public_url, filename = replace_person(
      source_image_path = "outputs/scraped_xyz.jpg",
      influencer_name   = "madison",
      ref_face_path     = "data/ref_madison_face.jpg",
      ref_body_path     = "data/ref_madison_body.jpg",
  )

SDK : google-genai (même SDK que image_generator.py)
⚠️  Modèle inpainting : GEMINI_MODEL_INPAINTING dans config.py
    Vérifier la disponibilité : https://ai.google.dev/gemini-api/docs/image-generation
"""

import io
import os
import shutil
from datetime import datetime
from pathlib import Path

from google import genai
from google.genai import types
from PIL import Image

from config import (
    GEMINI_API_KEY,
    GEMINI_MODEL_INPAINTING,
    NGINX_BASE_URL,
    NGINX_OUTPUT_DIR,
    OUTPUTS_DIR,
)
from logger import get_logger

logger = get_logger(__name__)

# Client Gemini singleton (partagé avec image_generator.py si importé — pas de double init)
_client = genai.Client(api_key=GEMINI_API_KEY)

# ================================================================
# Template prompt inpainting
# ================================================================

INPAINTING_PROMPT_TEMPLATE = """Replace the person in the masked area with {influencer_name}.
Use the attached face reference sheet for exact facial features, skin tone and likeness.
Use the attached body reference sheet for exact body proportions and silhouette.
Preserve all lighting, shadows, background, color grading and environment from the original image.
Match the original photo's perspective and atmosphere exactly.
The result must look like {influencer_name} was always in this scene.
Photorealistic, natural, seamless integration. 4K quality.
Do not modify anything outside the masked area."""


# ================================================================
# Helpers internes
# ================================================================

def _pil_to_bytes(img: Image.Image, format: str = "JPEG") -> tuple[bytes, str]:
    """Convertit une image PIL en bytes pour l'API Gemini."""
    if img.mode in ("RGBA", "P", "LA") and format == "JPEG":
        img = img.convert("RGB")
    buf = io.BytesIO()
    img.save(buf, format=format, quality=95)
    mime = "image/jpeg" if format == "JPEG" else "image/png"
    return buf.getvalue(), mime


def _generate_person_mask(image_path: str) -> Image.Image:
    """
    Génère un masque binaire du personnage présent dans l'image.
    Zone blanche = personnage (à remplacer), zone noire = arrière-plan (à préserver).

    Utilise rembg en local — aucun appel API.
    """
    try:
        from rembg import remove
    except ImportError:
        raise ImportError(
            "rembg n'est pas installé. Lance : pip install rembg\n"
            "Ou : pip install -r requirements.txt"
        )

    logger.info(f"Segmentation personnage — rembg : {image_path}")
    with open(image_path, "rb") as f:
        img_bytes = f.read()

    # only_mask=True retourne une image L (niveaux de gris) : blanc = sujet détouré
    mask_rgba = remove(img_bytes, only_mask=True)

    # remove() retourne des bytes PNG — on charge en PIL
    mask = Image.open(io.BytesIO(mask_rgba)).convert("L")

    # Binarisation nette : seuil = 128 → blanc pur ou noir pur
    mask = mask.point(lambda px: 255 if px > 128 else 0, "L")

    logger.debug(f"Masque généré : {mask.size} px, mode={mask.mode}")
    return mask


def _load_image_as_part(path: str) -> types.Part:
    """Charge une image depuis un chemin et la prépare comme Part Gemini."""
    if not os.path.exists(path):
        raise FileNotFoundError(
            f"Image introuvable : {path}\n"
            f"Vérifie que le fichier existe avant de lancer le workflow inpainting."
        )
    img = Image.open(path).copy()
    data, mime = _pil_to_bytes(img)
    return types.Part.from_bytes(data=data, mime_type=mime)


def _mask_as_part(mask: Image.Image) -> types.Part:
    """Convertit un masque PIL en Part Gemini (PNG pour conserver la précision binaire)."""
    data, mime = _pil_to_bytes(mask, format="PNG")
    return types.Part.from_bytes(data=data, mime_type=mime)


def _save_image_from_response(response, filename: str) -> str:
    """
    Extrait les bytes d'image de la réponse Gemini et sauvegarde dans outputs/.
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
        "Gemini n'a retourné aucune image dans la réponse inpainting.\n"
        "Vérifier que le modèle supporte bien l'édition d'image (inpainting natif).\n"
        f"Modèle utilisé : {GEMINI_MODEL_INPAINTING}\n"
        f"Réponse complète : {response}"
    )


def _copy_to_nginx(local_path: str, filename: str) -> str:
    """Copie l'image dans le dossier nginx et retourne l'URL publique."""
    try:
        os.makedirs(NGINX_OUTPUT_DIR, exist_ok=True)
        nginx_path = os.path.join(NGINX_OUTPUT_DIR, filename)
        public_url = f"{NGINX_BASE_URL}/{filename}"
        if os.path.abspath(local_path) == os.path.abspath(nginx_path):
            logger.info(f"Image déjà dans le dossier nginx : {nginx_path}")
            return public_url
        shutil.copy(local_path, nginx_path)
        logger.info(f"Image copiée vers nginx : {nginx_path} → {public_url}")
        return public_url
    except Exception as e:
        logger.error(f"Erreur copie nginx : {e}")
        raise


# ================================================================
# Fonction principale
# ================================================================

def replace_person(
    source_image_path: str,
    influencer_name: str,
    ref_face_path: str,
    ref_body_path: str,
) -> tuple[str, str, str]:
    """
    Remplace le personnage d'une image par l'influenceuse via inpainting Gemini.

    Args:
        source_image_path : chemin vers l'image Pinterest source (avec personnage)
        influencer_name   : nom de l'influenceuse (pour le prompt)
        ref_face_path     : chemin vers la référence visage (3 angles)
        ref_body_path     : chemin vers la référence corps (3 panels)

    Returns:
        (local_path, public_url, filename)
        local_path  : chemin local de l'image générée dans outputs/
        public_url  : URL publique nginx (pour l'API Meta Instagram)
        filename    : nom du fichier

    Raises:
        FileNotFoundError : si une image de référence est introuvable
        ValueError        : si Gemini ne retourne pas d'image
    """
    logger.info(f"Inpainting — source : {source_image_path} | influenceuse : {influencer_name}")
    logger.info(f"Modèle Gemini inpainting : {GEMINI_MODEL_INPAINTING}")

    # ── Étape 1 : Segmentation du personnage ─────────────────────
    logger.info("Étape 1/3 : Segmentation personnage (rembg)...")
    mask = _generate_person_mask(source_image_path)

    # ── Étape 2 : Préparation des parts Gemini ────────────────────
    logger.info("Étape 2/3 : Chargement des références et appel Gemini...")
    prompt = INPAINTING_PROMPT_TEMPLATE.format(influencer_name=influencer_name)

    source_part   = _load_image_as_part(source_image_path)
    mask_part     = _mask_as_part(mask)
    ref_face_part = _load_image_as_part(ref_face_path)
    ref_body_part = _load_image_as_part(ref_body_path)

    logger.debug(f"Prompt inpainting : {prompt[:200]}...")

    # Ordre de passage : source → masque → ref_face → ref_body → prompt texte
    # Gemini interprète le masque blanc comme la zone à éditer
    response = _client.models.generate_content(
        model=GEMINI_MODEL_INPAINTING,
        contents=[source_part, mask_part, ref_face_part, ref_body_part, prompt],
        config=types.GenerateContentConfig(
            response_modalities=["IMAGE", "TEXT"],
            response_mime_type="image/jpeg",
        ),
    )

    # ── Étape 3 : Sauvegarde + nginx ─────────────────────────────
    logger.info("Étape 3/3 : Sauvegarde et exposition nginx...")
    filename   = f"inpainted_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
    local_path = _save_image_from_response(response, filename)
    public_url = _copy_to_nginx(local_path, filename)

    logger.info(f"Inpainting terminé : {local_path}")
    return local_path, public_url, filename
