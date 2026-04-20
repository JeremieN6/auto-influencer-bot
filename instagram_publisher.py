"""
instagram_publisher.py — Publication sur Instagram via Meta Graph API.

Responsabilités :
- publish_post()         : publication image (feed) en 2 étapes
- publish_reel()         : publication vidéo Reel (media_type=REELS)
- publish_story_video()  : publication vidéo Story (media_type=STORIES)
- L'image/vidéo est servie depuis le VPS via nginx (URL publique HTTPS)
- Suppression du fichier nginx après publication réussie

Prérequis :
- Access Token Instagram Long-Lived (valable 60 jours depuis Meta for Developers)
- Compte Instagram Professionnel ou Créateur lié à une Page Facebook
- Fichier accessible publiquement via NGINX_BASE_URL (https, port 443)

Note V2 : publication autonome (sans /validate Telegram)
  → Activer en retirant la validation Telegram dans main.py quand l'automatisation est validée.
"""

import time

import requests

from config import INSTAGRAM_ACCESS_TOKEN, INSTAGRAM_ACCOUNT_ID
from logger import get_logger

logger = get_logger(__name__)

META_API_VERSION = "v19.0"
META_API_BASE    = f"https://graph.facebook.com/{META_API_VERSION}"

# Polling : attente que le container soit prêt (statuts Meta)
CONTAINER_POLL_INTERVAL = 5    # secondes entre chaque vérification
CONTAINER_MAX_POLLS     = 12   # max 12 * 5s = 60s d'attente — images
REEL_MAX_POLLS          = 60   # max 60 * 10s = 600s = 10 min — vidéos (encoding plus long)
REEL_POLL_INTERVAL      = 10   # secondes entre chaque vérif Reel


# ================================================================
# Helpers
# ================================================================

def _check_container_status(creation_id: str) -> str:
    """
    Interroge Meta pour vérifier le statut de traitement du container.
    Retourne le statut : FINISHED | IN_PROGRESS | ERROR | EXPIRED | PUBLISHED
    """
    resp = requests.get(
        f"{META_API_BASE}/{creation_id}",
        params={
            "fields":       "status_code,processing_progress,video_status,errors",
            "access_token": INSTAGRAM_ACCESS_TOKEN,
        },
        timeout=15,
    )
    try:
        data = resp.json()
    except Exception:
        logger.error(f"Impossible de parser la réponse container {creation_id} : status={resp.status_code}")
        return "UNKNOWN"

    # Log brut pour faciliter le debug en cas d'erreur côté Meta
    logger.debug(f"Container status response ({creation_id}): {data}")

    # Prioriser le champ 'status_code' si présent, sinon essayer d'autres clés
    return data.get("status_code") or data.get("status") or data.get("video_status") or "UNKNOWN"


def _validate_media_url(url: str, expected_type: str = "video") -> bool:
    """
    Vérifie rapidement que l'URL publique est accessible et semble contenir
    le bon type média (video|image). Retourne True si OK, False sinon.
    Effectue un HEAD puis un GET bref en fallback si nécessaire.
    """
    try:
        r = requests.head(url, allow_redirects=True, timeout=10)
    except Exception as e:
        logger.error(f"Erreur connexion à l'URL média {url} : {e}")
        return False

    if r.status_code != 200:
        logger.error(f"URL média inaccessible ({r.status_code}) : {url}")
        return False

    ct = r.headers.get("Content-Type", "")
    if expected_type == "video":
        if "video" in ct:
            return True
        # Certains serveurs ne répondent pas correctement aux HEAD : tester avec GET bref
        logger.warning(f"Content-Type HEAD inattendu pour vidéo : {ct} — tentative GET")
        try:
            r2 = requests.get(url, stream=True, timeout=10)
            ct2 = r2.headers.get("Content-Type", "")
            r2.close()
            if "video" in ct2:
                return True
            logger.error(f"Content-Type GET inattendu pour vidéo : {ct2}")
            return False
        except Exception as e:
            logger.error(f"Erreur GET rapide pour vérifier URL vidéo {url} : {e}")
            return False

    else:  # image
        if "image" in ct:
            return True
        logger.warning(f"Content-Type HEAD inattendu pour image : {ct} — tentative GET")
        try:
            r2 = requests.get(url, stream=True, timeout=10)
            ct2 = r2.headers.get("Content-Type", "")
            r2.close()
            if "image" in ct2:
                return True
            logger.error(f"Content-Type GET inattendu pour image : {ct2}")
            return False
        except Exception as e:
            logger.error(f"Erreur GET rapide pour vérifier URL image {url} : {e}")
            return False


