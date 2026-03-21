"""
image_generator.py — Génération et analyse d'images via Gemini API (google-genai SDK).

Responsabilités :
- generate_image()              : génère une image depuis un prompt + image de référence
- image_to_json()               : analyse une image Pinterest → JSON de scène (PROMPT_IMAGE_TO_JSON)
- build_madison_json()          : assemble le JSON final depuis MADISON_JSON_TEMPLATE + concept
- generate_image_from_concept() : point d'entrée workflow_generatif — JSON → Gemini → image
- cleanup_nginx()               : supprime l'image du dossier nginx après publication Instagram

SDK : google-genai (remplace google-generativeai déprécié)
https://github.com/googleapis/python-genai

Formats supportés pour l'image de référence : .jpg, .jpeg, .png, .webp, .avif
⚠️  Les noms de modèles Gemini dans config.py sont des previews.
    Vérifier leur disponibilité sur https://ai.google.dev/models
"""

import copy
import io
import json
import os
import random
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

    # Défense : candidates absents ou vides
    if not response.candidates:
        raise ValueError(
            "Gemini n'a retourné aucun candidat dans la réponse.\n"
            f"Réponse brute : {response}"
        )

    candidate = response.candidates[0]

    # Logger la raison d'arrêt si disponible (SAFETY, RECITATION, etc.)
    finish_reason = getattr(candidate, "finish_reason", None)
    if finish_reason and str(finish_reason) not in ("FinishReason.STOP", "STOP", "1"):
        logger.warning(f"Gemini finish_reason inattendu : {finish_reason}")

    # Défense : content ou parts absents / None
    content = getattr(candidate, "content", None)
    parts   = getattr(content, "parts", None) if content else None

    if not parts:
        raise ValueError(
            f"Gemini n'a retourné aucune partie dans la réponse (finish_reason={finish_reason}). "
            "Le modèle a peut-être refusé de générer l'image (filtre de sécurité ou contenu). "
            f"Réponse complète : {response}"
        )

    for part in parts:
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
        public_url = f"{NGINX_BASE_URL}/{filename}"
        # Si outputs/ et NGINX_OUTPUT_DIR pointent vers le même dossier, pas besoin de copier
        if os.path.abspath(local_path) == os.path.abspath(nginx_path):
            logger.info(f"Image déjà dans le dossier nginx : {nginx_path} → {public_url}")
            return public_url
        shutil.copy(local_path, nginx_path)
        logger.info(f"Image copiée vers nginx : {nginx_path} → {public_url}")
        return public_url
    except Exception as e:
        logger.error(f"Erreur copie nginx : {e}")
        logger.warning("Publication Instagram impossible sans URL publique. Vérifier la config nginx.")
        raise


# ================================================================
# Génération d'image (prompt + référence)
# ================================================================

def generate_image(prompt_text: str, max_retries: int = 3) -> tuple[str, str]:
    """
    Génère une image via Gemini à partir d'un prompt texte et de l'image de référence.

    Args:
        prompt_text : prompt décrivant la scène (construit par workflow_pinterest)
        max_retries : nombre de tentatives si Gemini ne retourne pas d'image (défaut : 3)

    Returns:
        (chemin_local, url_publique_nginx)
        L'URL publique est indispensable pour l'API Meta Instagram.

    Raises:
        FileNotFoundError : si l'image de référence est absente
        ValueError        : si Gemini ne retourne pas d'image après toutes les tentatives
    """
    logger.info(f"Génération image — modèle : {GEMINI_MODEL_IMAGE_PRO2}")
    logger.debug(f"Prompt (extrait) : {prompt_text[:200]}...")

    ref_part      = _load_ref_image_part()

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            if attempt > 1:
                wait = random.uniform(3, 8)
                logger.warning(f"Tentative {attempt}/{max_retries} — pause {wait:.1f}s avant retry...")
                import time; time.sleep(wait)

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

        except ValueError as e:
            last_error = e
            logger.error(f"Tentative {attempt}/{max_retries} échouée : {e}")

    raise ValueError(
        f"Gemini n'a pas retourné d'image après {max_retries} tentatives. "
        f"Dernière erreur : {last_error}"
    )


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
    raw_text = (response.text or "").strip()

    if not raw_text:
        raise ValueError(
            f"Gemini ({GEMINI_MODEL_VISION}) a retourné une réponse vide pour image_to_json.\n"
            "Vérifier que GEMINI_MODEL_VISION est bien un modèle texte+vision "
            "(ex: gemini-2.0-flash) et non un modèle de génération d'image."
        )

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
# inject_madison_body — cohérence corporelle inter-générations
# ================================================================

