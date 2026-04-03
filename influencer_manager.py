"""
influencer_manager.py — Gestionnaire de profils d'influenceurs.

RESPONSABILITÉS :
- Charger le profil actif depuis data/active_influencer.txt
- Résoudre les chemins de fichiers selon le profil actif
- Fournir des helpers pour accéder aux données de chaque influenceur
- Permettre le switch de profil (via Telegram /influencer)

UTILISÉ PAR :
config.py, concept_generator.py, image_generator.py,
telegram_bot.py, main.py, tous les workflows

ARCHITECTURE :
data/
├── influencers.json              # Registry global {"profiles": ["madison"], "default": "madison"}
├── active_influencer.txt         # "madison" (profil actif courant)
│
├── profiles/
│   ├── madison.json              # Profil complet (variables, calendar, character_template, hashtags)
│   └── example.json              # Template vide pour les utilisateurs
│
├── refs/
│   ├── madison_face.jpg          # Référence visage
│   ├── madison_body.jpg          # Référence corps
│   └── example_*.jpg             # Placeholders
│
└── runtime/                      # Données générées (gitignore)
    ├── madison_history.json
    ├── madison_pending_state.json
    └── madison_video_history.json
"""

import json
import os
from pathlib import Path
from typing import Any

# ================================================================
# CONSTANTES
# ================================================================

_BASE_DIR = Path(__file__).parent
_DATA_DIR = _BASE_DIR / "data"
_INFLUENCERS_JSON = _DATA_DIR / "influencers.json"
_ACTIVE_INFLUENCER_FILE = _DATA_DIR / "active_influencer.txt"
_PROFILES_DIR = _DATA_DIR / "profiles"
_REFS_DIR = _DATA_DIR / "refs"
_RUNTIME_DIR = _DATA_DIR / "runtime"

# Fallback pour rétrocompatibilité (Phase 1 — migration douce)
_FALLBACK_PATHS = {
    "variables": _DATA_DIR / "variables.json",
    "calendar": _DATA_DIR / "calendar.json",
    "history": _DATA_DIR / "history.json",
    "pending_state": _DATA_DIR / "pending_state.json",
    "video_history": _DATA_DIR / "video_history.json",
}

# ================================================================
# CACHE GLOBAL
# ================================================================

_cache: dict[str, Any] = {}


def _clear_cache() -> None:
    """Vide le cache (utilisé après set_active_influencer)."""
    global _cache
    _cache = {}


# ================================================================
# GESTION DU PROFIL ACTIF
# ================================================================

def get_active_influencer() -> str:
    """
    Retourne le nom du profil actif (ex: "madison").
    
    Cherche dans cet ordre :
    1. data/active_influencer.txt
    2. default depuis data/influencers.json
    3. Fallback hardcodé : "madison"
    
    Returns:
        str : nom du profil actif
    """
    if "active_influencer" in _cache:
        return _cache["active_influencer"]
    
    # Priorité 1 : active_influencer.txt
    if _ACTIVE_INFLUENCER_FILE.exists():
        name = _ACTIVE_INFLUENCER_FILE.read_text(encoding="utf-8").strip()
        if name:
            _cache["active_influencer"] = name
            return name
    
    # Priorité 2 : default depuis influencers.json
    if _INFLUENCERS_JSON.exists():
        registry = json.loads(_INFLUENCERS_JSON.read_text(encoding="utf-8"))
        default = registry.get("default")
        if default:
            _cache["active_influencer"] = default
            return default
    
    # Fallback hardcodé (rétrocompatibilité Phase 1)
    _cache["active_influencer"] = "madison"
    return "madison"


def set_active_influencer(name: str) -> None:
    """
    Change le profil actif — écrit dans data/active_influencer.txt.
    
    Args:
        name : nom du profil (doit exister dans influencers.json)
    
    Raises:
        ValueError : si le profil n'existe pas
    """
    if name not in list_influencers():
        raise ValueError(
            f"Profil '{name}' inconnu. Profils disponibles : {list_influencers()}"
        )
    
    _ACTIVE_INFLUENCER_FILE.parent.mkdir(parents=True, exist_ok=True)
    _ACTIVE_INFLUENCER_FILE.write_text(name, encoding="utf-8")
    _clear_cache()