def _wait_for_container(creation_id: str) -> bool:
    """
    Attend que Meta ait fini de traiter l'image (fetch + processing).
    Retourne True si FINISHED, False si timeout ou ERROR.
    """
    logger.info(f"Attente traitement container {creation_id}...")
    for i in range(CONTAINER_MAX_POLLS):
        status = _check_container_status(creation_id)
        logger.debug(f"Container status [{i+1}/{CONTAINER_MAX_POLLS}] : {status}")

        if status == "FINISHED":
            logger.info("Container prêt — publication possible")
            return True
        if status in ("ERROR", "EXPIRED"):
            logger.error(f"Container en erreur : status={status}")
            return False

        time.sleep(CONTAINER_POLL_INTERVAL)

    logger.error("Timeout : container non prêt après 60s")
    return False


# ================================================================
# Publication principale
# ================================================================

def publish_post(public_url: str, caption: str, image_filename: str) -> dict:
    """
    Publie une image sur Instagram via Meta Graph API.

    Flux :
      1. Créer un container média (Meta fetch l'image via l'URL nginx)
      2. Attendre que le container soit traité (polling)
      3. Publier le container
      4. Nettoyer nginx après publication réussie

    Args:
        public_url      : URL HTTPS de l'image (ex: https://ton-domaine.com/outputs/pending_xxx.jpg)
        caption         : texte de la caption Instagram (+ hashtags si applicable)
        image_filename  : nom du fichier local pour nettoyage nginx (ex: pending_xxx.jpg)

    Returns:
        dict : réponse Meta API {"id": "..."} si succès

    Raises:
        ValueError : si création du container ou publication échoue
    """
    logger.info(f"=== Instagram publish démarré ===")
    logger.info(f"URL image  : {public_url}")
    logger.info(f"Caption    : {caption[:80]}...")

    # ── Étape 1 : créer le container média ──────────────────────
    logger.info("Étape 1/3 — Création container média")
    # Vérification préliminaire : URL accessible et content-type valide
    if not _validate_media_url(public_url, expected_type="image"):
        raise ValueError(f"URL image inaccessible ou content-type invalide : {public_url}")
    r1 = requests.post(
        f"{META_API_BASE}/{INSTAGRAM_ACCOUNT_ID}/media",
        data={
            "image_url":    public_url,
            "caption":      caption,
            "access_token": INSTAGRAM_ACCESS_TOKEN,
        },
        timeout=30,
    )

    r1_data     = r1.json()
    creation_id = r1_data.get("id")

    if not creation_id:
        logger.error(f"Erreur création container : {r1_data}")
        raise ValueError(f"Erreur création container Instagram : {r1_data}")

    logger.info(f"Container créé : {creation_id}")

    # ── Étape 2 : attendre que Meta ait traité l'image ──────────
    logger.info("Étape 2/3 — Attente traitement Meta")
    container_ready = _wait_for_container(creation_id)
    if not container_ready:
        raise ValueError(f"Container {creation_id} non prêt pour publication")

    # ── Étape 3 : publier ────────────────────────────────────────
    logger.info("Étape 3/3 — Publication")
    r2 = requests.post(
        f"{META_API_BASE}/{INSTAGRAM_ACCOUNT_ID}/media_publish",
        data={
            "creation_id":  creation_id,
            "access_token": INSTAGRAM_ACCESS_TOKEN,
        },
        timeout=30,
    )

    r2_data = r2.json()
    if "id" not in r2_data:
        logger.error(f"Erreur publication : {r2_data}")
        raise ValueError(f"Erreur publication Instagram : {r2_data}")

    logger.info(f"Post publié ! Instagram media ID : {r2_data['id']}")

    # ── Nettoyage nginx après publication réussie ────────────────
    from image_generator import cleanup_nginx
    cleanup_nginx(image_filename)

    logger.info("=== Instagram publish terminé avec succès ===")
    return r2_data


