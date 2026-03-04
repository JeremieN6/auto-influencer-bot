"""
caption_generator.py — Génération de captions via Claude API (Anthropic).

Responsabilités :
- generate_caption()       : génère une caption Instagram depuis un prompt formaté
- validate_custom_input()  : valide une saisie libre /run (V2) contre le style influenceuse
"""

import json

import anthropic

from config import ANTHROPIC_API_KEY
from logger import get_logger

logger = get_logger(__name__)

# Initialisation du client Anthropic (une seule fois)
client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# Modèle Claude utilisé
CLAUDE_MODEL = "claude-sonnet-4-20250514"


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
    logger.info(f"Génération caption — modèle : {CLAUDE_MODEL}")
    logger.debug(f"Prompt caption (extrait) : {caption_prompt[:200]}...")

    message = client.messages.create(
        model=CLAUDE_MODEL,
        max_tokens=max_tokens,
        messages=[{"role": "user", "content": caption_prompt}],
    )

    caption = message.content[0].text.strip()
    logger.info(f"Caption générée ({len(caption)} chars) : {caption[:100]}...")
    return caption


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
        message = client.messages.create(
            model=CLAUDE_MODEL,
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
