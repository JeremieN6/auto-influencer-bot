"""
workflows/workflow_generatif.py — Workflow V2 : génération 100% IA sans Pinterest.

================================================================
V2 — SCAFFOLD — NON IMPLÉMENTÉ
================================================================

DESCRIPTION :
  Claude remplace Pinterest : il lit variables.json et imagine la scène lui-même
  en générant un JSON de scène complet via PROMPT_GENERATIVE_SCENE.
  Plus aucune dépendance externe (pas de scraping).

FLUX V2 :
  [variables.json — tirage aléatoire de tous les paramètres]
        ↓
  [Claude API — PROMPT_GENERATIVE_SCENE — génère un JSON de scène complet]
        ↓
  [Gemini — PROMPT_JSON_TO_IMAGE + référence influenceuse → image finale]
        ↓
  [Claude API — PROMPT_CAPTION_TEMPLATE → caption + hashtags selon calendrier]
        ↓
  [Telegram — envoi pour validation humaine]
        ↓
  [Instagram — publication sur approbation /validate]
          (publication autonome activable en V2 quand décidé)

AVANTAGES V2 vs V1 :
  - Pas de dépendance Pinterest (plus stable, pas de blocage)
  - Scènes 100% originales, jamais vues sur internet
  - Cohérence parfaite avec le style de l'influenceuse

TODO V2 :
  1. Implémenter _generate_scene_json() via Claude (PROMPT_GENERATIVE_SCENE)
  2. Implémenter run() en appelant image_generator.generate_image()
  3. Tester la qualité des scènes générées vs Pinterest
  4. Activer en changeant workflow="generatif" dans main.py
"""

from logger import get_logger

logger = get_logger(__name__)


def run(concept: dict) -> tuple[str, str, str]:
    """
    Exécute le workflow génératif V2.

    Args:
        concept : dict généré par concept_generator.generate_concept()

    Returns:
        (local_path, public_url, filename)

    Raises:
        NotImplementedError : V2 non encore implémenté
    """
    raise NotImplementedError(
        "Workflow génératif V2 non encore implémenté. "
        "Utiliser workflow='pinterest' pour la V1. "
        "Voir les TODO dans workflows/workflow_generatif.py"
    )

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
