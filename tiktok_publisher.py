"""
tiktok_publisher.py — Publication vidéo sur TikTok via TikTok Content Posting API.

Documentation : https://developers.tiktok.com/doc/content-posting-api-get-started

Flux de publication (Direct Post) :
  1. Initialiser l'upload → obtenir un upload_url + publish_id
  2. Uploader la vidéo binaire via PUT sur l'upload_url
  3. Vérifier l'état via publish_id → polling jusqu'à success

Variables .env requises :
    TIKTOK_ACCESS_TOKEN=...     (OAuth2 Access Token avec scope video.upload)
    TIKTOK_OPEN_ID=...          (Open ID de l'utilisateur TikTok)

Notes :
  - L'access token expire (typiquement 24h). Prévoir un refresh token en V2.
  - La vidéo doit être .mp4 H.264, 720p+ recommandé, max 500MB.
  - La caption est limitée à 2200 caractères (TikTok) — troncature automatique si nécessaire.
"""

import os
import time

import requests

from config import TIKTOK_ACCESS_TOKEN, TIKTOK_OPEN_ID
from logger import get_logger

logger = get_logger(__name__)

TIKTOK_API_BASE       = "https://open.tiktokapis.com/v2"
CAPTION_MAX_LENGTH    = 2200   # limite TikTok

# Polling : attente que TikTok accepte la vidéo
POLL_INTERVAL_S = 5
POLL_MAX        = 24  # 24 * 5s = 120s max


# ================================================================
# Helpers
# ================================================================

def _check_credentials() -> None:
    """Vérifie que les credentials TikTok sont configurés."""
    if not TIKTOK_ACCESS_TOKEN or not TIKTOK_OPEN_ID:
        raise ValueError(
            "TIKTOK_ACCESS_TOKEN et TIKTOK_OPEN_ID sont requis.\n"
            "Configurer ces variables dans .env avant d'utiliser TikTok."
        )


def _truncate_caption(caption: str) -> str:
    """Tronque la caption si elle dépasse la limite TikTok."""
    if len(caption) > CAPTION_MAX_LENGTH:
        logger.warning(
            f"Caption tronquée : {len(caption)} → {CAPTION_MAX_LENGTH} caractères"
        )
        return caption[:CAPTION_MAX_LENGTH - 3] + "..."
    return caption


# ================================================================
# Publication principale
# ================================================================

def publish_video(video_path: str, caption: str) -> str:
    """
    Publie une vidéo sur TikTok via l'API Content Posting (Direct Post).

    Args:
        video_path : chemin local de la vidéo (.mp4)
        caption    : légende du post (max 2200 caractères)

    Returns:
        str : publish_id de la publication TikTok

    Raises:
        FileNotFoundError : si video_path n'existe pas
        ValueError        : si les credentials sont manquants ou si l'API retourne une erreur
        RuntimeError      : si le polling dépasse le timeout
    """
    _check_credentials()

    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Vidéo introuvable : {video_path}")

    caption       = _truncate_caption(caption)
    video_size    = os.path.getsize(video_path)
    headers_auth  = {
        "Authorization": f"Bearer {TIKTOK_ACCESS_TOKEN}",
        "Content-Type":  "application/json; charset=UTF-8",
    }

    logger.info("=== TikTok publish démarré ===")
    logger.info(f"Vidéo : {video_path} ({video_size / (1024*1024):.1f} MB)")
    logger.info(f"Caption : {caption[:80]}...")

    # ── Étape 1 : Initialiser l'upload ──────────────────────────
    logger.info("Étape 1/3 — Initialisation upload TikTok")
    init_payload = {
        "post_info": {
            "title":         caption,
            "privacy_level": "SELF_ONLY",   # Privé par défaut — changer en PUBLIC_TO_EVERYONE pour publier
            "disable_duet":        False,
            "disable_comment":     False,
            "disable_stitch":      False,
            "video_cover_timestamp_ms": 1000,
        },
        "source_info": {
            "source":          "FILE_UPLOAD",
            "video_size":      video_size,
            "chunk_size":      video_size,   # Upload en une seule partie
            "total_chunk_count": 1,
        },
    }

    init_resp = requests.post(
        f"{TIKTOK_API_BASE}/post/publish/video/init/",
        json=init_payload,
        headers=headers_auth,
        timeout=30,
    )
    init_data = init_resp.json()

    if init_resp.status_code not in (200, 201) or init_data.get("error", {}).get("code", "ok") != "ok":
        raise ValueError(f"TikTok init upload erreur ({init_resp.status_code}) : {init_data}")

    publish_id = init_data.get("data", {}).get("publish_id")
    upload_url = init_data.get("data", {}).get("upload_url")

    if not publish_id or not upload_url:
        raise ValueError(f"TikTok n'a pas retourné publish_id ou upload_url : {init_data}")

    logger.info(f"Upload initialisé — publish_id : {publish_id}")

    # ── Étape 2 : Upload de la vidéo ────────────────────────────
    logger.info("Étape 2/3 — Upload vidéo")

    with open(video_path, "rb") as f:
        video_bytes = f.read()

    upload_resp = requests.put(
        upload_url,
        data=video_bytes,
        headers={
            "Content-Type":  "video/mp4",
            "Content-Range": f"bytes 0-{video_size - 1}/{video_size}",
            "Content-Length": str(video_size),
        },
        timeout=120,
    )

    if upload_resp.status_code not in (200, 201, 204):
        raise ValueError(f"TikTok upload vidéo erreur ({upload_resp.status_code})")

    logger.info("Vidéo uploadée avec succès")

    # ── Étape 3 : Polling statut publication ─────────────────────
    logger.info("Étape 3/3 — Vérification statut publication")

    for i in range(POLL_MAX):
        time.sleep(POLL_INTERVAL_S)

        status_resp = requests.post(
            f"{TIKTOK_API_BASE}/post/publish/status/fetch/",
            json={"publish_id": publish_id},
            headers=headers_auth,
            timeout=15,
        )
        status_data = status_resp.json()

        status = (
            status_data.get("data", {}).get("status")
            or status_data.get("status", "unknown")
        )
        logger.debug(f"Polling TikTok [{i+1}/{POLL_MAX}] — statut : {status}")

        if status in ("PUBLISH_COMPLETE", "SUCCESS"):
            logger.info(f"Vidéo publiée sur TikTok ! publish_id : {publish_id}")
            logger.info("=== TikTok publish terminé ===")
            return publish_id

        if status in ("FAILED", "ERROR"):
            err = status_data.get("data", {}).get("fail_reason", "")
            raise ValueError(f"Publication TikTok échouée : {err or status_data}")

    raise RuntimeError(
        f"TikTok timeout après {POLL_MAX * POLL_INTERVAL_S}s "
        f"(publish_id={publish_id})"
    )