def inject_madison_body(scene_json: dict) -> dict:
    """
    Injecte le bloc corps fixe de Madison dans un JSON de scène extrait de Pinterest.

    Le JSON retourné par image_to_json() décrit la scène (décor, lumière, tenue)
    mais ne contient aucune description corporelle. Cette fonction garantit que
    Madison aura toujours les mêmes proportions à chaque génération, quelle que
    soit l'image Pinterest source.

    Args:
        scene_json : dict retourné par image_to_json()

    Returns:
        dict : scene_json enrichi avec subject.body et subject.face de Madison
    """
    import copy
    enriched = copy.deepcopy(scene_json)

    # Récupérer le vêtement du haut depuis le JSON Pinterest si disponible,
    # pour contextualiser "stretching the {top_garment}" avec le vêtement réel.
    top_garment = "top garment"
    try:
        wardrobe = enriched.get("subject", {}).get("wardrobe", {})
        top_raw = wardrobe.get("top", "") or wardrobe.get("top_garment", "")
        if top_raw:
            # Extraire les premiers mots (ex: "White triangle bikini top, ..." → "white triangle bikini top")
            top_garment = top_raw.split(",")[0].strip().lower()
    except Exception:
        pass  # fallback silencieux sur "top garment"

    # Bloc corps fixe — formules anatomiques gagnantes
    madison_body = {
        "physique": (
            f"Voluptuous hourglass figure with significantly enlarged breasts that fill the {top_garment}. "
            "Narrow defined waist leading to wider hips, full and rounded high-set glutes with extreme waist-to-hip ratio. "
            "Muscle tone visible in core and legs."
        ),
        "anatomy": {
            "shoulders": "Defined, proportional.",
            "waist": "Narrow, defined waist.",
            "hips": "Wide hips with full, high and rounded glutes. Prominent gluteal muscles, extreme waist-to-hip ratio, shapely posterior chain. Not muscular — curvaceous and natural.",
            "breasts": (
                f"Extremely large, very full breasts causing cleavage and stretching the {top_garment}."
            ),
            "waist_to_hip_ratio": "Pronounced hourglass.",
        },
        "skin": {
            "tone": "Warm light beige, natural sun-kissed California glow.",
            "texture": "Visible pores, natural skin texture, not airbrushed, raw photo.",
            "details": "No tattoos.",
        },
    }

    # Bloc visage fixe de Madison
    madison_face = {
        "hair": "Dirty blonde.",
        "eyes": "Light blue-grey.",
        "features": "Soft facial features, natural makeup.",
        "skin": "Clear, freckle-free, warm beige tone.",
    }

    # Garantir que subject existe
    if "subject" not in enriched:
        enriched["subject"] = {}

    # Injecter — on écrase body et face car ils doivent toujours décrire Madison,
    # pas le personnage de l'image Pinterest source
    enriched["subject"]["description"] = (
        "A young blonde woman named Madison, californian aesthetic, mid-20s."
    )
    enriched["subject"]["body"] = madison_body
    enriched["subject"]["face"] = madison_face

    logger.debug(
        f"inject_madison_body — top_garment détecté : '{top_garment}' | "
        f"clés subject : {list(enriched['subject'].keys())}"
    )

    return enriched


# ================================================================
# Workflow génératif V2 — build JSON + génération image
# ================================================================

# Lookup tables : mappe les valeurs simples de variables.json
# vers les champs détaillés attendus par MADISON_JSON_TEMPLATE.

_OUTFIT_MAP: dict[str, dict] = {
    "white crop top + high waist jeans": {
        "top_garment":       "white crop top",
        "top_description":   "White fitted crop top, stretched tight over her large bust",
        "bottom_description": "High-waist white denim jeans, fitted at the waist",
    },
    "black bikini": {
        "top_garment":       "black bikini top",
        "top_description":   "Black triangle bikini top, stretched over very large breasts, causing visible cleavage",
        "bottom_description": "Matching black string bikini bottoms",
    },
    "beige linen dress": {
        "top_garment":       "beige linen dress",
        "top_description":   "Loose beige linen dress, draped over her curves, slightly fitted at the bust",
        "bottom_description": "Same beige linen dress flowing below the hips",
    },
    "oversized grey hoodie": {
        "top_garment":       "oversized grey hoodie",
        "top_description":   "Oversized grey hoodie worn casually, slightly off one shoulder",
        "bottom_description": "Hoodie covers upper thighs, no visible bottom",
    },
    "satin slip dress nude": {
        "top_garment":       "nude satin slip dress",
        "top_description":   "Thin nude satin slip dress, clinging to her curves and full bust",
        "bottom_description": "Same satin slip dress flowing past the hips",
    },
    "sport bra + leggings": {
        "top_garment":       "sports bra",
        "top_description":   "Black sports bra, stretched over full natural bust, minimal coverage",
        "bottom_description": "High-waist black leggings, fitted and form-hugging",
    },
    "blazer only no shirt": {
        "top_garment":       "blazer",
        "top_description":   "Oversized blazer worn directly on skin with no shirt, slightly open in front",
        "bottom_description": "Blazer barely covers hips, minimal visible bottom",
    },
    "floral summer dress": {
        "top_garment":       "floral summer dress",
        "top_description":   "Floral print summer dress, fitted at the bust, light flowing fabric",
        "bottom_description": "Same floral dress flowing loosely past the hips",
    },
    "white button-down shirt half open": {
        "top_garment":       "white button-down shirt",
        "top_description":   "White button-down shirt, half open, slightly off one shoulder, knotted at waist",
        "bottom_description": "Shirt drapes over hips, no visible separate bottom",
    },
    "long cardigan + cycling shorts": {
        "top_garment":       "long cardigan",
        "top_description":   "Long oversized cardigan, open front, cozy texture",
        "bottom_description": "Black cycling shorts, form-fitting and high-waisted",
    },
}