# ================================================================
# Publication Reel (vidéo)
# ================================================================

def publish_reel(video_url: str, caption: str, video_filename: str) -> dict:
    """
    Publie une vidéo en Reel Instagram via Meta Graph API.

    Flux :
      1. Créer un container REELS (Meta fetch la vidéo via l'URL nginx)
      2. Polling plus long que pour les images (encoding vidéo Meta)
      3. Publier le container
      4. Nettoyer nginx après publication réussie

    Args:
        video_url      : URL HTTPS de la vidéo (servie depuis nginx)
        caption        : texte de la caption Reel
        video_filename : nom du fichier local pour nettoyage nginx

    Returns:
        dict : réponse Meta API {"id": "..."} si succès

    Raises:
        ValueError : si création du container ou publication échoue
    """
    logger.info("=== Instagram Reel publish démarré ===")
    logger.info(f"URL vidéo : {video_url}")
    logger.info(f"Caption   : {caption[:80]}...")

    # ── Étape 1 : créer le container Reel ───────────────────────
    logger.info("Étape 1/3 — Création container REELS")
    # Vérification préliminaire : URL accessible et content-type vidéo
    if not _validate_media_url(video_url, expected_type="video"):
        raise ValueError(f"URL vidéo inaccessible ou content-type invalide : {video_url}")
    r1 = requests.post(
        f"{META_API_BASE}/{INSTAGRAM_ACCOUNT_ID}/media",
        data={
            "media_type":   "REELS",
            "video_url":    video_url,
            "caption":      caption,
            "share_to_feed": True,
            "access_token": INSTAGRAM_ACCESS_TOKEN,
        },
        timeout=30,
    )

    r1_data     = r1.json()
    creation_id = r1_data.get("id")

    if not creation_id:
        logger.error(f"Erreur création container Reel : {r1_data}")
        raise ValueError(f"Erreur création container Reel Instagram : {r1_data}")

    logger.info(f"Container Reel créé : {creation_id}")

    # ── Étape 2 : attendre que Meta ait encodé la vidéo ─────────
    # L'encoding vidéo est plus long que le simple fetch image
    logger.info("Étape 2/3 — Attente encoding vidéo Meta")
    logger.info(f"Attente container Reel {creation_id} (polling {REEL_MAX_POLLS}×{REEL_POLL_INTERVAL}s)...")

    for i in range(REEL_MAX_POLLS):
        status = _check_container_status(creation_id)
        logger.debug(f"Reel container status [{i+1}/{REEL_MAX_POLLS}] : {status}")

        if status == "FINISHED":
            logger.info("Container Reel prêt — publication possible")
            break
        if status in ("ERROR", "EXPIRED"):
            raise ValueError(f"Container Reel en erreur : status={status}")

        time.sleep(REEL_POLL_INTERVAL)
    else:
        raise ValueError(f"Container Reel {creation_id} non prêt après {REEL_MAX_POLLS * REEL_POLL_INTERVAL}s")

    # ── Étape 3 : publier ────────────────────────────────────────
    logger.info("Étape 3/3 — Publication Reel")
    r2 = requests.post(
        f"{META_API_BASE}/{INSTAGRAM_ACCOUNT_ID}/media_publish",
        data={
            "creation_id":  creation_id,
            "access_token": INSTAGRAM_ACCESS_TOKEN,
        },
        timeout=30,
    )

    r2_data = r2.json()
    if "id" not in r2_data:
        logger.error(f"Erreur publication Reel : {r2_data}")
        raise ValueError(f"Erreur publication Reel Instagram : {r2_data}")

    logger.info(f"Reel publié ! Instagram media ID : {r2_data['id']}")

    # ── Nettoyage nginx ──────────────────────────────────────────
    from image_generator import cleanup_nginx
    cleanup_nginx(video_filename)

    logger.info("=== Instagram Reel publish terminé avec succès ===")
    return r2_data


