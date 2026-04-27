"""
config.py — Configuration centrale du système.

================================================================
POUR CHANGER D'INFLUENCEUSE :
Modifier uniquement le bloc INFLUENCER CONFIG ci-dessous.
Tout le reste est automatiquement dérivé ou reste inchangé.
================================================================
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Charge .env.local en priorité (dev local), puis .env (CI / VPS)
# Convention : copier .env.example → .env.local pour le développement local.
_env_candidates = [".env.local", ".env"]
for _env_file in _env_candidates:
    if Path(_env_file).exists():
        load_dotenv(dotenv_path=_env_file, override=True)
        break

# ================================================================
# INFLUENCER CONFIG — chargé depuis influencer_manager (Phase 1)
# ================================================================

# Tentative de chargement depuis le nouveau système (data/profiles/madison.json).
# Si échec (migration en cours), fallback sur les valeurs hardcodées ci-dessous.

try:
    from influencer_manager import (
        get_active_influencer,
        get_profile,
        get_path,
        get_display_name,
        get_gender,
    )
    
    _profile = get_profile()
    INFLUENCER_NAME = _profile["display_name"]
    INFLUENCER_STYLE = _profile["style"]
    INFLUENCER_GENDER = _profile.get("gender", "female")
    
    # Chemins des images de référence
    INFLUENCER_REF_FACE_PATH = get_path("ref_face.jpg")
    INFLUENCER_REF_BODY_PATH = get_path("ref_body.jpg")
    INFLUENCER_REF_IMAGE_PATH = INFLUENCER_REF_FACE_PATH.replace("_face.jpg", "")  # legacy
    
    # Mots-clés Pinterest (pour l'instant, utiliser les keywords lifestyle comme fallback)
    # TODO Phase 2 : extraire depuis _profile["variables"]["relevant_keywords"]
    try:
        _keywords_all = []
        for cat_keywords in _profile.get("variables", {}).get("relevant_keywords", {}).values():
            _keywords_all.extend(cat_keywords)
        PINTEREST_KEYWORDS = _keywords_all[:5] if _keywords_all else [
            "lifestyle aesthetic", "aesthetic girl", "instagram aesthetic"
        ]
    except Exception:
        PINTEREST_KEYWORDS = ["lifestyle aesthetic", "aesthetic girl", "instagram aesthetic"]

except (ImportError, FileNotFoundError, KeyError) as e:
    # Fallback — Phase 1 : migration en cours, utiliser les valeurs hardcodées
    import warnings
    warnings.warn(
        f"Impossible de charger le profil depuis influencer_manager : {e}. "
        f"Utilisation des valeurs hardcodées (rétrocompatibilité Phase 1).",
        stacklevel=2,
    )
    
    INFLUENCER_NAME = "Madison"
    INFLUENCER_STYLE = (
        "blonde californienne, casual-sexy aesthetic, "
        "beige/neutral color palette, authentic photography"
    )
    INFLUENCER_GENDER = "female"
    
    # Chemins legacy
    INFLUENCER_REF_IMAGE_PATH = f"data/ref_{INFLUENCER_NAME.lower()}"
    INFLUENCER_REF_FACE_PATH = f"data/ref_{INFLUENCER_NAME.lower()}_face.jpg"
    INFLUENCER_REF_BODY_PATH = f"data/ref_{INFLUENCER_NAME.lower()}_body.jpg"
    
    PINTEREST_KEYWORDS = [
        "lifestyle aesthetic",
        "skincare routine",
        "golden hour portrait",
        "casual outfit",
        "morning routine",
    ]

# ================================================================
# API KEYS — chargées depuis .env (jamais en dur ici)
# ================================================================

GEMINI_API_KEY         = os.getenv("GEMINI_API_KEY")
ANTHROPIC_API_KEY      = os.getenv("ANTHROPIC_API_KEY")
TELEGRAM_BOT_TOKEN     = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID       = os.getenv("TELEGRAM_CHAT_ID")
INSTAGRAM_ACCESS_TOKEN = os.getenv("INSTAGRAM_ACCESS_TOKEN")
INSTAGRAM_ACCOUNT_ID   = os.getenv("INSTAGRAM_ACCOUNT_ID")

# Kling AI Motion Control (workflow vidéo)
KLING_API_KEY    = os.getenv("KLINGAI_ACCESS_KEY")
KLING_API_SECRET = os.getenv("KLINGAI_SECRET_KEY")
KLING_MODEL      = os.getenv("KLINGAI_MODEL", "kling-v3")

# TikTok Content Posting API
TIKTOK_ACCESS_TOKEN = os.getenv("TIKTOK_ACCESS_TOKEN")
TIKTOK_OPEN_ID      = os.getenv("TIKTOK_OPEN_ID")

# ================================================================
# SYSTEM CONFIG
# ================================================================

POSTING_INTERVAL_DAYS = 1
MIN_DAYS_BETWEEN_RUNS = float(os.getenv("MIN_DAYS_BETWEEN_RUNS", str(POSTING_INTERVAL_DAYS)))
HISTORY_WINDOW_DAYS   = 30
LOG_PATH              = "logs/run.log"

# ----------------------------------------------------------------
# Modèles Gemini
# ⚠️  Vérifier la disponibilité des modèles sur :
#     https://ai.google.dev/models
# Les noms ci-dessous correspondent aux previews annoncées.
# Si un modèle n'est pas dispo, utiliser le fallback :
#   FALLBACK_IMAGE_MODEL = "gemini-1.5-pro"
# ----------------------------------------------------------------
GEMINI_MODEL_IMAGE           = "gemini-3-pro-image-preview"                        # Génération image native (responseModalities IMAGE) — stable
GEMINI_MODEL_IMAGE_PRO2      = "gemini-3-pro-image-preview"                        # Génération image native (même modèle)
GEMINI_MODEL_VISION          = "gemini-2.5-flash"                              # Analyse texte+vision (JSON, détection)
GEMINI_MODEL_FALLBACK        = "gemini-2.5-flash"                              # Fallback stable texte+vision
GEMINI_MODEL_INPAINTING      = "gemini-3-pro-image-preview"                        # Inpainting natif — workflow inpainting
# Modèles image alternatifs disponibles (vérifiés le 2026-03-27) :
# "gemini-3.1-flash-image-preview"  — plus récent, preview
# "gemini-3-pro-image-preview"      — fonctionne mais 500 INTERNAL intermittents

# ----------------------------------------------------------------
# Hébergement temporaire nginx (VPS)
# L'image générée est copiée ici pour être accessible par l'API Meta.
# ⚠️  Adapter ces deux valeurs lors du déploiement sur VPS.
# ----------------------------------------------------------------
NGINX_OUTPUT_DIR = os.getenv("NGINX_OUTPUT_DIR", "/var/www/influencer-bot/outputs")
NGINX_BASE_URL   = os.getenv("NGINX_BASE_URL",   "https://ton-domaine.com/outputs")

# ================================================================
# PATHS — ne pas modifier sauf restructuration intentionnelle
# ================================================================

DATA_DIR              = "data"
OUTPUTS_DIR           = "outputs"
VIDEOS_DIR            = f"{DATA_DIR}/videos"
TEMP_VIDEOS_DIR       = "temp/videos"         # Réservoir de vagues (v0-*, v1-*, ...) — même structure en local et sur VPS
VARIABLES_PATH        = f"{DATA_DIR}/variables.json"
HISTORY_PATH          = f"{DATA_DIR}/history.json"
CALENDAR_PATH         = f"{DATA_DIR}/calendar.json"
PENDING_STATE_PATH    = f"{DATA_DIR}/pending_state.json"
VIDEO_HISTORY_PATH    = f"{DATA_DIR}/video_history.json"
PAUSE_STATE_PATH      = f"{DATA_DIR}/pause_state.json"