_LIGHTING_MAP: dict[str, dict] = {
    "golden hour warm backlight": {
        "type":    "Golden hour sunlight from behind the subject",
        "quality": "Warm golden-orange, directional backlight",
        "shadows": "Long soft shadows toward camera, warm rim light glow",
    },
    "soft diffused indoor": {
        "type":    "Soft indoor ambient light",
        "quality": "Diffused, even, no harsh shadows",
        "shadows": "Very soft, almost shadowless",
    },
    "bright natural window light": {
        "type":    "Natural daylight from large window",
        "quality": "Bright, crisp, cool-white directional light",
        "shadows": "Clean, defined directional shadows from window",
    },
    "warm sunset side light": {
        "type":    "Warm sunset golden light from the side",
        "quality": "Warm amber-orange, strong directional side light",
        "shadows": "Deep warm side shadows, dramatic and moody",
    },
    "cool morning light": {
        "type":    "Soft cool morning daylight",
        "quality": "Soft, cool-blue, diffused morning light",
        "shadows": "Gentle, minimal, cool-toned shadows",
    },
    "candlelight intimate": {
        "type":    "Warm flickering candlelight",
        "quality": "Intimate, warm orange-amber glow",
        "shadows": "Deep moody shadows with warm highlights",
    },
    "overcast outdoor soft": {
        "type":    "Overcast outdoor daylight",
        "quality": "Flat, even, milky-white diffused light",
        "shadows": "Near shadowless, very soft and even",
    },
    "harsh midday sun editorial": {
        "type":    "Harsh direct midday sunlight",
        "quality": "High contrast, bright white, editorial quality",
        "shadows": "Sharp, deep, hard shadows",
    },
}

_LOCATION_BACKGROUND_MAP: dict[str, str] = {
    "bedroom mirror":           "Bedroom seen through a large full-length mirror, unmade white linen bedding, window with soft natural light, white walls",
    "beach at sunset":          "Sandy beach at golden hour, warm ocean horizon, gentle waves, pink-orange sky",
    "cafe terrace Paris":       "Parisian café terrace, small round tables, rattan chairs, cobblestone street, soft afternoon light",
    "poolside luxury":          "Edge of a luxury pool, clear turquoise water, white marble tiles, sun deck chairs in background",
    "rooftop city view":        "Urban rooftop, open city skyline stretching to the horizon, late afternoon sky",
    "bathroom vanity":          "Modern bathroom with large vanity mirror, marble countertop, warm vanity strip lighting",
    "hotel room morning":       "Minimalist hotel room, soft white bed linen, large window with morning light flooding in",
    "forest path golden hour":  "Forest path lined with tall trees, golden light filtering through leaves, dappled light patterns on the ground",
    "kitchen counter":          "Modern white kitchen, marble countertop, natural light from a nearby window",
    "balcony with city skyline": "Open balcony with metal railing, city skyline visible below, open blue sky above",
    "linen sofa living room":   "Cozy living room, natural linen sofa, books and plants on shelves, warm ambient lamplight",
    "outdoor terrace white stone": "Mediterranean-style outdoor terrace, white stone walls, terracotta pots, warm blue-sky backdrop",
}

_POSE_CAMERA_MAP: dict[str, dict] = {
    "mirror selfie arm raised": {
        "pose_description": "Standing facing a full-length mirror, arm raised holding smartphone to take a selfie, body slightly angled",
        "camera_type":      "Smartphone camera mirror selfie",
        "lens_type":        "Wide-angle smartphone lens",
        "camera_angle":     "Eye-level from the mirror reflection",
    },
    "over shoulder looking back": {
        "pose_description": "Walking away from camera, head turned to look back over shoulder with natural ease",
        "camera_type":      "DSLR candid capture",
        "lens_type":        "50mm portrait lens",
        "camera_angle":     "Eye-level from behind, slight three-quarter angle",
    },
    "sitting legs crossed candid": {
        "pose_description": "Sitting casually on a surface, legs loosely crossed, relaxed candid posture",
        "camera_type":      "DSLR candid portrait",
        "lens_type":        "35mm lens",
        "camera_angle":     "Eye-level, candid",
    },
    "standing profile arms relaxed": {
        "pose_description": "Standing in profile view, arms relaxed at sides, neutral confident stance",
        "camera_type":      "DSLR editorial portrait",
        "lens_type":        "85mm portrait lens",
        "camera_angle":     "Profile view, eye-level",
    },
    "lying on bed reading": {
        "pose_description": "Lying on bed on stomach, propped comfortably on elbows, looking at phone or reading",
        "camera_type":      "DSLR angled shot from above",
        "lens_type":        "35mm lens",
        "camera_angle":     "Slightly high angle, looking down",
    },
    "walking looking down": {
        "pose_description": "Walking casually, looking down at phone, candid street-style movement",
        "camera_type":      "DSLR candid street photography",
        "lens_type":        "35mm lens",
        "camera_angle":     "Eye-level, candid from the front",
    },
    "leaning against wall": {
        "pose_description": "Leaning relaxed against a wall, arms loosely at sides, confident casual stance",
        "camera_type":      "DSLR portrait",
        "lens_type":        "50mm lens",
        "camera_angle":     "Eye-level, straight on",
    },
    "head tilted soft smile": {
        "pose_description": "Standing or sitting, head gently tilted to one side, warm natural expression",
        "camera_type":      "DSLR close portrait",
        "lens_type":        "85mm portrait lens",
        "camera_angle":     "Slightly above eye-level",
    },
    "sitting on floor hugging knees": {
        "pose_description": "Sitting on floor, knees pulled up to chest, arms wrapped around knees, intimate pose",
        "camera_type":      "DSLR intimate portrait",
        "lens_type":        "50mm lens",
        "camera_angle":     "Eye-level from slightly above",
    },
    "standing in doorway backlit": {
        "pose_description": "Standing in a doorway, body backlit by light pouring in from behind, slightly silhouetted with details visible",
        "camera_type":      "DSLR backlit portrait",
        "lens_type":        "35mm lens",
        "camera_angle":     "Eye-level, straight-on from the front",
    },
}

