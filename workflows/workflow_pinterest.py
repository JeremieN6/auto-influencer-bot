"""
workflows/workflow_pinterest.py — Workflow V1 : inspiration depuis Pinterest.

DESCRIPTION :
  Source d'inspiration visuelle = Pinterest.
  Les variables.json servent à construire la requête de recherche Pinterest.

FLUX COMPLET :
  [variables.json — tirage aléatoire mood + location + lighting]
        ↓
  [Assemblage requête Pinterest via combine concept + keywords]
        ↓
  [Playwright — recherche Pinterest — récupération URLs 10-20 résultats en mémoire]
        ↓
  [Sélection aléatoire + test personnage via Gemini Vision (PROMPT_PERSON_DETECTION)]
    OUI → on continue
    NON → retry dans la liste → liste épuisée → boost "insta" → shuffle keywords
        ↓
  [Gemini Vision — PROMPT_IMAGE_TO_JSON → JSON de scène]
        ↓
  [Gemini — PROMPT_JSON_TO_IMAGE + référence influenceuse → image finale]
        ↓
  [Sauvegarde dans outputs/ + copie vers nginx]
        ↓
  [Retourne (local_path, public_url, filename)]

Appelé par : main.run_pipeline(workflow="pinterest")
"""

import json
import os

from image_generator import generate_image, image_to_json
from logger import get_logger, log_section, log_step
from prompts import PROMPT_JSON_TO_IMAGE

logger = get_logger(__name__)

# Nombre d'étapes pour les logs de pipeline
TOTAL_STEPS = 3


def run(concept: dict) -> tuple[str, str, str]:
    """
    Exécute le workflow Pinterest complet.

    Args:
        concept : dict généré par concept_generator.generate_concept()
                  {location, outfit, pose, mood, lighting, generated_at}

    Returns:
        (local_path, public_url, filename)
        - local_path  : chemin local dans outputs/
        - public_url  : URL publique nginx (pour API Meta)
        - filename    : nom de fichier seul (pour cleanup nginx après publication)

    Raises:
        RuntimeError  : si Pinterest ne trouve aucune image valide
        FileNotFoundError : si l'image de référence influenceuse est absente
        ValueError    : si Gemini échoue à retourner une image
    """
    log_section(__name__, f"WORKFLOW PINTEREST")
    logger.info(f"Concept : {concept}")

    # ── Étape 1/3 : Scraping Pinterest ──────────────────────────
    log_step(__name__, 1, TOTAL_STEPS, "Scraping Pinterest")

    from pinterest_scraper import scrape_pinterest_image
    from config import PINTEREST_KEYWORDS

    inspiration_path, source_url, search_query = scrape_pinterest_image(concept, PINTEREST_KEYWORDS)
    logger.info(f"Image d'inspiration : {inspiration_path}")

    # ── Affichage récap recherche Pinterest ─────────────────────
    sep = "─" * 60
    logger.info(
        f"\n{sep}\n"
        f"  RECHERCHE PINTEREST\n"
        f"  Requête  : {search_query}\n"
        f"  URL base : {source_url}\n"
        f"{sep}"
    )

    # ── Étape 2/3 : Image → JSON de scène ───────────────────────
    log_step(__name__, 2, TOTAL_STEPS, "Analyse image → JSON de scène (Gemini Vision)")

    scene_json = image_to_json(inspiration_path)
    logger.info(f"JSON de scène extrait — clés : {list(scene_json.keys())}")
    logger.debug(f"JSON complet : {json.dumps(scene_json, indent=2, ensure_ascii=False)[:800]}...")

    # Nettoyer l'image Pinterest source (elle n'est plus nécessaire)
    _cleanup_inspiration(inspiration_path)

    # ── Étape 3/3 : JSON + référence → Image finale ──────────────
    log_step(__name__, 3, TOTAL_STEPS, "Génération image finale (Gemini + référence influenceuse)")

    prompt_text = PROMPT_JSON_TO_IMAGE.format(
        scene_json=json.dumps(scene_json, indent=2, ensure_ascii=False)
    )
    local_path, public_url = generate_image(prompt_text)

    filename = os.path.basename(local_path)
    logger.info(f"=== Workflow Pinterest terminé : {local_path} ===")

    return local_path, public_url, filename, source_url, search_query


# ================================================================
# Helpers
# ================================================================

def _cleanup_inspiration(path: str) -> None:
    """Supprime l'image Pinterest source après extraction du JSON."""
    try:
        if os.path.exists(path):
            os.remove(path)
            logger.debug(f"Image Pinterest source supprimée : {path}")
    except Exception as e:
        logger.warning(f"Impossible de supprimer l'image source : {e}")
