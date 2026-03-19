"""
kling_generator.py — Génération vidéo via Kling AI Motion Control API.

Technique : Motion Control (PAS image-to-video classique).
  - image_url : image Madison générée avec le bon outfit/décor (character)
  - video_url : vidéo source Pinterest/locale (motion reference)
  Kling transfère les mouvements de la vidéo source sur le personnage Madison.

API officielle :
  POST https://api-singapore.klingai.com/v1/videos/motion-control
  Réponse asynchrone — polling via task_id.

Authentification :
  JWT signé avec KLING_API_KEY (iss) + KLING_API_SECRET (secret HS256).

Modèles disponibles :
  - kling-v3   (recommandé — meilleure stabilité faciale)
  - kling-v2-6

Mode de transport des fichiers (auto-détecté) :
  VPS avec nginx  → NGINX_BASE_URL / NGINX_OUTPUT_DIR configurés → URL publique
  Local Windows   → image en Base64, vidéo uploadée sur file.io (temporaire)

Variables .env :
  KLINGAI_ACCESS_KEY=...
  KLINGAI_SECRET_KEY=...
  KLINGAI_MODEL=kling-v3     (optionnel)
  NGINX_BASE_URL=...         (optionnel — si non défini, mode local activé)

Utilisé par : workflow_video_local.py, workflow_video_pinterest.py
"""

import base64
import os
import shutil
import time
from datetime import datetime
from pathlib import Path

import jwt
import requests

from config import (
    KLING_API_KEY,
    KLING_API_SECRET,
    KLING_MODEL,
    NGINX_BASE_URL,
    NGINX_OUTPUT_DIR,
    OUTPUTS_DIR,
)
from frame_extractor import _get_video_duration
from logger import get_logger

logger = get_logger(__name__)

KLING_API_BASE         = "https://api-singapore.klingai.com/v1"
KLING_MOTION_ENDPOINT  = f"{KLING_API_BASE}/videos/motion-control"

# URL placeholder du default config → nginx non configuré
_NGINX_DEFAULT_PLACEHOLDER = "ton-domaine.com"

# Polling : attente que Kling ait fini de générer la vidéo
POLL_INTERVAL_S = 10   # secondes entre chaque vérification
POLL_MAX        = 72   # max 72 * 10s = 720s = 12 min

# Scènes → hints de mouvement pour le motion prompt
_MOTION_HINTS = {
    ("beach", "ocean", "sea", "wave"): "natural beach movement, hair flowing in the wind, relaxed and natural",
    ("pool", "water", "swim"):         "relaxed poolside movement, natural sunlight ambiance",
    ("mirror", "bedroom", "bathroom"): "confident mirror pose, subtle natural movement, casual and authentic",
    ("city", "rooftop", "urban", "street", "building"): "natural urban movement, confident city walk aesthetic",
    ("forest", "nature", "outdoor", "garden", "park"): "natural outdoor movement, light breeze, relaxed nature vibes",
    ("cafe", "coffee", "restaurant", "table"):          "relaxed seated movement, casual lifestyle energy",
    ("hotel", "room", "balcony", "terrace"):            "elegant relaxed movement, lifestyle luxury aesthetic",
}


# ================================================================
# Authentification JWT
# ================================================================

def _generate_auth_token() -> str:
    """Génère un JWT Bearer token pour l'authentification Kling AI."""
    if not KLING_API_KEY or not KLING_API_SECRET:
        raise ValueError(
            "KLING_API_KEY et KLING_API_SECRET sont requis.\n"
            "Ajouter ces variables dans .env avant d'utiliser Kling."
        )
    payload = {
        "iss": KLING_API_KEY,
        "exp": int(time.time()) + 1800,   # expire dans 30 min
        "nbf": int(time.time()) - 5,       # valide depuis maintenant - 5s (drift tolerance)
    }
    return jwt.encode(payload, KLING_API_SECRET, algorithm="HS256")


# ================================================================
# Détection du mode de transport
# ================================================================