def list_influencers() -> list[str]:
    """
    Retourne la liste des profils disponibles depuis influencers.json.
    
    Returns:
        list[str] : noms des profils (ex: ["madison", "sofia"])
    """
    if "influencer_list" in _cache:
        return _cache["influencer_list"]
    
    if not _INFLUENCERS_JSON.exists():
        # Fallback : si influencers.json n'existe pas encore (migration)
        _cache["influencer_list"] = ["madison"]
        return ["madison"]
    
    registry = json.loads(_INFLUENCERS_JSON.read_text(encoding="utf-8"))
    profiles = registry.get("profiles", [])
    _cache["influencer_list"] = profiles
    return profiles


# ================================================================
# CHARGEMENT DES DONNÉES
# ================================================================

def get_profile(name: str | None = None) -> dict:
    """
    Retourne le profil complet depuis data/profiles/{name}.json.
    
    Args:
        name : nom du profil (None = profil actif)
    
    Returns:
        dict : contenu complet du profil (variables, calendar, character_template, etc.)
    
    Raises:
        FileNotFoundError : si le profil n'existe pas
    """
    if name is None:
        name = get_active_influencer()
    
    cache_key = f"profile_{name}"
    if cache_key in _cache:
        return _cache[cache_key]
    
    profile_path = _PROFILES_DIR / f"{name}.json"
    
    if not profile_path.exists():
        raise FileNotFoundError(
            f"Profil '{name}' introuvable : {profile_path}\n"
            f"Profils disponibles : {list_influencers()}"
        )
    
    profile = json.loads(profile_path.read_text(encoding="utf-8"))
    _cache[cache_key] = profile
    return profile


def get_variables(name: str | None = None) -> dict:
    """
    Retourne le bloc variables depuis le profil actif.
    
    Contient : locations, outfits, poses, moods, lighting, pinterest_tags, etc.
    
    Args:
        name : nom du profil (None = profil actif)
    
    Returns:
        dict : bloc "variables"
    """
    profile = get_profile(name)
    return profile.get("variables", {})


def get_calendar(name: str | None = None) -> dict:
    """
    Retourne le bloc calendar depuis le profil actif.
    
    Args:
        name : nom du profil (None = profil actif)
    
    Returns:
        dict : bloc "calendar" (cycle éditorial)
    """
    profile = get_profile(name)
    return profile.get("calendar", {})


def get_character_template(name: str | None = None) -> dict:
    """
    Retourne le bloc character_template depuis le profil actif.
    
    Utilisé pour inject_character_body() et workflow_generatif.
    
    Args:
        name : nom du profil (None = profil actif)
    
    Returns:
        dict : bloc "character_template"
    """
    profile = get_profile(name)
    return profile.get("character_template", {})


def get_hashtag_blocks(name: str | None = None) -> dict:
    """
    Retourne les blocs hashtags depuis le profil actif.
    
    Args:
        name : nom du profil (None = profil actif)
    
    Returns:
        dict : {"lifestyle": "#...", "beach": "#...", ...}
    """
    profile = get_profile(name)
    return profile.get("hashtag_blocks", {})


# ================================================================
# RÉSOLUTION DE CHEMINS
# ================================================================

