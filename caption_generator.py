"""
caption_generator.py — Génération de captions via Claude API (Anthropic).

Responsabilités :
- generate_caption()       : génère une caption Instagram depuis un prompt formaté
- validate_custom_input()  : valide une saisie libre /run (V2) contre le style influenceuse
"""

import json

import anthropic

from config import ANTHROPIC_API_KEY, ANTHROPIC_MODEL, ANTHROPIC_MODEL_FALLBACKS
from logger import get_logger

logger = get_logger(__name__)

# Initialisation du client Anthropic (une seule fois)
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Modèles Claude utilisés (fallback auto si un modèle n'existe plus)
CLAUDE_MODELS = [ANTHROPIC_MODEL, *ANTHROPIC_MODEL_FALLBACKS]


def _dedupe_models(models: list[str]) -> list[str]:
    seen = set()
    unique = []
    for model in models:
        if model not in seen:
            seen.add(model)
            unique.append(model)
    return unique


CLAUDE_MODELS = _dedupe_models(CLAUDE_MODELS)
CLAUDE_MODEL = CLAUDE_MODELS[0] if CLAUDE_MODELS else "claude-sonnet-4-0"


def _is_model_not_found_error(exc: Exception) -> bool:
    text = str(exc).lower()
    return "not_found_error" in text or ("404" in text and "model" in text)


def _create_message_with_fallback(max_tokens: int, messages: list[dict]):
    """Essaie les modèles Anthropic dans l'ordre jusqu'à un succès."""
    last_exc = None
    for model_name in CLAUDE_MODELS:
        try:
            message = client.messages.create(
                model=model_name,
                max_tokens=max_tokens,
                messages=messages,
            )
            return message, model_name
        except anthropic.APIError as e:
            if _is_model_not_found_error(e):
                logger.warning(f"Modèle Claude indisponible : {model_name} — fallback")
                last_exc = e
                continue
            raise

    if last_exc:
        raise last_exc
    raise RuntimeError("Aucun modèle Claude configuré")


# ================================================================
# Génération caption
# ================================================================

def generate_caption(caption_prompt: str, max_tokens: int = 500) -> str:
    """
    Génère une caption Instagram via Claude.

    Args:
        caption_prompt : prompt formaté par concept_generator.build_caption_prompt()
        max_tokens     : limite de tokens de la réponse

    Returns:
        Caption générée, nettoyée (strip)
    """
    logger.info(f"Génération caption — modèles candidats : {', '.join(CLAUDE_MODELS)}")
    logger.debug(f"Prompt caption (extrait) : {caption_prompt[:200]}...")

    message, model_used = _create_message_with_fallback(
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": caption_prompt}],
    )

    caption = message.content[0].text.strip()
    logger.info(f"Caption générée avec : {model_used}")
    logger.info(f"Caption générée ({len(caption)} chars) : {caption[:100]}...")
    return caption


def generate_caption_from_scene(
    scene_json: dict,
    content_type: str = "feed",
    max_tokens: int = 300,
) -> str:
    """
    Génère une caption Instagram contextualisée depuis un JSON de scène.
    
    Cette fonction produit des captions courtes, engageantes et aguicheuses
    qui reflètent le contenu réel de l'image/vidéo (location, tenue, mood...).
    
    Args:
        scene_json   : JSON de scène généré par image_to_json() ou build_madison_json()
        content_type : type de contenu ("feed", "story", "reel")
        max_tokens   : limite de tokens de la réponse
        
    Returns:
        Caption générée, nettoyée (strip)
    """
    from config import INFLUENCER_NAME
    from prompts import PROMPT_CAPTION_CONTEXTUALIZED
    
    logger.info(f"Génération caption contextualisée — type : {content_type}")
    
    # Extraire les informations clés du JSON de scène
    scene_desc = _build_scene_description(scene_json)
    
    prompt = PROMPT_CAPTION_CONTEXTUALIZED.format(
        influencer_name=INFLUENCER_NAME,
        content_type=content_type,
        scene_description=scene_desc,
    )
    
    logger.debug(f"Prompt caption contextualisée :\n{prompt[:300]}...")
    
    message, model_used = _create_message_with_fallback(
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": prompt}],
    )
    
    caption = message.content[0].text.strip()
    logger.info(f"Caption contextualisée générée avec : {model_used}")
    logger.info(f"Caption contextualisée générée ({len(caption)} chars) : {caption[:100]}...")
    return caption


def _build_scene_description(scene_json: dict) -> str:
    """
    Extrait les informations pertinentes du JSON de scène pour le prompt caption.
    
    Args:
        scene_json : JSON de scène complet
        
    Returns:
        Description textuelle de la scène (location, tenue, mood, lighting...)
    """
    def _as_dict(val) -> dict:
        """Retourne val si c'est un dict, {} sinon (robustesse si Gemini retourne une string)."""
        return val if isinstance(val, dict) else {}

    parts = []
    
    # Context global
    global_ctx = _as_dict(scene_json.get("global_context"))
    if scene_desc := global_ctx.get("scene_description"):
        parts.append(f"Scene: {scene_desc}")
    if time_of_day := global_ctx.get("time_of_day"):
        parts.append(f"Time: {time_of_day}")
    if weather := global_ctx.get("weather_atmosphere"):
        parts.append(f"Atmosphere: {weather}")
    
    # Lighting
    lighting = _as_dict(global_ctx.get("lighting"))
    if light_quality := lighting.get("quality"):
        parts.append(f"Lighting: {light_quality}")
    
    # Pose & expression
    subject = _as_dict(scene_json.get("subject"))
    pose = _as_dict(subject.get("pose"))
    if body_pos := pose.get("body_position"):
        parts.append(f"Pose: {body_pos}")
    if expression := pose.get("expression_mood"):
        parts.append(f"Mood: {expression}")
    
    # Tenue
    clothing = _as_dict(subject.get("clothing"))
    if outfit := clothing.get("outfit_description"):
        parts.append(f"Outfit: {outfit}")
    elif style := clothing.get("style"):
        parts.append(f"Style: {style}")
    
    # Location (environment)
    environment = _as_dict(scene_json.get("environment"))
    if location := environment.get("location_type"):
        parts.append(f"Location: {location}")
    
    return " | ".join(parts) if parts else "Casual lifestyle content"


# ================================================================
# Validation saisie libre /run (V2)
# ================================================================

def validate_custom_input(field: str, value: str, influencer_style: str) -> dict:
    """
    Valide une saisie libre soumise via la commande Telegram /run (V2).

    Interroge Claude pour vérifier que la valeur est cohérente avec le style
    de l'influenceuse, a une longueur raisonnable et n'est pas hors-sujet.

    Args:
        field             : champ concerné (ex: "location", "outfit", "mood")
        value             : valeur saisie par l'utilisateur
        influencer_style  : style de l'influenceuse (depuis config.INFLUENCER_STYLE)

    Returns:
        dict {"valid": bool, "reason": str}
    """
    from prompts import PROMPT_VALIDATE_INPUT

    logger.debug(f"Validation saisie libre — champ='{field}', valeur='{value}'")

    prompt = PROMPT_VALIDATE_INPUT.format(
        influencer_style=influencer_style,
        field=field,
        value=value,
    )

    try:
        message, _ = _create_message_with_fallback(
            max_tokens=150,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text.strip()

        # Nettoyer les éventuels backticks markdown
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
            raw = raw.strip()

        result = json.loads(raw)
        logger.debug(f"Résultat validation : {result}")
        return result

    except json.JSONDecodeError:
        logger.warning(f"Réponse Claude non-parsable pour validate_custom_input : {raw!r}")
        return {"valid": False, "reason": "Impossible de valider la saisie."}
    except Exception as e:
        logger.error(f"Erreur validate_custom_input : {e}")
        return {"valid": False, "reason": "Erreur lors de la validation."}
