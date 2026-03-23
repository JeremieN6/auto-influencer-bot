"""
video_batch_manager.py — Gestion automatique des vagues de vidéos locales.

Rôle :
  - Vérifie si /data/videos/ est vide
  - Si vide, prend le prochain bloc v[0-9]* depuis /temp/videos/
  - Renomme les vidéos proprement selon la convention
  - Déplace dans /data/videos/

Convention de nommage :
  Entrée  : n'importe quel nom (souvent illisible)
  Sortie  : video_[NN]_[keyword].mp4
  Exemple : video_01_beach.mp4, video_02_cosplay.mp4

Modes de détection des batches dans /temp/videos/ :
  1. Fichiers préfixés v[0-9]-  (ex: v2-From KlickPin CF ...)  ← mode principal
  2. Sous-dossiers dont le nom commence par v[0-9]  (ex: /temp/videos/v2/)  ← fallback

Appelé automatiquement par main.py quand /data/videos/ devient vide.
"""

import re
import shutil
from pathlib import Path

from logger import get_logger

logger = get_logger(__name__)

DATA_VIDEOS_DIR = Path(__file__).parent / "data" / "videos"
TEMP_VIDEOS_DIR = Path(__file__).parent / "temp" / "videos"

# Mots à exclure lors de l'extraction du mot-clé depuis le nom de fichier
_STOPWORDS = {
    "from", "klickpin", "klick", "klik", "pin", "pins", "board",
    "the", "and", "for", "with", "this", "that", "vous", "pour",
    "video", "vídeo", "vidéo", "insta", "instagram", "tiktok",
    "sur", "von", "pinin", "your", "youre",
    "di", "en", "on", "by", "in", "at", "cf", "save", "follow", "more",
    "2026", "2025", "2024", "into", "here", "click", "get",
    "watch", "full", "link", "collection",
}


# ================================================================
# Helpers internes
# ================================================================

def _get_batch_prefixes() -> list[str]:
    """
    Retourne les préfixes vN distincts trouvés sur les fichiers .mp4
    dans /temp/videos/ (ex: "v2", "v3"...).
    Mode principal — les fichiers sont tous plats dans /temp/videos/.
    """
    if not TEMP_VIDEOS_DIR.exists():
        return []
    prefixes: set[str] = set()
    for f in TEMP_VIDEOS_DIR.iterdir():
        if f.is_file() and f.suffix.lower() == ".mp4":
            m = re.match(r'^(v\d+)-', f.name, re.IGNORECASE)
            if m:
                prefixes.add(m.group(1).lower())
    return sorted(prefixes)


def _get_batch_dirs() -> list[Path]:
    """
    Fallback : retourne les sous-dossiers de /temp/videos/ dont le nom commence par v[0-9].
    Non utilisé dans la configuration actuelle (fichiers plats).
    """
    if not TEMP_VIDEOS_DIR.exists():
        return []
    return sorted(
        [p for p in TEMP_VIDEOS_DIR.iterdir()
         if p.is_dir() and re.match(r'^v\d', p.name, re.IGNORECASE)],
        key=lambda p: p.name.lower(),
    )


def _extract_keyword(filename: str) -> str:
    """
    Extrait un mot-clé descriptif depuis un nom de fichier.
    Stratégie : premier mot alphabétique > 3 lettres, hors stopwords.
    """
    # Supprimer les crochets, parenthèses, URLs
    clean = re.sub(r'\[.*?\]|\(.*?\)|https?://\S+|©|®|™', '', filename, flags=re.IGNORECASE)
    # Supprimer les émojis et ponctuation
    clean = re.sub(r'[^\w\s]', ' ', clean)
    words = clean.split()

    candidates = [
        w.lower() for w in words
        if len(w) > 3
        and w.lower() not in _STOPWORDS
        and w.isalpha()
        and not re.match(r'^v\d', w)
    ]
    return candidates[0] if candidates else "clip"


# ================================================================
# API publique
# ================================================================