def _nginx_is_configured() -> bool:
    """
    Retourne True si nginx est configuré (NGINX_BASE_URL pointe vers un vrai domaine).
    Si NGINX_BASE_URL contient encore le placeholder par défaut → mode local.
    """
    return _NGINX_DEFAULT_PLACEHOLDER not in NGINX_BASE_URL


# ================================================================
# Transport : nginx (VPS)
# ================================================================

def _expose_file_via_nginx(local_path: str) -> str:
    """
    Copie un fichier dans le dossier nginx et retourne son URL publique.
    Nécessite NGINX_BASE_URL et NGINX_OUTPUT_DIR configurés.
    """
    filename   = Path(local_path).name
    nginx_path = os.path.join(NGINX_OUTPUT_DIR, filename)
    public_url = f"{NGINX_BASE_URL}/{filename}"

    os.makedirs(NGINX_OUTPUT_DIR, exist_ok=True)

    if os.path.abspath(local_path) != os.path.abspath(nginx_path):
        shutil.copy(local_path, nginx_path)
        logger.debug(f"Fichier exposé via nginx : {nginx_path} → {public_url}")
    else:
        logger.debug(f"Fichier déjà dans nginx : {public_url}")

    return public_url


# ================================================================
# Transport : mode local (Base64 + file.io)
# ================================================================

def _image_to_base64(image_path: str) -> str:
    """
    Encode une image en Base64 brut (sans préfixe data:...).
    Kling API accepte ce format directement dans le champ image_url.
    """
    with open(image_path, "rb") as f:
        return base64.b64encode(f.read()).decode("utf-8")


def _upload_video_to_fileio(video_path: str) -> str:
    """
    Upload une vidéo sur tmpfiles.org et retourne l'URL publique directe.

    - Pas de compte requis, gratuit.
    - Fichier disponible pendant 24h (largement suffisant pour Kling).
    - Max ~1 Go par fichier.

    Args:
        video_path : chemin local vers la vidéo

    Returns:
        str : URL HTTPS publique de téléchargement direct

    Raises:
        RuntimeError : si l'upload échoue
    """
    filename  = Path(video_path).name
    file_size = os.path.getsize(video_path) / (1024 * 1024)
    logger.info(f"Upload vidéo locale vers tmpfiles.org : {filename} ({file_size:.1f} MB)...")

    session = requests.Session()
    session.headers.update({
        "User-Agent": "autoInsta/1.0 (+https://example.com)",
    })

    # Tentatives + backoff pour éviter les erreurs SSL transitoires
    max_attempts = 3
    backoff_base = 2

    for attempt in range(1, max_attempts + 1):
        try:
            with open(video_path, "rb") as f:
                resp = session.post(
                    "https://tmpfiles.org/api/v1/upload",
                    files={"file": (filename, f, "video/mp4")},
                    timeout=120,
                )

            if resp.status_code != 200:
                logger.warning(f"tmpfiles.org upload tentative {attempt} échoué ({resp.status_code})")
                raise RuntimeError(f"tmpfiles.org upload échoué ({resp.status_code}) : {resp.text[:200]}")

            try:
                data = resp.json()
            except Exception:
                raise RuntimeError(f"tmpfiles.org réponse non-JSON : {resp.text[:300]}")

            if data.get("status") != "success":
                raise RuntimeError(f"tmpfiles.org refus : {data}")

            raw_url = data["data"]["url"]
            dl_url  = raw_url.replace("http://tmpfiles.org/", "https://tmpfiles.org/dl/")
            logger.info(f"Vidéo uploadée sur tmpfiles.org : {dl_url}")
            return dl_url

        except requests.exceptions.SSLError as e:
            logger.warning(f"SSL error tmpfiles.org (attempt {attempt}): {e}")
        except (requests.exceptions.ConnectionError, RuntimeError) as e:
            logger.warning(f"tmpfiles.org erreur (attempt {attempt}): {e}")

        # Backoff avant retry
        if attempt < max_attempts:
            wait = backoff_base ** attempt
            logger.info(f"Pause {wait}s avant nouvelle tentative...")
            time.sleep(wait)

    # Si tmpfiles.org a échoué après retries, tenter un fallback via transfer.sh
    logger.info("tmpfiles.org a échoué après plusieurs tentatives — fallback: transfer.sh")
    try:
        with open(video_path, "rb") as f:
            resp = session.put(f"https://transfer.sh/{filename}", data=f, timeout=120)

        if resp.status_code in (200, 201):
            url = resp.text.strip()
            logger.info(f"Vidéo uploadée sur transfer.sh : {url}")
            return url
        else:
            raise RuntimeError(f"transfer.sh upload échoué ({resp.status_code}) : {resp.text[:200]}")
    except Exception as e:
        raise RuntimeError(f"Aucun service d'upload disponible : {e}")