_MOOD_EXPRESSION_MAP: dict[str, str] = {
    "playful smile":               "playful smile, bright and expressive eyes",
    "sultry soft look":            "sultry expression, soft half-smile, relaxed gaze",
    "candid laugh eyes closed":    "candid natural laugh, eyes closed with joy",
    "serene gaze distance":        "serene, gaze drifting into the distance",
    "confident direct eye contact": "confident, direct eye contact with the camera",
    "contemplative looking away":  "contemplative, eyes softly looking slightly off-frame",
    "warm natural smile":          "warm, genuine natural smile",
    "relaxed eyes half-closed":    "relaxed expression, eyes softly half-closed",
    "focused reading or scrolling": "focused, eyes looking downward, absorbed in thought",
}

_HAIR_STYLES = [
    "loose beach waves",
    "messy bun",
    "straight and down",
    "high ponytail",
    "low casual bun",
    "half-up half-down waves",
]

_ACCESSORIES = [
    "Small gold necklace",
    "Silver hoop earrings",
    "Dainty gold pendant necklace",
    "Gold bracelet and small earrings",
    "None",
    "Small gold stud earrings",
]


def build_madison_json(concept: dict, calendar_step: dict) -> str:
    """
    Assemble le JSON final pour Gemini depuis MADISON_JSON_TEMPLATE + concept.

    Args:
        concept       : dict produit par concept_generator.generate_concept()
                        {location, outfit, pose, mood, lighting, generated_at}
        calendar_step : étape courante depuis calendar.json {format, ...}

    Returns:
        str : JSON string prêt à être injecté dans PROMPT_JSON_TO_IMAGE
    """
    from prompts import MADISON_JSON_TEMPLATE

    outfit_str   = concept["outfit"].lower()
    lighting_str = concept["lighting"].lower()
    pose_str     = concept["pose"].lower()
    mood_str     = concept["mood"].lower()
    location_str = concept["location"].lower()

    outfit   = _OUTFIT_MAP.get(outfit_str, {
        "top_garment":        outfit_str,
        "top_description":    f"{concept['outfit']}, fitted over the bust",
        "bottom_description": "Matching bottom",
    })
    lighting = _LIGHTING_MAP.get(lighting_str, {
        "type":    concept["lighting"],
        "quality": "Natural, photorealistic",
        "shadows": "Natural shadows",
    })
    pose     = _POSE_CAMERA_MAP.get(pose_str, {
        "pose_description": concept["pose"],
        "camera_type":      "DSLR portrait",
        "lens_type":        "50mm lens",
        "camera_angle":     "Eye-level",
    })
    expression        = _MOOD_EXPRESSION_MAP.get(mood_str, concept["mood"])
    background        = _LOCATION_BACKGROUND_MAP.get(location_str, f"{concept['location']}, natural light")
    aspect_ratio      = "9:16" if calendar_step.get("format") == "story" else "4:5"

    replacements = {
        "{top_garment}":            outfit["top_garment"],
        "{top_description}":        outfit["top_description"],
        "{bottom_description}":     outfit["bottom_description"],
        "{accessories}":            random.choice(_ACCESSORIES),
        "{hair_style}":             random.choice(_HAIR_STYLES),
        "{expression}":             expression,
        "{pose_description}":       pose["pose_description"],
        "{location}":               concept["location"],
        "{background_description}": background,
        "{lighting_type}":          lighting["type"],
        "{lighting_quality}":       lighting["quality"],
        "{shadow_description}":     lighting["shadows"],
        "{camera_type}":            pose["camera_type"],
        "{lens_type}":              pose["lens_type"],
        "{camera_angle}":           pose["camera_angle"],
        "{aspect_ratio}":           aspect_ratio,
    }

    template_str = json.dumps(copy.deepcopy(MADISON_JSON_TEMPLATE), ensure_ascii=False)
    for key, value in replacements.items():
        template_str = template_str.replace(key, value)

    logger.debug(f"JSON scène assemblé ({aspect_ratio}) — outfit={outfit_str} | lighting={lighting_str}")
    return template_str


