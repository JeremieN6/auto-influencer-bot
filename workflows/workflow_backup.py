"""
workflows/workflow_backup.py — Workflow de secours — Activation manuelle uniquement.

================================================================
DORMANT — NE PAS INCLURE DANS LE CRON NI LE PIPELINE AUTO
================================================================

DESCRIPTION :
  Workflow de secours pour répliquer l'ambiance exacte d'une image source
  trouvée manuellement (Pinterest, Twitter, Instagram...).
  Utile pour tester une image spécifique ou débloquer le pipeline manuellement.

FLUX :
  [Image source fournie manuellement (chemin local ou URL)]
        ↓
  [PROMPT_IMAGE_TO_JSON → Gemini Vision → JSON de la scène]
        ↓
  [JSON enrichi avec variables influenceuse si nécessaire]
        ↓
  [PROMPT_JSON_TO_IMAGE + référence influenceuse → Gemini → image finale]
        ↓
  [Rejoint le pipeline standard]
        → Retourne (local_path, public_url, filename)
        → Appeler ensuite telegram_bot.send_for_validation() manuellement

USAGE (ligne de commande) :
  python -c "
  from workflows.workflow_backup import run
  result = run('chemin/vers/image_source.jpg')
  print(result)
  "

USAGE (import depuis un script) :
  from workflows.workflow_backup import run
  local_path, public_url, filename = run("images/inspiration.jpg")

NOTE : n'affecte pas history.json ni le calendrier éditorial automatique.
"""

import json
import os

from image_generator import generate_image, image_to_json
from logger import get_logger, log_section, log_step
from prompts import PROMPT_JSON_TO_IMAGE

logger = get_logger(__name__)


def run(source_path: str, enrich_with_concept: bool = False) -> tuple[str, str, str, dict]:
    """
    Exécute le workflow backup depuis une image source manuelle.

    Args:
        source_path         : chemin local vers l'image source
        enrich_with_concept : si True, tire un concept aléatoire et l'applique
                              sur les champs outfit/pose du JSON généré.
                              Utile pour modifier la mise en scène tout en gardant
                              l'ambiance de l'image source.

    Returns:
        (local_path, public_url, filename, scene_json)
        - scene_json : JSON de scène extrait (pour génération caption contextualisée)

    Raises:
        FileNotFoundError : si source_path n'existe pas
        ValueError        : si Gemini échoue
    """
    log_section(__name__, "WORKFLOW BACKUP (manuel)")
    logger.info(f"Image source : {source_path}")

    if not os.path.exists(source_path):
        raise FileNotFoundError(
            f"Image source introuvable : {source_path}\n"
            "Fournir un chemin local valide vers l'image d'inspiration."
        )

    # ── Étape 1 : Image source → JSON de scène ─────────────────
    log_step(__name__, 1, 2, "Analyse image source → JSON de scène (Gemini Vision)")
    scene_json = image_to_json(source_path)
    logger.info(f"JSON extrait — clés : {list(scene_json.keys())}")

    # ── Enrichissement optionnel avec un concept aléatoire ──────
    if enrich_with_concept:
        from concept_generator import generate_concept
        concept = generate_concept(persist=False)
        logger.info(f"Enrichissement avec concept aléatoire : {concept}")
        # Injecter les overrides dans le JSON de scène
        if "subject" in scene_json:
            subj = scene_json["subject"]
            if "clothing" in subj:
                subj["clothing"]["outfit_description"] = concept.get("outfit", subj["clothing"].get("outfit_description", ""))
            if "pose" in subj:
                subj["pose"]["body_position"] = concept.get("pose", subj["pose"].get("body_position", ""))
                subj["pose"]["expression_mood"] = concept.get("mood", subj["pose"].get("expression_mood", ""))
        if "global_context" in scene_json:
            ctx = scene_json["global_context"]
            ctx["lighting"] = ctx.get("lighting", {})
            ctx["lighting"]["quality"] = concept.get("lighting", ctx["lighting"].get("quality", ""))

    # ── Étape 2 : JSON + référence → Image finale ───────────────
    log_step(__name__, 2, 2, "Génération image finale (Gemini + référence influenceuse)")

    prompt_text = PROMPT_JSON_TO_IMAGE.format(
        scene_json=json.dumps(scene_json, indent=2, ensure_ascii=False)
    )
    local_path, public_url = generate_image(prompt_text)
    filename = os.path.basename(local_path)

    logger.info(f"=== Workflow Backup terminé : {local_path} ===")
    logger.info(
        "Pour envoyer sur Telegram :\n"
        f"  from telegram_bot import send_for_validation\n"
        f"  import asyncio\n"
        f"  asyncio.run(send_for_validation('{local_path}', 'votre caption'))"
    )

    return local_path, public_url, filename, scene_json