# ================================================================
# Publication Story vidéo
# ================================================================

def publish_story_video(video_url: str, video_filename: str) -> dict:
    """
    Publie une vidéo en Story Instagram via Meta Graph API.

    Args:
        video_url      : URL HTTPS de la vidéo (servie depuis nginx)
        video_filename : nom du fichier local pour nettoyage nginx

    Returns:
        dict : réponse Meta API {"id": "..."} si succès

    Raises:
        ValueError : si création du container ou publication échoue
    """
    logger.info("=== Instagram Story vidéo publish démarré ===")
    logger.info(f"URL vidéo : {video_url}")

    # ── Étape 1 : créer le container Story ──────────────────────
    logger.info("Étape 1/3 — Création container STORIES (vidéo)")
    # Vérification préliminaire : URL accessible et content-type vidéo
    if not _validate_media_url(video_url, expected_type="video"):
        raise ValueError(f"URL vidéo inaccessible ou content-type invalide : {video_url}")
    r1 = requests.post(
        f"{META_API_BASE}/{INSTAGRAM_ACCOUNT_ID}/media",
        data={
            "media_type":   "STORIES",
            "video_url":    video_url,
            "access_token": INSTAGRAM_ACCESS_TOKEN,
        },
        timeout=30,
    )

    r1_data     = r1.json()
    creation_id = r1_data.get("id")

    if not creation_id:
        logger.error(f"Erreur création container Story : {r1_data}")
        raise ValueError(f"Erreur création container Story Instagram : {r1_data}")

    logger.info(f"Container Story créé : {creation_id}")

    # ── Étape 2 : attendre encoding ─────────────────────────────
    logger.info("Étape 2/3 — Attente encoding vidéo Meta (Story)")

    for i in range(REEL_MAX_POLLS):
        status = _check_container_status(creation_id)
        logger.debug(f"Story container status [{i+1}/{REEL_MAX_POLLS}] : {status}")

        if status == "FINISHED":
            logger.info("Container Story prêt — publication possible")
            break
        if status in ("ERROR", "EXPIRED"):
            raise ValueError(f"Container Story en erreur : status={status}")

        time.sleep(REEL_POLL_INTERVAL)
    else:
        raise ValueError(f"Container Story {creation_id} non prêt après {REEL_MAX_POLLS * REEL_POLL_INTERVAL}s")

    # ── Étape 3 : publier ────────────────────────────────────────
    logger.info("Étape 3/3 — Publication Story")
    r2 = requests.post(
        f"{META_API_BASE}/{INSTAGRAM_ACCOUNT_ID}/media_publish",
        data={
            "creation_id":  creation_id,
            "access_token": INSTAGRAM_ACCESS_TOKEN,
        },
        timeout=30,
    )

    r2_data = r2.json()
    if "id" not in r2_data:
        logger.error(f"Erreur publication Story : {r2_data}")
        raise ValueError(f"Erreur publication Story Instagram : {r2_data}")

    logger.info(f"Story publiée ! Instagram media ID : {r2_data['id']}")

    # ── Nettoyage nginx ──────────────────────────────────────────
    from image_generator import cleanup_nginx
    cleanup_nginx(video_filename)

    logger.info("=== Instagram Story vidéo publish terminé avec succès ===")
    return r2_data



# Pour activer la publication sans /validate Telegram :
#   1. Dans main.py, retirer l'appel à send_for_validation()
#   2. Appeler directement publish_post(public_url, caption, filename)
#   3. Envoyer seulement une notification de confirmation à Telegram
#
# Activer uniquement quand la qualité des posts a été validée manuellement
# pendant plusieurs cycles.
