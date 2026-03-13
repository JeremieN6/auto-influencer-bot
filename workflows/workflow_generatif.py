"""
workflows/workflow_generatif.py — Workflow V2 : génération 100% IA sans Pinterest.

DESCRIPTION :
  Pas de scraping externe. Le concept est tiré depuis variables.json par concept_generator.
  image_generator.build_madison_json() assemble un JSON de scène complet depuis le template
  MADISON_JSON_TEMPLATE (prompts.py), puis Gemini génère l'image finale avec la référence
  de l'influenceuse.

FLUX :
  [variables.json — tirage aléatoire mood + location + outfit + pose + lighting]
        ↓
  [image_generator.build_madison_json() — mappe les valeurs vers MADISON_JSON_TEMPLATE]
        ↓
  [image_generator.generate_image_from_concept() — PROMPT_JSON_TO_IMAGE + ref image → Gemini]
        ↓
  [Sauvegarde dans outputs/ + copie vers nginx]
        ↓
  [Retourne (local_path, public_url, filename)]

AVANTAGES vs workflow_pinterest.py :
  - Pas de dépendance Pinterest (plus stable, pas de blocage)
  - Scènes 100% originales, cohérentes avec le style Madison
  - JSON de scène entièrement contrôlé (pas d'image source aléatoire)

Appelé par : main.run_pipeline(workflow="generatif")
"""

from concept_generator import get_current_calendar_step
from image_generator import generate_image_from_concept
from logger import get_logger, log_section, log_step

logger = get_logger(__name__)

TOTAL_STEPS = 2


def run(concept: dict) -> tuple[str, str, str]:
    """
    Exécute le workflow génératif V2.

    Args:
        concept : dict généré par concept_generator.generate_concept()
                  {location, outfit, pose, mood, lighting, generated_at}

    Returns:
        (local_path, public_url, filename)

    Raises:
        FileNotFoundError : si l'image de référence influenceuse est absente
        ValueError        : si Gemini échoue à retourner une image
    """
    log_section("workflow_generatif", "DÉMARRAGE WORKFLOW GÉNÉRATIF V2")

    # ── Étape 1 : Assemblage JSON de scène ───────────────────────
    log_step("workflow_generatif", 1, TOTAL_STEPS, "Assemblage JSON + appel Gemini")
    logger.info(
        f"Concept : {concept['mood']} | {concept['outfit']} | "
        f"{concept['location']} | {concept['lighting']}"
    )

    calendar_step = get_current_calendar_step()
    logger.info(f"Format : {calendar_step['format']} | Type : {calendar_step['type']}")

    # ── Étape 2 : Génération image ────────────────────────────────
    log_step("workflow_generatif", 2, TOTAL_STEPS, "Génération image Gemini")
    local_path, public_url, filename = generate_image_from_concept(concept, calendar_step)

    logger.info(f"Image finale : {local_path}")
    logger.info(f"URL publique : {public_url}")

    log_section("workflow_generatif", "WORKFLOW GÉNÉRATIF TERMINÉ")
    return local_path, public_url, filename

    # ================================================================
    # TODO V2 — Implémentation à venir
    # ================================================================

    # ── Étape 1 : Construire les paramètres pour le prompt ────────
    # parameters_block = json.dumps({
    #     "location": concept["location"],
    #     "outfit":   concept["outfit"],
    #     "pose":     concept["pose"],
    #     "mood":     concept["mood"],
    #     "lighting": concept["lighting"],
    # }, indent=2)
    #
    # ── Étape 2 : Claude génère le JSON de scène ─────────────────
    # from prompts import PROMPT_GENERATIVE_SCENE
    # from caption_generator import client as claude_client, CLAUDE_MODEL
    # import json
    #
    # prompt = PROMPT_GENERATIVE_SCENE.format(parameters=parameters_block)
    # response = claude_client.messages.create(
    #     model=CLAUDE_MODEL,
    #     max_tokens=1000,
    #     messages=[{"role": "user", "content": prompt}]
    # )
    # raw = response.content[0].text.strip()
    # scene_json = json.loads(raw)
    # logger.info(f"JSON de scène généré par Claude : {list(scene_json.keys())}")
    #
    # ── Étape 3 : JSON + référence → image finale ─────────────────
    # from image_generator import generate_image
    # from prompts import PROMPT_JSON_TO_IMAGE
    # import os
    #
    # prompt_text = PROMPT_JSON_TO_IMAGE.format(
    #     scene_json=json.dumps(scene_json, indent=2, ensure_ascii=False)
    # )
    # local_path, public_url = generate_image(prompt_text)
    # filename = os.path.basename(local_path)
    #
    # return local_path, public_url, filename
