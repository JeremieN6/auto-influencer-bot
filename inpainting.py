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
import time
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
from image_generator import ImageSafetyError, _is_transient_gemini_error, _sanitize_prompt_for_safety
from logger import get_logger

logger = get_logger(__name__)

# Client Gemini singleton (partagé avec image_generator.py si importé — pas de double init)
_client = genai.Client(api_key=GEMINI_API_KEY)

# ================================================================
# Template prompt inpainting
# ================================================================

INPAINTING_PROMPT_TEMPLATE = """You are performing image inpainting. Task: replace ONLY the person in the white masked region with {influencer_name}.

OUTPUT REQUIREMENT: A SINGLE seamless photo — same framing and composition as the source image.
Do NOT produce a reference sheet, a 3-panel sheet, or any multi-view composite. ONE photo only.

FACE — use the FIRST attached reference image for {influencer_name}'s exact facial features. Match 1:1.
BODY — use the SECOND attached reference image for {influencer_name}'s exact body proportions and silhouette.

PRESERVE UNCHANGED from the source photo:
• The same pose, body angle and gesture of the original subject
• The same outfit and clothing — exact same garments
• Full background, environment and all objects/props
• Lighting: direction, intensity and color temperature
• Shadows and reflections on the ground and surroundings
• Camera angle, depth of field and framing
• Color grading and overall atmosphere
• Every pixel OUTSIDE the masked area — completely untouched

The final image must look as if {influencer_name} was always naturally in this scene.
Photorealistic, seamless, 4K quality."""


INPAINTING_PROMPT_FALLBACK_TEMPLATE = """Replace only the person inside the white mask with {influencer_name}.

Return one single photorealistic photo, not a collage.
Preserve the original pose, outfit, camera angle, framing, lighting, background and objects.
Use the attached face reference to match {influencer_name}'s identity closely.
Keep the body natural and consistent with the original person's silhouette and clothing.
Do not change anything outside the masked region."""


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

    # Vérifications null-safe : candidates peut être None ou vide si le modèle
    # refuse la requête (safety filter) ou si le modèle ne supporte pas l'image.
    candidates = getattr(response, "candidates", None)
    if not candidates:
        raise ValueError(
            "Gemini a retourné une réponse sans candidates (requête bloquée ou modèle invalide).\n"
            f"Modèle utilisé : {GEMINI_MODEL_INPAINTING}\n"
            f"Réponse complète : {response}"
        )

    candidate = candidates[0]
    finish_reason = getattr(candidate, "finish_reason", None)
    finish_reason_str = str(finish_reason) if finish_reason else ""
    if finish_reason and finish_reason_str not in ("FinishReason.STOP", "STOP", "1"):
        logger.warning(f"Gemini finish_reason inattendu (inpainting) : {finish_reason}")

    content = getattr(candidate, "content", None)
    if content is None:
        raise ImageSafetyError(
            "Gemini candidate[0].content est None (refus safety ou erreur modèle).\n"
            f"Modèle utilisé : {GEMINI_MODEL_INPAINTING}\n"
            f"finish_reason : {finish_reason or 'unknown'}"
        )

    parts = getattr(content, "parts", None)
    if not parts:
        if "IMAGE_SAFETY" in finish_reason_str or "IMAGE_OTHER" in finish_reason_str:
            raise ImageSafetyError(
                "Gemini n'a pas généré d'image pour la tentative inpainting.\n"
                f"finish_reason : {finish_reason_str or 'unknown'}\n"
                f"Modèle utilisé : {GEMINI_MODEL_INPAINTING}\n"
                f"Réponse complète : {response}"
            )
        raise ValueError(
            "Gemini candidate[0].content.parts est vide — le modèle n'a pas généré d'image.\n"
            "Vérifier que le modèle supporte bien l'édition d'image (inpainting natif).\n"
            f"Modèle utilisé : {GEMINI_MODEL_INPAINTING}\n"
            f"Réponse complète : {response}"
        )

    for part in parts:
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


def _build_inpainting_attempts(prompt: str, influencer_name: str) -> list[tuple[str, bool, str]]:
    """Construit les variantes de prompt/fallback pour l'inpainting."""
    attempts: list[tuple[str, bool, str]] = []
    attempts.append((prompt, True, "prompt complet + refs visage/corps"))

    sanitized_prompt = _sanitize_prompt_for_safety(prompt)
    if sanitized_prompt != prompt:
        attempts.append((sanitized_prompt, True, "prompt sanitisé + refs visage/corps"))

    fallback_prompt = INPAINTING_PROMPT_FALLBACK_TEMPLATE.format(influencer_name=influencer_name)
    attempts.append((fallback_prompt, False, "prompt simplifié + ref visage seule"))
    return attempts


# ================================================================
# Fonction principale
# ================================================================

def replace_person(
    source_image_path: str,
    influencer_name: str,
    ref_face_path: str,
    ref_body_path: str,
    custom_prompt: str = "",
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
    base_prompt = (
        custom_prompt.strip()
        if custom_prompt.strip()
        else INPAINTING_PROMPT_TEMPLATE.format(influencer_name=influencer_name)
    )

    source_part   = _load_image_as_part(source_image_path)
    mask_part     = _mask_as_part(mask)
    ref_face_part = _load_image_as_part(ref_face_path)
    ref_body_part = _load_image_as_part(ref_body_path)

    attempts = _build_inpainting_attempts(base_prompt, influencer_name)
    last_error: Exception | None = None

    for attempt_index, (attempt_prompt, include_body_ref, label) in enumerate(attempts, start=1):
        try:
            if attempt_index > 1:
                wait_s = 2 * attempt_index
                logger.warning(
                    f"Inpainting retry {attempt_index}/{len(attempts)} — {label} — pause {wait_s}s"
                )
                time.sleep(wait_s)

            logger.info(
                f"Tentative inpainting {attempt_index}/{len(attempts)} — {label}"
            )
            logger.debug(f"Prompt inpainting : {attempt_prompt[:200]}...")

            parts = [source_part, mask_part, ref_face_part]
            if include_body_ref:
                parts.append(ref_body_part)
            parts.append(attempt_prompt)

            response = _client.models.generate_content(
                model=GEMINI_MODEL_INPAINTING,
                contents=parts,
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE", "TEXT"],
                ),
            )

            logger.info("Étape 3/3 : Sauvegarde et exposition nginx...")
            filename = f"inpainted_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
            local_path = _save_image_from_response(response, filename)
            public_url = _copy_to_nginx(local_path, filename)
            logger.info(f"Inpainting terminé : {local_path}")
            return local_path, public_url, filename

        except ImageSafetyError as e:
            last_error = e
            logger.warning(f"Tentative inpainting {attempt_index}/{len(attempts)} refusée : {e}")
            continue
        except Exception as e:
            last_error = e
            if _is_transient_gemini_error(e) and attempt_index < len(attempts):
                logger.warning(
                    f"Tentative inpainting {attempt_index}/{len(attempts)} — erreur transitoire Gemini : {e}"
                )
                continue
            logger.warning(
                f"Tentative inpainting {attempt_index}/{len(attempts)} échouée : {e}"
            )

    raise ValueError(
        "Inpainting Gemini a échoué après plusieurs tentatives locales.\n"
        "Fallbacks essayés : prompt complet, prompt sanitisé, puis prompt simplifié avec ref visage seule.\n"
        f"Modèle utilisé : {GEMINI_MODEL_INPAINTING}\n"
        f"Dernière erreur : {last_error}"
    )
