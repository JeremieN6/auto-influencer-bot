"""
frame_extractor.py — Extraction intelligente de frames depuis une vidéo via ffmpeg.

Rôle dans le pipeline Motion Control :
  La frame extraite est analysée par Gemini Vision pour extraire le JSON de scène
  (outfit, décor, éclairage). Elle n'est PAS envoyée à Kling directement.
  C'est l'image Madison générée à partir de ce JSON qui va dans Kling.

Fonctions disponibles :
  extract_best_frame(video_path)  — recommandée : scan multi-timestamps, retourne
                                    la première frame avec un personnage détecté.
  extract_first_frame(video_path) — utilitaire : extrait uniquement la frame 1.

Résolution ffmpeg (par ordre de priorité) :
  1. Binaire ffmpeg dans le PATH système (Linux/macOS/Windows avec PATH configuré)
  2. Binaire embarqué par imageio-ffmpeg (cross-platform, fonctionne sur Windows sans install)

  Sur Linux/VPS : sudo apt install ffmpeg   (recommandé pour la prod)
  Sur Windows   : rien à faire — imageio-ffmpeg fournit le binaire automatiquement

Utilisé par : workflow_video_local.py, workflow_video_pinterest.py
"""

import os
import re
import shutil
import subprocess
from pathlib import Path

from config import OUTPUTS_DIR
from logger import get_logger

logger = get_logger(__name__)

# Timestamps relatifs à essayer (% de la durée totale) pour la détection personnage
_SCAN_PERCENTAGES = [0.15, 0.30, 0.50, 0.70]
# Nombre max d'appels Gemini pour détecter un personnage (économie de quota)
_MAX_GEMINI_CALLS = 3


def _get_ffmpeg_exe() -> str:
    """
    Retourne le chemin vers l'exécutable ffmpeg.

    Priorité :
      1. ffmpeg dans le PATH système
      2. Binaire embarqué imageio-ffmpeg (disponible via pip, cross-platform)

    Raises:
        RuntimeError : si aucune source n'est disponible
    """
    # 1. ffmpeg dans le PATH
    if shutil.which("ffmpeg"):
        return "ffmpeg"

    # 2. Binaire embarqué imageio-ffmpeg
    try:
        import imageio_ffmpeg
        exe = imageio_ffmpeg.get_ffmpeg_exe()
        logger.debug(f"ffmpeg système absent — utilisation imageio-ffmpeg : {exe}")
        return exe
    except ImportError:
        pass
    except Exception as e:
        logger.warning(f"imageio-ffmpeg disponible mais erreur : {e}")

    raise RuntimeError(
        "ffmpeg introuvable.\n"
        "  Option 1 (recommandée) : pip install imageio-ffmpeg\n"
        "  Option 2 (Linux/VPS)   : sudo apt install ffmpeg\n"
        "  Option 3 (macOS)       : brew install ffmpeg\n"
        "  Option 4 (Windows)     : https://ffmpeg.org/download.html (ajouter au PATH)"
    )


def _get_video_duration(video_path: str) -> float:
    """
    Retourne la durée de la vidéo en secondes via ffprobe.
    Fallback 10.0s si ffprobe échoue.
    """
    ffmpeg_exe = _get_ffmpeg_exe()
    # ffprobe est souvent fourni avec ffmpeg ; on tente via ffmpeg -i et on parse stderr
    cmd = [ffmpeg_exe, "-i", video_path]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=15)
        # ffmpeg -i sans output écrit les infos dans stderr
        output = result.stderr
        # Chercher "Duration: HH:MM:SS.xx"
        match = re.search(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)", output)
        if match:
            h, m, s = int(match.group(1)), int(match.group(2)), float(match.group(3))
            duration = h * 3600 + m * 60 + s
            logger.debug(f"Durée vidéo : {duration:.1f}s")
            return duration
    except Exception as e:
        logger.warning(f"Impossible de lire la durée vidéo : {e}")
    return 10.0  # fallback