def get_path(filename: str, name: str | None = None, ensure_exists: bool = False) -> str:
    """
    Résout un chemin de fichier pour le profil actif.
    
    Chemins supportés :
    - "ref_face.jpg"        → data/refs/{name}_face.jpg
    - "ref_body.jpg"        → data/refs/{name}_body.jpg
    - "history.json"        → data/runtime/{name}_history.json
    - "pending_state.json"  → data/runtime/{name}_pending_state.json
    - "video_history.json"  → data/runtime/{name}_video_history.json
    - "variables.json"      → data/profiles/{name}.json (bloc variables)
    - "calendar.json"       → data/profiles/{name}.json (bloc calendar)
    
    FALLBACK (rétrocompatibilité Phase 1) :
    Si le fichier n'existe pas dans runtime/, chercher dans data/ racine.
    
    Args:
        filename : nom du fichier à résoudre
        name : nom du profil (None = profil actif)
        ensure_exists : créer le dossier parent si nécessaire (défaut : False)
    
    Returns:
        str : chemin absolu résolu
    
    Examples:
        >>> get_path("ref_face.jpg")
        'c:/.../ data/refs/madison_face.jpg'
        >>> get_path("history.json")
        'c:/.../data/runtime/madison_history.json'
    """
    if name is None:
        name = get_active_influencer()
    
    # Résolution selon le type de fichier
    if filename in ("ref_face.jpg", "ref_face"):
        path = _REFS_DIR / f"{name}_face.jpg"
    elif filename in ("ref_body.jpg", "ref_body"):
        path = _REFS_DIR / f"{name}_body.jpg"
    elif filename in ("history.json", "history"):
        path = _RUNTIME_DIR / f"{name}_history.json"
        # Fallback Phase 1 : si le fichier n'existe pas, chercher dans data/ racine
        if not path.exists() and _FALLBACK_PATHS.get("history", Path()).exists():
            return str(_FALLBACK_PATHS["history"])
    elif filename in ("pending_state.json", "pending_state"):
        path = _RUNTIME_DIR / f"{name}_pending_state.json"
        if not path.exists() and _FALLBACK_PATHS.get("pending_state", Path()).exists():
            return str(_FALLBACK_PATHS["pending_state"])
    elif filename in ("video_history.json", "video_history"):
        path = _RUNTIME_DIR / f"{name}_video_history.json"
        if not path.exists() and _FALLBACK_PATHS.get("video_history", Path()).exists():
            return str(_FALLBACK_PATHS["video_history"])
    elif filename in ("variables.json", "variables"):
        # Note : variables est maintenant dans le profil JSON, pas un fichier séparé
        # Mais on maintient la compatibilité en retournant le chemin du profil
        # L'appelant devra utiliser get_variables() à la place
        if _FALLBACK_PATHS.get("variables", Path()).exists():
            return str(_FALLBACK_PATHS["variables"])
        raise NotImplementedError(
            f"'{filename}' est maintenant dans data/profiles/{name}.json. "
            f"Utilisez get_variables() à la place."
        )
    elif filename in ("calendar.json", "calendar"):
        if _FALLBACK_PATHS.get("calendar", Path()).exists():
            return str(_FALLBACK_PATHS["calendar"])
        raise NotImplementedError(
            f"'{filename}' est maintenant dans data/profiles/{name}.json. "
            f"Utilisez get_calendar() à la place."
        )
    else:
        # Fallback générique : {name}_{filename} dans runtime/
        path = _RUNTIME_DIR / f"{name}_{filename}"
    
    if ensure_exists:
        path.parent.mkdir(parents=True, exist_ok=True)
    
    return str(path)


# ================================================================
# HELPERS SUPPLÉMENTAIRES
# ================================================================

def get_instagram_credentials(name: str | None = None) -> tuple[str, str]:
    """
    Retourne (INSTAGRAM_ACCOUNT_ID, INSTAGRAM_ACCESS_TOKEN) depuis le profil.
    
    Args:
        name : nom du profil (None = profil actif)
    
    Returns:
        tuple[str, str] : (account_id, access_token)
    """
    profile = get_profile(name)
    account_id = profile.get("instagram_account_id", "")
    access_token = profile.get("instagram_access_token", "")
    return account_id, access_token


def get_display_name(name: str | None = None) -> str:
    """
    Retourne le display_name depuis le profil (ex: "Madison").
    
    Args:
        name : nom du profil (None = profil actif)
    
    Returns:
        str : display_name
    """
    profile = get_profile(name)
    return profile.get("display_name", profile.get("name", "Unknown"))


def get_gender(name: str | None = None) -> str:
    """
    Retourne le gender depuis le profil ("female" ou "male").
    
    Args:
        name : nom du profil (None = profil actif)
    
    Returns:
        str : "female" | "male"
    """
    profile = get_profile(name)
    return profile.get("gender", "female")
