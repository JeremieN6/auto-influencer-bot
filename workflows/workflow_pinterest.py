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
  [inject_madison_body() — injection du bloc corps fixe de Madison dans le JSON]
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

from image_generator import ImageSafetyError, generate_image, image_to_json, inject_madison_body, validate_body_proportions
from logger import get_logger, log_section, log_step
from prompts import PROMPT_JSON_TO_IMAGE

logger = get_logger(__name__)

# Nombre d'étapes pour les logs de pipeline
TOTAL_STEPS = 3


def run(concept: dict, keyword_pool: list[str] | None = None) -> tuple[str, str, str]:
    """
    Exécute le workflow Pinterest complet.

    Args:
        concept      : dict généré par concept_generator.generate_concept()
                       {location, outfit, pose, mood, lighting, generated_at}
        keyword_pool : pool de mots-clés Pinterest optionnel (mode --relevant).
                       Si None, utilise _PERSON_KEYWORDS par défaut.

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

    inspiration_path, source_url, search_query = scrape_pinterest_image(concept, keyword_pool=keyword_pool)
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

    # ── Injection du corps fixe de Madison ──────────────────────
    # Le JSON extrait de Pinterest ne contient pas de description corporelle.
    # inject_madison_body() greffe le bloc subject.body avec les formules
    # anatomiques gagnantes (hourglass, proportions, etc.) pour garantir
    # la cohérence du corps à chaque génération.
    scene_json = inject_madison_body(scene_json)
    logger.info("Bloc corps Madison injecté dans le JSON de scène")

    # Nettoyer l'image Pinterest source (elle n'est plus nécessaire)
    _cleanup_inspiration(inspiration_path)

    # ── Étape 3/3 : JSON + référence → Image finale ──────────────
    log_step(__name__, 3, TOTAL_STEPS, "Génération image finale (Gemini + référence influenceuse)")

    prompt_text = PROMPT_JSON_TO_IMAGE.format(
        scene_json=json.dumps(scene_json, indent=2, ensure_ascii=False)
    )
    local_path, public_url = generate_image(prompt_text)

    # ── Validation proportions + retry unique ────────────────────
    # Même logique que workflow_video_local : Gemini peut ignorer les consignes
    # corporelles à la première génération. Si les proportions hourglass ne sont
    # pas respectées on relance une fois avant d'accepter.
    body_ok = validate_body_proportions(local_path)
    if not body_ok:
        logger.warning("Proportions corps insuffisantes — 1 retry génération image...")
        try:
            local_path, public_url = generate_image(prompt_text)
            body_ok = validate_body_proportions(local_path)
            logger.info(f"Proportions après retry : {'✓ OK' if body_ok else '⚠ non validé (accepté quand même)'}")
        except ImageSafetyError:
            logger.warning("IMAGE_SAFETY sur retry proportions — image initiale conservée")
    else:
        logger.info("Proportions corps : ✓ OK")

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