def _extract_frame_at(video_path: str, timestamp: float, output_path: str) -> bool:
    """
    Extrait une frame à un timestamp précis (secondes).
    Retourne True si succès, False sinon.
    """
    ffmpeg_exe = _get_ffmpeg_exe()
    cmd = [
        ffmpeg_exe,
        "-ss", str(timestamp),
        "-i", video_path,
        "-vframes", "1",
        "-q:v", "2",
        "-y",
        output_path,
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        return result.returncode == 0 and os.path.exists(output_path)
    except Exception as e:
        logger.warning(f"Extraction frame à {timestamp:.1f}s échouée : {e}")
        return False


def extract_best_frame(video_path: str, output_path: str | None = None) -> str:
    """
    Extrait la meilleure frame d'une vidéo — celle qui contient un personnage humain.

    Stratégie :
      1. Récupère la durée totale de la vidéo via ffprobe.
      2. Essaie successivement des frames à 15%, 30%, 50%, 70% de la durée.
      3. Pour chacune, appelle Gemini Vision (_detect_person_in_image) pour vérifier
         si un personnage est visible. S'arrête dès qu'un personnage est trouvé.
      4. Limite à _MAX_GEMINI_CALLS appels Gemini au total (économie de quota).
      5. Fallback : retourne la frame à 50% si aucun personnage n'est détecté.

    Args:
        video_path  : chemin local vers la vidéo (.mp4, .mov, .webm)
        output_path : chemin de sortie pour la frame finale.
                      Si None → outputs/<stem>_frame.jpg

    Returns:
        str : chemin local de la frame extraite

    Raises:
        FileNotFoundError : si video_path n'existe pas
        RuntimeError      : si ffmpeg n'est pas disponible
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Vidéo introuvable : {video_path}")

    stem = Path(video_path).stem
    os.makedirs(OUTPUTS_DIR, exist_ok=True)

    if output_path is None:
        output_path = os.path.join(OUTPUTS_DIR, f"{stem}_frame.jpg")

    logger.info(f"Extraction intelligente frame : {Path(video_path).name}")

    duration = _get_video_duration(video_path)

    # Import ici pour éviter les imports circulaires
    from pinterest_scraper import _detect_person_in_image

    gemini_calls = 0
    fallback_path: str | None = None
    temp_paths: list[str] = []

    for pct in _SCAN_PERCENTAGES:
        ts = max(0.5, duration * pct)
        temp = os.path.join(OUTPUTS_DIR, f"{stem}_scan_{int(pct*100)}.jpg")
        temp_paths.append(temp)

        ok = _extract_frame_at(video_path, ts, temp)
        if not ok:
            logger.debug(f"  Frame à {pct*100:.0f}% ({ts:.1f}s) : échec extraction")
            continue

        # Garder la frame à 50% comme fallback au cas où
        if abs(pct - 0.50) < 0.05:
            fallback_path = temp

        if gemini_calls < _MAX_GEMINI_CALLS:
            gemini_calls += 1
            has_person = _detect_person_in_image(temp)
            logger.info(f"  Frame à {pct*100:.0f}% ({ts:.1f}s) : personnage={'OUI' if has_person else 'NON'} (appel {gemini_calls}/{_MAX_GEMINI_CALLS})")

            if has_person:
                # Copier vers output_path final, nettoyer les autres temporaires
                if os.path.abspath(temp) != os.path.abspath(output_path):
                    shutil.copy(temp, output_path)
                _cleanup_scan_temps(temp_paths, keep=temp if temp == output_path else None)
                if temp != output_path and os.path.exists(temp):
                    os.remove(temp)
                logger.info(f"Frame retenue (personnage détecté à {pct*100:.0f}%) : {output_path}")
                return output_path
        else:
            logger.debug(f"  Frame à {pct*100:.0f}% : quota Gemini atteint, skip détection")

    # Fallback : utiliser la frame à 50% (ou la première frame si extraction 50% a échoué)
    fallback = fallback_path or (temp_paths[0] if temp_paths else None)
    if fallback and os.path.exists(fallback):
        if os.path.abspath(fallback) != os.path.abspath(output_path):
            shutil.copy(fallback, output_path)
        _cleanup_scan_temps(temp_paths, keep=None)
        logger.info(f"Frame fallback (aucun personnage détecté) : {output_path}")
        return output_path

    # Dernier recours : frame 1
    logger.warning("Scan intelligent échoué — extraction frame 1 en fallback")
    return extract_first_frame(video_path, output_path)


def _cleanup_scan_temps(paths: list[str], keep: str | None = None) -> None:
    """Supprime les fichiers temporaires de scan, sauf 'keep'."""
    for p in paths:
        if p and p != keep and os.path.exists(p):
            try:
                os.remove(p)
            except OSError:
                pass


def extract_first_frame(video_path: str, output_path: str | None = None) -> str:
    """
    Extrait la première frame d'une vidéo et la sauvegarde en JPEG.

    Args:
        video_path  : chemin local vers la vidéo source (.mp4, .mov, .webm)
        output_path : chemin de sortie optionnel.
                      Si None → outputs/<stem>_frame.jpg

    Returns:
        str : chemin local de la frame extraite

    Raises:
        FileNotFoundError : si video_path n'existe pas
        RuntimeError      : si ffmpeg échoue ou n'est pas disponible
    """
    if not os.path.exists(video_path):
        raise FileNotFoundError(f"Vidéo introuvable : {video_path}")

    if output_path is None:
        stem = Path(video_path).stem
        os.makedirs(OUTPUTS_DIR, exist_ok=True)
        output_path = os.path.join(OUTPUTS_DIR, f"{stem}_frame.jpg")

    logger.info(f"Extraction frame 1 : {Path(video_path).name} → {Path(output_path).name}")

    ffmpeg_exe = _get_ffmpeg_exe()

    cmd = [
        ffmpeg_exe,
        "-i", video_path,
        "-vframes", "1",
        "-q:v", "2",
        "-y",            # overwrite sans confirmation
        output_path,
    ]

    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(f"ffmpeg timeout après 30s sur : {video_path}")

    if result.returncode != 0:
        raise RuntimeError(
            f"ffmpeg a échoué (code {result.returncode}) :\n{result.stderr[-500:]}"
        )

    if not os.path.exists(output_path):
        raise RuntimeError(
            f"ffmpeg n'a pas créé le fichier de sortie : {output_path}\n"
            f"stderr : {result.stderr[-300:]}"
        )

    size = os.path.getsize(output_path)
    logger.info(f"Frame extraite : {output_path} ({size} bytes)")
    return output_path