def generate_image_from_concept(concept: dict, calendar_step: dict, max_retries: int = 3) -> tuple[str, str, str]:
    """
    Point d'entrée principal pour workflow_generatif.py.
    Assemble le JSON, formate le prompt, appelle Gemini, retourne les chemins.

    Args:
        concept       : dict de concept_generator.generate_concept()
        calendar_step : étape courante de calendar.json
        max_retries   : nombre de tentatives si Gemini ne retourne pas d'image (défaut : 3)

    Returns:
        (local_path, public_url, filename, wildcard_used)
          - wildcard_used : description de l'élément surprise injecté

    Raises:
        ValueError : si Gemini ne retourne pas d'image après toutes les tentatives
    """
    from prompts import PROMPT_JSON_TO_IMAGE

    logger.info(f"Génération image depuis concept — modèle : {GEMINI_MODEL_IMAGE_PRO2}")

    json_scene, wildcard = build_madison_json(concept, calendar_step)
    final_prompt = PROMPT_JSON_TO_IMAGE.format(scene_json=json_scene)
    ref_part     = _load_ref_image_part()

    last_error: Exception | None = None
    for attempt in range(1, max_retries + 1):
        try:
            if attempt > 1:
                wait = random.uniform(3, 8)
                logger.warning(f"Tentative {attempt}/{max_retries} — pause {wait:.1f}s avant retry...")
                import time; time.sleep(wait)

            response = _client.models.generate_content(
                model=GEMINI_MODEL_IMAGE_PRO2,
                contents=[final_prompt, ref_part],
                config=types.GenerateContentConfig(
                    response_modalities=["IMAGE", "TEXT"],
                ),
            )

            filename   = f"generatif_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
            local_path = _save_image_from_response(response, filename)
            public_url = _copy_to_nginx(local_path, filename)

            logger.info(f"Image générée (workflow génératif) : {local_path}")
            return local_path, public_url, filename, wildcard

        except ValueError as e:
            last_error = e
            logger.error(f"Tentative {attempt}/{max_retries} échouée : {e}")

    raise ValueError(
        f"Gemini n'a pas retourné d'image après {max_retries} tentatives. "
        f"Dernière erreur : {last_error}"
    )


# ================================================================
# Workflow génératif V2 — build JSON + génération image
# ================================================================

# Lookup tables : mappe les valeurs simples de variables.json
# vers les champs détaillés attendus par MADISON_JSON_TEMPLATE.

_OUTFIT_MAP: dict[str, dict] = {
    "white crop top + high waist jeans": {
        "top_garment":        "white crop top",
        "top_description":    "White fitted crop top, stretched tight over her large bust",
        "bottom_description": "High-waist white denim jeans, fitted at the waist",
    },
    "black bikini": {
        "top_garment":        "black bikini top",
        "top_description":    "Black triangle bikini top, stretched over very large breasts, causing visible cleavage",
        "bottom_description": "Matching black string bikini bottoms",
    },
    "beige linen dress": {
        "top_garment":        "beige linen dress",
        "top_description":    "Loose beige linen dress, draped over her curves, slightly fitted at the bust",
        "bottom_description": "Same beige linen dress flowing below the hips",
    },
    "oversized grey hoodie": {
        "top_garment":        "oversized grey hoodie",
        "top_description":    "Oversized grey hoodie worn casually, slightly off one shoulder",
        "bottom_description": "Hoodie covers upper thighs, no visible bottom",
    },
    "satin slip dress nude": {
        "top_garment":        "nude satin slip dress",
        "top_description":    "Thin nude satin slip dress, clinging to her curves and full bust",
        "bottom_description": "Same satin slip dress flowing past the hips",
    },
    "sport bra + leggings": {
        "top_garment":        "sports bra",
        "top_description":    "Black sports bra, stretched over full natural bust, minimal coverage",
        "bottom_description": "High-waist black leggings, fitted and form-hugging",
    },
    "blazer only no shirt": {
        "top_garment":        "blazer",
        "top_description":    "Oversized blazer worn directly on skin with no shirt, slightly open in front",
        "bottom_description": "Blazer barely covers hips, minimal visible bottom",
    },
    "floral summer dress": {
        "top_garment":        "floral summer dress",
        "top_description":    "Floral print summer dress, fitted at the bust, light flowing fabric",
        "bottom_description": "Same floral dress flowing loosely past the hips",
    },
    "white button-down shirt half open": {
        "top_garment":        "white button-down shirt",
        "top_description":    "White button-down shirt, half open, slightly off one shoulder, knotted at waist",
        "bottom_description": "Shirt drapes over hips, no visible separate bottom",
    },
    "long cardigan + cycling shorts": {
        "top_garment":        "long cardigan",
        "top_description":    "Long oversized cardigan, open front, cozy texture",
        "bottom_description": "Black cycling shorts, form-fitting and high-waisted",
    },
}

