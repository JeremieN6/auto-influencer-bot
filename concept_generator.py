"""
concept_generator.py — Génération de concepts créatifs.

Responsabilités :
- Tirer aléatoirement location / outfit / pose / mood / lighting depuis variables.json
- Éviter les répétitions sur la fenêtre HISTORY_WINDOW_DAYS (anti-répétition 30 jours)
- Maintenir history.json (ajout après chaque run automatique)
- Calculer le step éditorial courant depuis calendar.json
- Construire le prompt caption formaté

Utilisé par : workflow_pinterest.py, workflow_generatif.py, main.py
"""

import json
import random
from datetime import datetime

from config import (
    HISTORY_WINDOW_DAYS,
    VARIABLES_PATH,
    HISTORY_PATH,
    CALENDAR_PATH,
    INFLUENCER_NAME,
)
from logger import get_logger

logger = get_logger(__name__)


# ================================================================
# I/O — variables, historique, calendrier
# ================================================================

def load_variables(path: str = VARIABLES_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def load_history(path: str = HISTORY_PATH) -> list:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        logger.debug(f"history.json absent — démarrage avec historique vide ({path})")
        return []


def save_history(history: list, path: str = HISTORY_PATH) -> None:
    import os
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)
    logger.debug(f"Historique sauvegardé — {len(history)} entrée(s)")


def load_calendar(path: str = CALENDAR_PATH) -> dict:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ================================================================
# Génération de concept
# ================================================================

def generate_concept(
    override_params: dict | None = None,
    persist: bool = True,
) -> dict:
    """
    Tire un concept créatif aléatoire depuis variables.json.

    - override_params : paramètres fournis manuellement (commande /run V2).
      Les champs fournis remplacent le tirage aléatoire correspondant.
    - persist : si True, ajoute le concept à history.json.
      Mettre à False pour les runs manuels via /run (non-cycle).

    Retourne un dict avec les clés :
        location, outfit, pose, mood, lighting, generated_at
    """
    variables = load_variables()
    history   = load_history()
    recent    = history[-HISTORY_WINDOW_DAYS:]
    recent_keys = {
        f"{e['outfit']}|{e['location']}|{e['pose']}"
        for e in recent
        if all(k in e for k in ("outfit", "location", "pose"))
    }

    logger.info(f"Génération concept — {len(recent_keys)} combinaisons vues dans les {HISTORY_WINDOW_DAYS} derniers jours")

    concept: dict = {}
    for attempt in range(50):
        concept = {
            "location":     random.choice(variables["locations"]),
            "outfit":       random.choice(variables["outfits"]),
            "pose":         random.choice(variables["poses"]),
            "mood":         random.choice(variables["moods"]),
            "lighting":     random.choice(variables["lighting"]),
            "generated_at": datetime.now().isoformat(),
        }

        # Appliquer les overrides manuels si fournis (/run mode manuel)
        if override_params:
            concept.update({k: v for k, v in override_params.items() if v})

        key = f"{concept['outfit']}|{concept['location']}|{concept['pose']}"
        if key not in recent_keys:
            logger.debug(f"Concept unique trouvé à la tentative {attempt + 1} : {key}")
            break
        logger.debug(f"Tentative {attempt + 1} — combinaison déjà vue ({key}), re-tirage...")
    else:
        logger.warning("Toutes les combinaisons ont été vues — utilisation du dernier concept tiré")

    if persist:
        history.append(concept)
        save_history(history)
        logger.info(f"Concept persisté → history.json ({len(history)} total)")

    logger.info(
        f"Concept : {concept['mood']} | {concept['outfit']} | "
        f"{concept['location']} | {concept['lighting']}"
    )
    return concept


# ================================================================
# Calendrier éditorial
# ================================================================

def get_current_calendar_step(
    history_path: str = HISTORY_PATH,
    calendar_path: str = CALENDAR_PATH,
) -> dict:
    """
    Retourne l'étape calendrier courante en fonction de la position dans l'historique.
    Le cycle tourne en boucle : step 1, 2, 3, 4, 1, 2, 3, 4, ...

    Retourne un dict avec les clés : step, format, type, hashtags, note
    """
    calendar = load_calendar(calendar_path)
    history  = load_history(history_path)
    cycle    = calendar["cycle"]
    step     = cycle[len(history) % len(cycle)]
    logger.info(f"Étape calendrier : step={step['step']} | format={step['format']} | type={step['type']} | hashtags={step['hashtags']}")
    return step


def get_schedule_preview(
    history_path: str = HISTORY_PATH,
    calendar_path: str = CALENDAR_PATH,
    n: int = 4,
) -> list[dict]:
    """
    Retourne les N prochaines étapes du calendrier éditorial (pour /schedule).
    """
    from datetime import timedelta

    calendar = load_calendar(calendar_path)
    history  = load_history(history_path)
    cycle    = calendar["cycle"]
    base_idx = len(history)
    now      = datetime.now()

    from config import POSTING_INTERVAL_DAYS

    preview = []
    for i in range(n):
        step = cycle[(base_idx + i) % len(cycle)]
        eta  = now + timedelta(days=i * POSTING_INTERVAL_DAYS)
        preview.append({**step, "eta": eta.strftime("%d/%m/%Y %H:%M")})
    return preview


# ================================================================
# Construction du prompt caption
# ================================================================

def build_caption_prompt(concept: dict, step: dict) -> str:
    """
    Formate PROMPT_CAPTION_TEMPLATE avec les données du concept et du step calendrier.
    Retourne le prompt prêt à envoyer à Claude.
    """
    from prompts import PROMPT_CAPTION_TEMPLATE, HASHTAG_BLOCK_SKINCARE

    include_hashtags    = step["hashtags"]
    hashtag_instruction = (
        f"Ajoute ce bloc de hashtags en fin de caption :\n{HASHTAG_BLOCK_SKINCARE}"
        if include_hashtags
        else "N'ajoute aucun hashtag."
    )
    concept_description = (
        f"{concept['mood']} — {concept['outfit']} — "
        f"{concept['location']} — {concept['lighting']}"
    )
    prompt = PROMPT_CAPTION_TEMPLATE.format(
        influencer_name=INFLUENCER_NAME,
        concept_description=concept_description,
        post_type=step["type"],
        include_hashtags="oui" if include_hashtags else "non",
        hashtag_instruction=hashtag_instruction,
    )
    logger.debug("Prompt caption construit")
    return prompt
