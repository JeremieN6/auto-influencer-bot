"""
instagram_publisher.py — Publication sur Instagram via Meta Graph API.

Responsabilités :
- publish_post()    : publication en 2 étapes (création container + publish)
- L'image est servie depuis le VPS via nginx (URL publique HTTPS)
- Suppression de l'image du dossier nginx après publication réussie

Prérequis :
- Access Token Instagram Long-Lived (valable 60 jours depuis Meta for Developers)
- Compte Instagram Professionnel ou Créateur lié à une Page Facebook
- Image accessible publiquement via NGINX_BASE_URL (https, port 443)

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
CONTAINER_POLL_INTERVAL = 5   # secondes entre chaque vérification
CONTAINER_MAX_POLLS     = 12  # max 12 * 5s = 60s d'attente


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
            "fields":       "status_code",
            "access_token": INSTAGRAM_ACCESS_TOKEN,
        },
        timeout=15,
    )
    data = resp.json()
    return data.get("status_code", "UNKNOWN")


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
# NOTE V2 — publication autonome
# ================================================================
# Pour activer la publication sans /validate Telegram :
#   1. Dans main.py, retirer l'appel à send_for_validation()
#   2. Appeler directement publish_post(public_url, caption, filename)
#   3. Envoyer seulement une notification de confirmation à Telegram
#
# Activer uniquement quand la qualité des posts a été validée manuellement
# pendant plusieurs cycles.
