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
    content_type: str | None = None,
    pool_type: str | None = None,
) -> dict:
    """
    Tire un concept créatif aléatoire depuis variables.json.

    - override_params : paramètres fournis manuellement (commande /run V2).
      Les champs fournis remplacent le tirage aléatoire correspondant.
    - persist : si True, ajoute le concept à history.json.
      Mettre à False pour les runs manuels via /run (non-cycle).
    - content_type : type de contenu (story/reel/feed) — enregistré dans history.json
      pour le scheduler multi-fréquence.
    - pool_type : type de pool Pinterest (story/reel) — enregistré dans history.json
      pour le tracking faceless/character par le content planner.

    Retourne un dict avec les clés :
        location, outfit, pose, mood, lighting, generated_at[, content_type, pool_type]
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

    # Enregistrer le type de contenu et le pool pour le scheduler multi-fréquence
    if content_type:
        concept["content_type"] = content_type
    if pool_type:
        concept["pool_type"] = pool_type

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
    Retourne un step calendrier synthétique basé sur le prochain type de contenu dû.

    Rétro-compatible : retourne un dict avec les clés step, format, type, hashtags, note.
    Utilise le nouveau format multi-fréquence de calendar.json.
    """
    due = get_due_content_types(history_path=history_path, calendar_path=calendar_path)
    if due:
        ct = due[0]
    else:
        # Rien n'est dû — retourner le type avec la plus grande dette relative
        calendar = load_calendar(calendar_path)
        ct_name = next(iter(calendar.get("content_types", {})), "feed")
        ct = calendar["content_types"][ct_name]
        ct = {**ct, "_content_type": ct_name, "_deficit": 0}

    content_type = ct["_content_type"]
    step = {
        "step": 1,
        "format": ct.get("format", "4:5"),
        "type": content_type,
        "hashtags": ct.get("hashtags", True),
        "note": ct.get("note", ""),
    }
    logger.info(
        f"Étape calendrier : type={step['type']} | format={step['format']} "
        f"| hashtags={step['hashtags']} | deficit={ct.get('_deficit', '?')}"
    )
    return step


def get_due_content_types(
    history_path: str = HISTORY_PATH,
    calendar_path: str = CALENDAR_PATH,
) -> list[dict]:
    """
    Analyse le calendrier multi-fréquence et l'historique pour déterminer
    quels types de contenu doivent être produits maintenant.

    Pour chaque content_type dans calendar.json :
      - Compte combien ont été produits dans les derniers `interval_days` jours
      - Si count < batch_size → le type est "dû" avec un déficit

    Retourne une liste de dicts triée par priorité (plus gros déficit relatif d'abord).
    Chaque dict contient les clés du content_type + _content_type (nom) + _deficit (int).
    Liste vide si tout est à jour.
    """
    calendar = load_calendar(calendar_path)
    history  = load_history(history_path)
    now      = datetime.now()

    content_types = calendar.get("content_types", {})
    if not content_types:
        logger.warning("calendar.json ne contient pas de content_types — aucun contenu dû")
        return []

    due_list = []

    for ct_name, ct_config in content_types.items():
        interval_days = ct_config.get("interval_days", 1)
        batch_size    = ct_config.get("batch_size", 1)

        # Compter les entrées de ce type dans la fenêtre
        count_in_window = 0
        for entry in reversed(history):
            entry_type = entry.get("content_type") or entry.get("step", {}).get("type", "")
            generated_at = entry.get("generated_at", "")
            if not generated_at:
                continue
            try:
                entry_time = datetime.fromisoformat(generated_at)
            except (ValueError, TypeError):
                continue
            elapsed = (now - entry_time).total_seconds() / 86400
            if elapsed > interval_days:
                break  # Historique trié chronologiquement — on peut arrêter
            if entry_type == ct_name:
                count_in_window += 1

        deficit = batch_size - count_in_window
        if deficit > 0:
            due_list.append({
                **ct_config,
                "_content_type": ct_name,
                "_deficit": deficit,
                "_count_in_window": count_in_window,
            })
            logger.info(
                f"Scheduler : {ct_name} — {count_in_window}/{batch_size} produits "
                f"sur {interval_days}j → déficit={deficit}"
            )
        else:
            logger.debug(
                f"Scheduler : {ct_name} — {count_in_window}/{batch_size} produits "
                f"sur {interval_days}j → OK"
            )

    # Trier par déficit relatif décroissant (% du batch restant)
    due_list.sort(key=lambda x: x["_deficit"] / x.get("batch_size", 1), reverse=True)
    return due_list


def get_schedule_preview(
    history_path: str = HISTORY_PATH,
    calendar_path: str = CALENDAR_PATH,
    n: int = 6,
) -> list[dict]:
    """
    Retourne un aperçu du statut de chaque type de contenu (pour /schedule).
    Format multi-fréquence : montre le déficit et la prochaine échéance.
    """
    calendar = load_calendar(calendar_path)
    history  = load_history(history_path)
    now      = datetime.now()

    content_types = calendar.get("content_types", {})
    preview = []

    for ct_name, ct_config in content_types.items():
        interval_days = ct_config.get("interval_days", 1)
        batch_size    = ct_config.get("batch_size", 1)

        # Trouver le dernier contenu de ce type
        last_date = None
        count_in_window = 0
        for entry in reversed(history):
            entry_type = entry.get("content_type") or entry.get("step", {}).get("type", "")
            generated_at = entry.get("generated_at", "")
            if not generated_at:
                continue
            try:
                entry_time = datetime.fromisoformat(generated_at)
            except (ValueError, TypeError):
                continue
            elapsed = (now - entry_time).total_seconds() / 86400
            if elapsed > interval_days:
                break
            if entry_type == ct_name:
                count_in_window += 1
                if last_date is None:
                    last_date = entry_time

        deficit = max(0, batch_size - count_in_window)
        status = "✅" if deficit == 0 else f"⚠️ -{deficit}"

        preview.append({
            "type": ct_name,
            "format": ct_config.get("format", "?"),
            "interval": f"{interval_days}j",
            "batch": f"{count_in_window}/{batch_size}",
            "status": status,
            "workflow": ct_config.get("workflow", "?"),
            "last": last_date.strftime("%d/%m %H:%M") if last_date else "jamais",
        })

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