def has_local_videos() -> bool:
    """
    Retourne True si /data/videos/ contient au moins un fichier .mp4.
    """
    if not DATA_VIDEOS_DIR.exists():
        return False
    return any(DATA_VIDEOS_DIR.glob("*.mp4"))


def get_next_batch() -> str | None:
    """
    Retourne le nom du prochain batch disponible dans /temp/videos/.

    Cherche d'abord les préfixes v[0-9]- sur les fichiers plats (mode principal),
    puis les sous-dossiers v[0-9]* (fallback).
    Tri alphabétique → v2 avant v3 avant v4...
    Retourne None si aucun batch disponible.
    """
    prefixes = _get_batch_prefixes()
    if prefixes:
        return prefixes[0]

    dirs = _get_batch_dirs()
    return dirs[0].name if dirs else None


def transfer_batch(batch_name: str) -> list[str]:
    """
    Transfère un bloc de vidéos depuis /temp/videos/ vers /data/videos/.

    Supporte deux modes :
    - Préfixe fichier : fichiers nommés {batch_name}-* dans /temp/videos/  ← principal
    - Sous-dossier   : /temp/videos/{batch_name}/                           ← fallback

    Renomme chaque vidéo en video_NN_keyword.mp4.
    Retourne la liste des chemins destination.
    """
    DATA_VIDEOS_DIR.mkdir(parents=True, exist_ok=True)

    # ── Mode principal : fichiers plats préfixés ─────────────────
    prefix_lower = batch_name.lower()
    source_files = sorted([
        f for f in TEMP_VIDEOS_DIR.iterdir()
        if f.is_file()
        and f.suffix.lower() == ".mp4"
        and f.name.lower().startswith(f"{prefix_lower}-")
    ])

    if source_files:
        logger.info(f"Batch '{batch_name}' — mode préfixe ({len(source_files)} fichiers)")
    else:
        # ── Fallback : sous-dossier ───────────────────────────────
        batch_dir = TEMP_VIDEOS_DIR / batch_name
        if batch_dir.is_dir():
            source_files = sorted(batch_dir.glob("*.mp4"))
            logger.info(f"Batch '{batch_name}' — mode sous-dossier ({len(list(source_files))} fichiers)")
        else:
            logger.warning(f"Aucun fichier .mp4 trouvé pour le batch '{batch_name}'")
            return []

    if not source_files:
        logger.warning(f"Aucun fichier .mp4 trouvé pour le batch '{batch_name}'")
        return []

    destinations: list[str] = []
    for idx, src in enumerate(source_files, start=1):
        keyword = _extract_keyword(src.stem)
        dest_name = f"video_{idx:02d}_{keyword}.mp4"
        dest_path = DATA_VIDEOS_DIR / dest_name

        # Éviter les collisions de noms
        if dest_path.exists():
            dest_name = f"video_{idx:02d}_{keyword}_{batch_name}.mp4"
            dest_path = DATA_VIDEOS_DIR / dest_name

        shutil.move(str(src), str(dest_path))
        logger.info(f"  {src.name}  →  {dest_name}")
        destinations.append(str(dest_path))

    # Supprimer le sous-dossier source s'il est maintenant vide (mode fallback)
    batch_dir = TEMP_VIDEOS_DIR / batch_name
    if batch_dir.is_dir() and not any(batch_dir.iterdir()):
        batch_dir.rmdir()
        logger.info(f"Dossier batch vide supprimé : {batch_dir}")

    return destinations


def auto_refill_if_empty() -> bool:
    """
    Point d'entrée appelé par main.py après chaque vidéo locale traitée.
    Si /data/videos/ est vide → détecte le prochain batch → transfert.

    Retourne True si un batch a été transféré, False sinon.
    """
    if has_local_videos():
        return False

    next_batch = get_next_batch()
    if next_batch is None:
        logger.info("auto_refill : aucun batch disponible dans /temp/videos/")
        return False

    logger.info(f"auto_refill : /data/videos/ vide — transfert batch '{next_batch}'")
    transferred = transfer_batch(next_batch)
    if transferred:
        logger.info(f"auto_refill : {len(transferred)} vidéo(s) transférée(s) dans /data/videos/")
        return True
    return False
