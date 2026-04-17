"""
content_planner.py — "Conscience" éditoriale de l'influenceur.

Responsabilités :
- Appeler Claude pour planifier les prochaines publications
- Construire le contexte (historique, stats, profil, variables)
- Parser la réponse JSON en plan d'exécution
- Fallback sur le pool_mix du calendrier si l'appel échoue

Utilisé par : main.py (scheduler multi-fréquence)
"""

import json
import random
from datetime import datetime

import anthropic

from config import ANTHROPIC_API_KEY, HISTORY_PATH, CALENDAR_PATH, VARIABLES_PATH
from concept_generator import load_history, load_calendar, load_variables
from logger import get_logger
from prompts import PROMPT_CONTENT_PLANNER

logger = get_logger(__name__)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
CLAUDE_MODEL = "claude-sonnet-4-20250514"

# Hints d'engagement par jour de la semaine (0=lundi)
WEEKDAY_HINTS = {
    0: "Lundi — reprise de semaine, stories ambiance cozy et restart bien reçues",
    1: "Mardi — mid-week, bon pour feed ou reel",
    2: "Mercredi — pic d'engagement, idéal pour reel ou feed flagship",
    3: "Jeudi — engagement stable, bon pour tout type de contenu",
    4: "Vendredi — mood weekend, contenu plus détendu et festif",
    5: "Samedi — audience active en journée, stories lifestyle bien reçues",
    6: "Dimanche — audience détendue, contenu cozy et introspectif",
}


# ================================================================
# Construction du prompt
# ================================================================

def _format_history_block(history: list, limit: int = 20) -> str:
    """Formate les N dernières entrées de l'historique pour le prompt."""
    recent = history[-limit:] if history else []
    if not recent:
        return "(aucun historique — premier run)"

    lines = []
    for entry in reversed(recent):
        date = entry.get("generated_at", "?")[:16].replace("T", " ")
        ct = entry.get("content_type", "?")
        mood = entry.get("mood", "?")
        outfit = entry.get("outfit", "?")
        location = entry.get("location", "?")
        pool = entry.get("pool_type", "?")
        lines.append(f"  [{date}] {ct} | mood={mood} | outfit={outfit} | loc={location} | pool={pool}")
    return "\n".join(lines)


def _compute_stats_block(history: list, calendar: dict) -> str:
    """Calcule les statistiques par type de contenu pour le prompt."""
    now = datetime.now()
    content_types = calendar.get("content_types", {})
    lines = []

    for ct_name, ct_config in content_types.items():
        interval = ct_config.get("interval_days", 1)
        batch = ct_config.get("batch_size", 1)
        count = 0
        last_date = None

        # Compter dans la fenêtre et sous-compter faceless vs character pour stories
        faceless_count = 0
        character_count = 0

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
            if elapsed > interval:
                break
            if entry_type == ct_name:
                count += 1
                if last_date is None:
                    last_date = entry_time
                # Track faceless vs character pour stories
                pool = entry.get("pool_type", "")
                if pool == "story":
                    faceless_count += 1
                elif pool == "reel":
                    character_count += 1

        # Trouver la date absolue du dernier contenu de ce type (hors fenêtre aussi)
        if last_date is None:
            for entry in reversed(history):
                entry_type = entry.get("content_type") or entry.get("step", {}).get("type", "")
                if entry_type != ct_name:
                    continue
                generated_at = entry.get("generated_at", "")
                if not generated_at:
                    continue
                try:
                    last_date = datetime.fromisoformat(generated_at)
                    break
                except (ValueError, TypeError):
                    continue

        last_str = f"il y a {(now - last_date).total_seconds() / 86400:.1f}j" if last_date else "jamais"
        line = f"- {ct_name} derniers {interval}j : {count}/{batch} | dernier : {last_str}"
        if ct_name == "story" and (faceless_count or character_count):
            line += f" (faceless: {faceless_count}, character: {character_count})"
        lines.append(line)

    return "\n".join(lines)


def _build_production_block(due_types: list[dict]) -> str:
    """Liste ce que le scheduler demande de produire."""
    lines = []
    for ct in due_types:
        ct_name = ct["_content_type"]
        deficit = ct["_deficit"]
        if ct_name == "story":
            lines.append(f"- {deficit}x story (à toi de choisir faceless ou character pour chacune)")
        elif ct_name == "reel":
            lines.append(f"- {deficit}x reel (vidéo avec personnage)")
        elif ct_name == "feed":
            lines.append(f"- {deficit}x feed (photo avec personnage)")
        else:
            lines.append(f"- {deficit}x {ct_name}")
    return "\n".join(lines)


def _load_profile() -> dict:
    """Charge le profil actif de l'influenceur."""
    try:
        from influencer_manager import get_profile
        return get_profile()
    except (ImportError, FileNotFoundError, KeyError) as e:
        logger.warning(f"Impossible de charger le profil via influencer_manager : {e}")
        return {
            "display_name": "Madison",
            "style": "californian aesthetic, mid-20s, blonde, casual-sexy vibe",
            "tone": "casual confident, playful",
            "audience": {"summary": "25-64, mostly men, values aesthetic quality"},
        }


