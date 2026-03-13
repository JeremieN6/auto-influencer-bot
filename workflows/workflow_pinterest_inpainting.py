"""
workflows/workflow_pinterest_inpainting.py — Workflow inpainting : Pinterest + Gemini inpainting natif.

DESCRIPTION :
  Variante du workflow Pinterest qui remplace directement le personnage de l'image source
  par l'influenceuse IA via inpainting Gemini — sans passer par une étape JSON intermédiaire.
  Le décor, la lumière et la composition de la photo originale sont préservés intégralement.

DIFFÉRENCE AVEC workflow_pinterest.py (workflow JSON) :
  workflow_pinterest.py  → Image Pinterest → JSON scène → Gemini génère une nouvelle image
  workflow_pinterest_inpainting.py → Image Pinterest → rembg masque → Gemini inpainting natif

FLUX COMPLET :
  [variables.json — tirage aléatoire mood + location + lighting]
        ↓
  [Pinterest — scraping image avec personnage]
        ↓
  [rembg — segmentation automatique → masque binaire personnage]
        ↓
  [Gemini inpainting natif]
        image source + masque + ref_face + ref_body + prompt
        remplace uniquement la zone masquée
        préserve décor, lumière, composition originale
        ↓
  [Sauvegarde dans outputs/ + copie vers nginx]
        ↓
  [Retourne (local_path, public_url, filename)]

Appelé par : main.run_pipeline(workflow="pinterest_inpainting")
Dépendances : inpainting.py, pinterest_scraper.py
Références  : data/ref_{influencer}_face.jpg + data/ref_{influencer}_body.jpg
"""

from config import INFLUENCER_NAME, INFLUENCER_REF_BODY_PATH, INFLUENCER_REF_FACE_PATH
from inpainting import replace_person
from logger import get_logger, log_section, log_step

logger = get_logger(__name__)

TOTAL_STEPS = 3


def run(concept: dict) -> tuple[str, str, str]:
    """
    Exécute le workflow Pinterest inpainting complet.

    Args:
        concept : dict généré par concept_generator.generate_concept()
                  {location, outfit, pose, mood, lighting, generated_at}

    Returns:
        (local_path, public_url, filename)
        - local_path  : chemin local dans outputs/
        - public_url  : URL publique nginx (pour API Meta)
        - filename    : nom de fichier seul (pour cleanup nginx après publication)

    Raises:
        RuntimeError      : si Pinterest ne trouve aucune image valide
        FileNotFoundError : si ref_face ou ref_body est absent
        ValueError        : si Gemini inpainting échoue à retourner une image
        ImportError       : si rembg n'est pas installé
    """
    log_section("workflow_pinterest_inpainting", "DÉMARRAGE WORKFLOW INPAINTING")

    # ── Étape 1 : Scraping Pinterest ─────────────────────────────
    log_step("workflow_pinterest_inpainting", 1, TOTAL_STEPS, "Scraping Pinterest")
    from pinterest_scraper import run as scrape

    source_image_path = scrape(concept)
    logger.info(f"Image source récupérée : {source_image_path}")

    # ── Étape 2 : Inpainting Gemini ───────────────────────────────
    log_step("workflow_pinterest_inpainting", 2, TOTAL_STEPS, "Inpainting Gemini (rembg + Gemini natif)")
    logger.info(
        f"Références : face={INFLUENCER_REF_FACE_PATH} | body={INFLUENCER_REF_BODY_PATH}"
    )

    local_path, public_url, filename = replace_person(
        source_image_path=source_image_path,
        influencer_name=INFLUENCER_NAME,
        ref_face_path=INFLUENCER_REF_FACE_PATH,
        ref_body_path=INFLUENCER_REF_BODY_PATH,
    )

    # ── Étape 3 : Confirmation ────────────────────────────────────
    log_step("workflow_pinterest_inpainting", 3, TOTAL_STEPS, "Image générée")
    logger.info(f"Image finale : {local_path}")
    logger.info(f"URL publique : {public_url}")

    log_section("workflow_pinterest_inpainting", "WORKFLOW INPAINTING TERMINÉ")
    return local_path, public_url, filename