_LIGHTING_MAP: dict[str, dict] = {
    "golden hour warm backlight": {
        "type":    "Golden hour sunlight from behind the subject",
        "quality": "Warm golden-orange, directional backlight",
        "shadows": "Long soft shadows toward camera, warm rim light glow",
    },
    "soft diffused indoor": {
        "type":    "Soft indoor ambient light",
        "quality": "Diffused, even, no harsh shadows",
        "shadows": "Very soft, almost shadowless",
    },
    "bright natural window light": {
        "type":    "Natural daylight from large window",
        "quality": "Bright, crisp, cool-white directional light",
        "shadows": "Clean, defined directional shadows from window",
    },
    "warm sunset side light": {
        "type":    "Warm sunset golden light from the side",
        "quality": "Warm amber-orange, strong directional side light",
        "shadows": "Deep warm side shadows, dramatic and moody",
    },
    "cool morning light": {
        "type":    "Soft cool morning daylight",
        "quality": "Soft, cool-blue, diffused morning light",
        "shadows": "Gentle, minimal, cool-toned shadows",
    },
    "candlelight intimate": {
        "type":    "Warm flickering candlelight",
        "quality": "Intimate, warm orange-amber glow",
        "shadows": "Deep moody shadows with warm highlights",
    },
    "overcast outdoor soft": {
        "type":    "Overcast outdoor daylight",
        "quality": "Flat, even, milky-white diffused light",
        "shadows": "Near shadowless, very soft and even",
    },
    "harsh midday sun editorial": {
        "type":    "Harsh direct midday sunlight",
        "quality": "High contrast, bright white, editorial quality",
        "shadows": "Sharp, deep, hard shadows",
    },
}

_LOCATION_BACKGROUND_MAP: dict[str, str] = {
    "bedroom mirror":              "Bedroom seen through a large full-length mirror, unmade white linen bedding, window with soft natural light, white walls",
    "beach at sunset":             "Sandy beach at golden hour, warm ocean horizon, gentle waves, pink-orange sky",
    "cafe terrace paris":          "Parisian café terrace, small round tables, rattan chairs, cobblestone street, soft afternoon light",
    "poolside luxury":             "Edge of a luxury pool, clear turquoise water, white marble tiles, sun deck chairs in background",
    "rooftop city view":           "Urban rooftop, open city skyline stretching to the horizon, late afternoon sky",
    "bathroom vanity":             "Modern bathroom with large vanity mirror, marble countertop, warm vanity strip lighting",
    "hotel room morning":          "Minimalist hotel room, soft white bed linen, large window with morning light flooding in",
    "forest path golden hour":     "Forest path lined with tall trees, golden light filtering through leaves, dappled light patterns on the ground",
    "kitchen counter":             "Modern white kitchen, marble countertop, natural light from a nearby window",
    "balcony with city skyline":   "Open balcony with metal railing, city skyline visible below, open blue sky above",
    "linen sofa living room":      "Cozy living room, natural linen sofa, books and plants on shelves, warm ambient lamplight",
    "outdoor terrace white stone": "Mediterranean-style outdoor terrace, white stone walls, terracotta pots, warm blue-sky backdrop",
}

_POSE_CAMERA_MAP: dict[str, dict] = {
    "mirror selfie arm raised": {
        "pose_description": "Standing facing a full-length mirror, arm raised holding smartphone to take a selfie, body slightly angled",
        "camera_type":      "Smartphone camera mirror selfie",
        "lens_type":        "Wide-angle smartphone lens",
        "camera_angle":     "Eye-level from the mirror reflection",
    },
    "over shoulder looking back": {
        "pose_description": "Walking away from camera, head turned to look back over shoulder with natural ease",
        "camera_type":      "DSLR candid capture",
        "lens_type":        "50mm portrait lens",
        "camera_angle":     "Eye-level from behind, slight three-quarter angle",
    },
    "sitting legs crossed candid": {
        "pose_description": "Sitting casually on a surface, legs loosely crossed, relaxed candid posture",
        "camera_type":      "DSLR candid portrait",
        "lens_type":        "35mm lens",
        "camera_angle":     "Eye-level, candid",
    },
    "standing profile arms relaxed": {
        "pose_description": "Standing in profile view, arms relaxed at sides, neutral confident stance",
        "camera_type":      "DSLR editorial portrait",
        "lens_type":        "85mm portrait lens",
        "camera_angle":     "Profile view, eye-level",
    },
    "lying on bed reading": {
        "pose_description": "Lying on bed on stomach, propped comfortably on elbows, looking at phone or reading",
        "camera_type":      "DSLR angled shot from above",
        "lens_type":        "35mm lens",
        "camera_angle":     "Slightly high angle, looking down",
    },
    "walking looking down": {
        "pose_description": "Walking casually, looking down at phone, candid street-style movement",
        "camera_type":      "DSLR candid street photography",
        "lens_type":        "35mm lens",
        "camera_angle":     "Eye-level, candid from the front",
    },
    "leaning against wall": {
        "pose_description": "Leaning relaxed against a wall, arms loosely at sides, confident casual stance",
        "camera_type":      "DSLR portrait",
        "lens_type":        "50mm lens",
        "camera_angle":     "Eye-level, straight on",
    },
    "head tilted soft smile": {
        "pose_description": "Standing or sitting, head gently tilted to one side, warm natural expression",
        "camera_type":      "DSLR close portrait",
        "lens_type":        "85mm portrait lens",
        "camera_angle":     "Slightly above eye-level",
    },
    "sitting on floor hugging knees": {
        "pose_description": "Sitting on floor, knees pulled up to chest, arms wrapped around knees, intimate pose",
        "camera_type":      "DSLR intimate portrait",
        "lens_type":        "50mm lens",
        "camera_angle":     "Eye-level from slightly above",
    },
    "standing in doorway backlit": {
        "pose_description": "Standing in a doorway, body backlit by light pouring in from behind, slightly silhouetted with details visible",
        "camera_type":      "DSLR backlit portrait",
        "lens_type":        "35mm lens",
        "camera_angle":     "Eye-level, straight-on from the front",
    },
}