def build_planner_prompt(due_types: list[dict]) -> str:
    """
    Construit le prompt complet pour le content planner.

    Args:
        due_types: liste des types de contenu dus (sortie de get_due_content_types)

    Returns:
        Prompt formaté prêt à envoyer à Claude.
    """
    profile = _load_profile()
    history = load_history()
    calendar = load_calendar()
    variables = load_variables()

    now = datetime.now()
    weekday_hint = WEEKDAY_HINTS.get(now.weekday(), "")

    prompt = PROMPT_CONTENT_PLANNER.format(
        display_name=profile.get("display_name", "Madison"),
        style=profile.get("style", ""),
        tone=profile.get("tone", "casual confident, playful"),
        audience_summary=profile.get("audience", {}).get("summary", "25-64, lifestyle audience")
            if isinstance(profile.get("audience"), dict)
            else str(profile.get("audience", "")),
        history_block=_format_history_block(history),
        stats_block=_compute_stats_block(history, calendar),
        locations=", ".join(variables.get("locations", [])),
        outfits=", ".join(variables.get("outfits", [])),
        moods=", ".join(variables.get("moods", [])),
        lighting=", ".join(variables.get("lighting", [])),
        production_block=_build_production_block(due_types),
        day_context=f"{weekday_hint}\n{now.strftime('%A %d/%m/%Y %H:%M')}",
    )
    return prompt


# ================================================================
# Appel Claude + parsing
# ================================================================

def _parse_plan(raw_text: str) -> list[dict]:
    """Parse la réponse JSON de Claude en liste de contenus planifiés."""
    text = raw_text.strip()
    # Nettoyer les éventuels markdown fences
    if text.startswith("```"):
        text = text.split("\n", 1)[-1]
        if text.endswith("```"):
            text = text[:-3].strip()

    data = json.loads(text)
    plan = data.get("plan", [])
    editorial_note = data.get("editorial_note", "")

    if editorial_note:
        logger.info(f"Planner editorial note: {editorial_note}")

    # Valider chaque item
    valid_types = {"story_faceless", "story_character", "reel", "feed"}
    validated = []
    for item in plan:
        item_type = item.get("type", "")
        if item_type not in valid_types:
            logger.warning(f"Planner : type inconnu '{item_type}' — ignoré")
            continue
        validated.append(item)

    return validated


def plan_content(due_types: list[dict]) -> list[dict] | None:
    """
    Appelle Claude pour planifier les prochaines publications.

    Args:
        due_types: liste des types de contenu dus

    Returns:
        Liste de dicts avec les clés : type, theme, tag_category, mood, lighting,
        location, outfit, reason.
        None si l'appel échoue (fallback sur pool_mix).
    """
    if not due_types:
        return []

    prompt = build_planner_prompt(due_types)
    logger.info(f"Content planner : appel Claude ({CLAUDE_MODEL})...")
    logger.debug(f"Prompt planner (extrait) : {prompt[:500]}...")

    try:
        message = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = message.content[0].text
        logger.debug(f"Planner raw response: {raw[:500]}...")

        plan = _parse_plan(raw)
        if not plan:
            logger.warning("Planner : plan vide retourné — fallback")
            return None

        logger.info(f"Content planner : {len(plan)} contenu(s) planifié(s)")
        for i, item in enumerate(plan):
            logger.info(
                f"  [{i+1}] {item['type']} | theme={item.get('theme', '?')} "
                f"| mood={item.get('mood', '?')} | reason={item.get('reason', '?')[:60]}"
            )
        return plan

    except json.JSONDecodeError as e:
        logger.error(f"Planner : JSON invalide — {e}")
        return None
    except anthropic.APIError as e:
        logger.error(f"Planner : erreur API Claude — {e}")
        return None
    except Exception as e:
        logger.error(f"Planner : erreur inattendue — {e}")
        return None


# ================================================================
# Fallback pool_mix
# ================================================================

def fallback_pool_mix(due_types: list[dict]) -> list[dict]:
    """
    Génère un plan de fallback basé sur le pool_mix du calendrier.

    Utilisé quand le planner Claude échoue.
    Retourne une liste de dicts au même format que plan_content().
    """
    calendar = load_calendar()
    variables = load_variables()
    content_types = calendar.get("content_types", {})
    plan = []

    for ct in due_types:
        ct_name = ct["_content_type"]
        deficit = ct["_deficit"]
        ct_config = content_types.get(ct_name, {})
        pool_mix = ct_config.get("pool_mix", [])

        for i in range(deficit):
            if ct_name == "story" and pool_mix:
                # Utiliser le pool_mix séquentiellement
                pool_choice = pool_mix[i % len(pool_mix)]
                item_type = "story_character" if pool_choice == "reel" else "story_faceless"
            elif ct_name == "story":
                item_type = "story_faceless"
            elif ct_name == "reel":
                item_type = "reel"
            elif ct_name == "feed":
                item_type = "feed"
            else:
                item_type = ct_name

            # Tirage aléatoire basique depuis variables
            plan.append({
                "type": item_type,
                "theme": None,
                "tag_category": random.choice(["lifestyle", "beach", "outfit"]),
                "mood": random.choice(variables.get("moods", ["natural"])),
                "lighting": random.choice(variables.get("lighting", ["natural"])),
                "location": random.choice(variables.get("locations", [None])) if item_type != "story_faceless" else None,
                "outfit": random.choice(variables.get("outfits", [None])) if item_type != "story_faceless" else None,
                "reason": "fallback pool_mix — planner indisponible",
            })

    logger.info(f"Fallback pool_mix : {len(plan)} contenu(s) planifiés")
    return plan


# ================================================================
# Fonction principale : plan ou fallback
# ================================================================

def get_content_plan(due_types: list[dict]) -> list[dict]:
    """
    Obtient le plan de contenu : essaie le planner Claude, fallback sur pool_mix.

    Args:
        due_types: liste des types de contenu dus

    Returns:
        Liste de dicts prêts à être exécutés par le scheduler main.py.
    """
    plan = plan_content(due_types)
    if plan is not None:
        return plan

    logger.warning("Content planner indisponible — utilisation du fallback pool_mix")
    return fallback_pool_mix(due_types)