# ================================================================
# Motion prompt builder
# ================================================================

def build_motion_prompt(scene_json: dict) -> str:
    """
    Génère un motion prompt optionnel pour Kling depuis le JSON de scène.
    Le mouvement principal vient de la vidéo source — ce prompt est un hint léger.

    Args:
        scene_json : dict retourné par image_to_json()

    Returns:
        str : motion prompt court (1-2 phrases max)
    """
    # Extraire description de la localisation
    location_desc = ""
    try:
        loc = scene_json.get("location", {})
        location_desc = (
            loc.get("description", "")
            or loc.get("place", "")
            or loc.get("setting", "")
            or ""
        ).lower()
    except Exception:
        pass

    for keywords, hint in _MOTION_HINTS.items():
        if any(kw in location_desc for kw in keywords):
            return hint

    return "natural fluid movement, authentic and relaxed lifestyle aesthetic"


# ================================================================
# Génération vidéo Motion Control
# ================================================================

def generate_video_motion_control(
    character_image_path: str,
    source_video_path: str,
    motion_prompt: str = "",
    character_orientation: str = "video",
    mode: str = "std",
) -> str:
    """
    Génère une vidéo Madison via Kling Motion Control.

    Args:
        character_image_path  : chemin local de l'image Madison générée
                                (outfit/décor extrait de la vidéo source)
        source_video_path     : chemin local de la vidéo source
                                (fournit les mouvements à transférer sur Madison)
        motion_prompt         : description optionnelle du mouvement souhaité
        character_orientation : "video" (max 30s, suit orientation vidéo source)
                                "image" (max 10s, suit orientation image)
        mode                  : "std" (standard, économique) | "pro" (haute qualité)

    Returns:
        str : chemin local de la vidéo générée (.mp4) dans outputs/

    Raises:
        ValueError   : si l'API Kling retourne une erreur ou que les credentials manquent
        RuntimeError : si le polling dépasse le timeout (12 min)

    Notes:
        - Image character : .jpg/.jpeg/.png, max 10MB, min 300px, ratio 2:5 à 5:2
        - Vidéo source    : .mp4/.mov, max 100MB, durée 3-30s
        - Statuts : queued → generating → succeed | failed
    """
    logger.info("=== Kling Motion Control démarré ===")
    logger.info(f"Character image : {character_image_path}")
    logger.info(f"Source video    : {source_video_path}")
    logger.info(f"Modèle          : {KLING_MODEL}")

    # ── Préparer les fichiers selon le mode de transport ─────────
    if _nginx_is_configured():
        # Mode VPS : exposer via nginx → URLs publiques
        logger.info("Mode transport : nginx (VPS)")
        image_payload_value = _expose_file_via_nginx(character_image_path)
        video_public_url    = _expose_file_via_nginx(source_video_path)
        logger.info(f"Image URL : {image_payload_value}")
        logger.info(f"Vidéo URL : {video_public_url}")
        use_image_base64 = False
    else:
        # Mode local : Base64 pour l'image, file.io pour la vidéo
        logger.info("Mode transport : local (nginx non configuré)")
        logger.info("  Image → Base64 (envoi direct sans URL publique)")
        image_payload_value = _image_to_base64(character_image_path)
        logger.info(f"  Base64 image encodé ({len(image_payload_value)} chars)")
        logger.info("  Vidéo → upload tmpfiles.org")
        video_public_url = _upload_video_to_fileio(source_video_path)
        use_image_base64 = True

    # ── Soumettre la tâche Motion Control ───────────────────────
    token   = _generate_auth_token()
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type":  "application/json",
    }

    # ── Calculer la durée Kling depuis la vidéo source ──────────
    # Kling supporte uniquement 5 ou 10 secondes.
    # On choisit la valeur la plus proche de la durée source.
    source_duration = 5.0
    try:
        source_duration = _get_video_duration(source_video_path)
        kling_duration  = 10 if abs(source_duration - 10) <= abs(source_duration - 5) else 5
    except Exception:
        kling_duration  = 5
    logger.info(f"Durée générée   : {kling_duration}s (source = {source_duration:.1f}s)")

    # Selon la doc Kling : image_url accepte une URL OU un Base64 brut (sans préfixe data:)
    payload: dict = {
        "model_name":            KLING_MODEL,
        "image_url":             image_payload_value,
        "video_url":             video_public_url,
        "character_orientation": character_orientation,
        "duration":              kling_duration,
        "mode":                  mode,
    }
    if motion_prompt:
        payload["prompt"] = motion_prompt
        logger.info(f"Motion prompt  : {motion_prompt}")

    logger.info("Soumission tâche Kling...")
    resp      = requests.post(KLING_MOTION_ENDPOINT, json=payload, headers=headers, timeout=30)
    resp_data = resp.json()

    if resp.status_code not in (200, 201):
        raise ValueError(f"Kling API erreur ({resp.status_code}) : {resp_data}")

    task_id = (
        resp_data.get("data", {}).get("task_id")
        or resp_data.get("task_id")
    )
    if not task_id:
        raise ValueError(f"Kling n'a pas retourné de task_id : {resp_data}")

    logger.info(f"Tâche soumise — task_id : {task_id}")

    # ── Polling statut ───────────────────────────────────────────
    poll_url = f"{KLING_MOTION_ENDPOINT}/{task_id}"

    for i in range(POLL_MAX):
        time.sleep(POLL_INTERVAL_S)

        # Renouveler le token périodiquement (toutes les 20 itérations ≈ 3.3 min)
        if i > 0 and i % 20 == 0:
            token = _generate_auth_token()
            headers["Authorization"] = f"Bearer {token}"

        poll_resp = requests.get(poll_url, headers=headers, timeout=15)
        poll_data = poll_resp.json()

        data    = poll_data.get("data", poll_data)
        status  = data.get("task_status") or data.get("status", "unknown")
        elapsed = (i + 1) * POLL_INTERVAL_S

        logger.debug(f"Polling [{i+1}/{POLL_MAX}] — statut : {status} ({elapsed}s)")

        if status == "succeed":
            # Extraire l'URL de la vidéo générée
            video_url: str | None = None
            try:
                videos = data.get("task_result", {}).get("videos", [])
                if videos:
                    video_url = videos[0].get("url")
            except Exception:
                pass

            if not video_url:
                raise ValueError(f"Tâche succeed mais URL vidéo absente : {data}")

            logger.info(f"Vidéo générée par Kling — URL : {video_url}")

            # Télécharger la vidéo dans outputs/
            filename   = f"video_kling_{datetime.now().strftime('%Y%m%d_%H%M%S')}.mp4"
            local_path = os.path.join(OUTPUTS_DIR, filename)
            os.makedirs(OUTPUTS_DIR, exist_ok=True)

            vid_resp = requests.get(video_url, timeout=120, stream=True)
            vid_resp.raise_for_status()
            with open(local_path, "wb") as f:
                for chunk in vid_resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            size_mb = os.path.getsize(local_path) / (1024 * 1024)
            logger.info(f"Vidéo téléchargée : {local_path} ({size_mb:.1f} MB)")
            logger.info("=== Kling Motion Control terminé ===")
            return local_path

        if status in ("failed", "error"):
            err_msg = data.get("task_status_msg") or data.get("error_message", "")
            raise ValueError(f"Kling tâche échouée (status={status}) : {err_msg or data}")

    raise RuntimeError(
        f"Kling Motion Control timeout après {POLL_MAX * POLL_INTERVAL_S}s "
        f"(task_id={task_id}). La vidéo est peut-être encore en cours de génération."
    )