_MOOD_EXPRESSION_MAP: dict[str, str] = {
    "playful smile":                "playful smile, bright and expressive eyes",
    "sultry soft look":             "sultry expression, soft half-smile, relaxed gaze",
    "candid laugh eyes closed":     "candid natural laugh, eyes closed with joy",
    "serene gaze distance":         "serene, gaze drifting into the distance",
    "confident direct eye contact": "confident, direct eye contact with the camera",
    "contemplative looking away":   "contemplative, eyes softly looking slightly off-frame",
    "warm natural smile":           "warm, genuine natural smile",
    "relaxed eyes half-closed":     "relaxed expression, eyes softly half-closed",
    "focused reading or scrolling": "focused, eyes looking downward, absorbed in thought",
}

_HAIR_STYLES = [
    "loose beach waves",
    "messy bun",
    "straight and down",
    "high ponytail",
    "low casual bun",
    "half-up half-down waves",
]

_ACCESSORIES = [
    "Small gold necklace",
    "Silver hoop earrings",
    "Dainty gold pendant necklace",
    "Gold bracelet and small earrings",
    "None",
    "Small gold stud earrings",
]

# ================================================================
# Éléments surprise — injectés aléatoirement dans le workflow
# génératif pour garantir l'unicité de chaque génération.
# Catégories : props nearby, atmosphérique, background, détails
# portés, environnement. ~60 entrées pour minimiser les répétitions.
# ================================================================
_WILDCARD_ELEMENTS: list[str] = [
    # Props / objets à proximité
    "A steaming ceramic coffee cup placed just out of reach on a nearby surface, tiny wisps of steam rising",
    "An open paperback novel left face-down on the surface beside her",
    "A half-eaten buttery croissant on a small white plate in the foreground",
    "A loose bunch of fresh white peonies resting casually against a nearby surface",
    "A vintage 35mm film camera sitting beside her, strap loosely coiled",
    "A soft-serve ice cream cone with a single melting drip running down the side",
    "A champagne flute with slowly rising bubbles placed on the nearby surface",
    "A small terracotta pot with a succulent in the corner of the frame",
    "An open leather journal with a fountain pen resting across the page",
    "A glossy fashion magazine rolled casually on the surface",
    "Scattered dried pressed lavender stems across the foreground surface",
    "A mason jar full of iced coffee with condensation droplets running down the glass",
    "A tiny glass bottle of perfume catching and refracting the light",
    "A softly glowing open laptop just visible at the edge of the scene",
    "A strip of four Polaroid photos left casually on the surface",
    "A pair of oversized cat-eye sunglasses left folded on the table",
    "A half-open pastel macaron box in the corner of the frame",
    "A small scattered trail of rose petals on the surface in front",
    "A single tall white pillar candle burning softly in the background, soft halo of light around it",
    "A vintage copper kettle in the corner, slightly steaming",
    "A half-filled glass of sparkling water with a lemon slice on the rim",
    "A short stack of art books with a velvet bookmark ribbon hanging out",
    "An open jewelry box with a necklace chain spilling gracefully over the edge",
    # Atmosphérique / effets de lumière
    "A golden lens flare streak crossing the corner of the frame from a light source",
    "Floating white flower petals drifting across the foreground in soft motion blur",
    "Soft warm bokeh from out-of-focus fairy string lights in the background",
    "A single white feather floating gently through the air in the midground",
    "Fine pollen or dust particles catching a beam of sunlight in visible golden suspension",
    "A rainbow prism stripe cast diagonally across the scene from a nearby glass object",
    "Several iridescent soap bubbles floating lazily near the foreground",
    "Caustic light ripples from a nearby water surface dancing on the wall behind her",
    "A narrow shaft of sunlight cutting dramatically through the scene with suspended dust motes",
    "Thin morning mist hovering just above the ground in the background",
    "Damp condensation patterns on a nearby glass surface reflecting the light softly",
    # Background moments
    "A stray tabby cat sitting calmly and watching from a soft-focus background",
    "A pigeon frozen mid-wing-flap in the blurred background",
    "A couple walking hand-in-hand far in the background, completely bokeh-blurred",
    "A small white sailboat drifting on the water visible in the distant background",
    "Two paper sky lanterns lifting slowly into the evening sky in the far background",
    "A street musician playing guitar, a blurred silhouette far in the background",
    "Autumn leaves spiralling down from out of frame",
    "A monarch butterfly hovering just at the edge of the composition",
    "A seagull frozen mid-flight against the sky in the background",
    "The very tips of distant fireworks blooming in the night sky far behind her",
    "A hot air balloon barely visible as a tiny colourful shape in the distant sky",
    "Bougainvillea petals falling from above, just entering the top of the frame",
    "A small lizard sunbathing on the stone wall directly behind her",
    "A vintage Vespa scooter parked just out of focus in the background",
    # Détails portés inattendus
    "A delicate golden butterfly clip tucked into her hair, not mentioned in the outfit",
    "A single wildflower stem tucked casually behind her ear",
    "A thin braided friendship bracelet barely visible on her wrist alongside the other accessories",
    "Sunglasses pushed up casually on top of her head as if forgotten there",
    "A tiny pressed-flower sticker on the back of her phone, just visible",
    # Environnement inattendu
    "Scattered photogenic confetti dots of different pastel colours catching the light around the scene",
    "A narrow rainbow arc visible in the pale background sky",
    "A surfer riding a small sparkling wave far at the horizon",
    "A child flying a bright red kite in the distant background sky",
    "Falling cherry blossom petals drifting gently through the scene",
    "A small red umbrella left propped open in the soft-focus background",
    "Fireflies beginning to glow at the edges of the frame in early twilight",
    "A single floating dandelion seed drifting slowly across the midground",
]


def build_madison_json(concept: dict, calendar_step: dict) -> tuple[str, str]:
    """
    Assemble le JSON final pour Gemini depuis MADISON_JSON_TEMPLATE + concept.

    Mappe les valeurs simples de variables.json vers les champs détaillés
    du template. Fallback sur la valeur brute si la clé est absente du map.
    Injecte un élément surprise aléatoire (wildcard) pour garantir que deux
    générations avec les mêmes paramètres produisent des images différentes.

    Args:
        concept       : dict produit par concept_generator.generate_concept()
                        {location, outfit, pose, mood, lighting, generated_at}
        calendar_step : étape courante depuis calendar.json {format, ...}

    Returns:
        (json_str, wildcard_used) :
          - json_str       : JSON string prêt à être injecté dans PROMPT_JSON_TO_IMAGE
          - wildcard_used  : description en clair de l'élément surprise choisi
    """
    from prompts import MADISON_JSON_TEMPLATE

    outfit_str   = concept["outfit"].lower()
    lighting_str = concept["lighting"].lower()
    pose_str     = concept["pose"].lower()
    mood_str     = concept["mood"].lower()
    location_str = concept["location"].lower()

    outfit   = _OUTFIT_MAP.get(outfit_str, {
        "top_garment":        outfit_str,
        "top_description":    f"{concept['outfit']}, fitted over the bust",
        "bottom_description": "Matching bottom",
    })
    lighting = _LIGHTING_MAP.get(lighting_str, {
        "type":    concept["lighting"],
        "quality": "Natural, photorealistic",
        "shadows": "Natural shadows",
    })
    pose     = _POSE_CAMERA_MAP.get(pose_str, {
        "pose_description": concept["pose"],
        "camera_type":      "DSLR portrait",
        "lens_type":        "50mm lens",
        "camera_angle":     "Eye-level",
    })
    expression   = _MOOD_EXPRESSION_MAP.get(mood_str, concept["mood"])
    background   = _LOCATION_BACKGROUND_MAP.get(location_str, f"{concept['location']}, natural light")
    aspect_ratio = "9:16" if calendar_step.get("format") == "story" else "4:5"

    replacements = {
        "{top_garment}":            outfit["top_garment"],
        "{top_description}":        outfit["top_description"],
        "{bottom_description}":     outfit["bottom_description"],
        "{accessories}":            random.choice(_ACCESSORIES),
        "{hair_style}":             random.choice(_HAIR_STYLES),
        "{expression}":             expression,
        "{pose_description}":       pose["pose_description"],
        "{location}":               concept["location"],
        "{background_description}": background,
        "{lighting_type}":          lighting["type"],
        "{lighting_quality}":       lighting["quality"],
        "{shadow_description}":     lighting["shadows"],
        "{camera_type}":            pose["camera_type"],
        "{lens_type}":              pose["lens_type"],
        "{camera_angle}":           pose["camera_angle"],
        "{aspect_ratio}":           aspect_ratio,
    }

    # ── Élément surprise ─────────────────────────────────────────
    wildcard = random.choice(_WILDCARD_ELEMENTS)

    template_copy = copy.deepcopy(MADISON_JSON_TEMPLATE)
    template_copy["scene"]["wildcard_scene_detail"] = wildcard

    template_str = json.dumps(template_copy, ensure_ascii=False)
    for key, value in replacements.items():
        template_str = template_str.replace(key, value)

    logger.info(f"🎲 Wildcard : {wildcard[:80]}...")
    logger.debug(f"JSON scène assemblé ({aspect_ratio}) — outfit={outfit_str} | lighting={lighting_str}")
    return template_str, wildcard


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
