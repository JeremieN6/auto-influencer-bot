"""
frame_extractor.py — Extraction intelligente de frames depuis une vidéo via ffmpeg.

Rôle dans le pipeline Motion Control :
    La frame extraite est analysée par Gemini Vision pour extraire le JSON de scène
    (outfit, décor, éclairage). Elle n'est PAS envoyée à Kling directement.
    C'est l'image Madison générée à partir de ce JSON qui va dans Kling.

Fonctions disponibles :
    extract_best_frame(video_path)  — recommandée : scan multi-timestamps, score
                                                                        plusieurs frames avec personnage et retient
                                                                        la plus exploitable pour la cohérence visage.
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

from PIL import Image, ImageStat

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



def check_min_shot_duration(video_path: str, min_seconds: float = 3.0) -> bool:
    """
    Vérifie que la vidéo contient au moins un plan continu de `min_seconds` secondes.

    Kling Motion Control exige ≥3s de mouvement continu. Les vidéos Pinterest
    avec des cuts rapides (montage clip) sont rejetées avec :
    "The duration of continuous valid motion is too short; it should last at least 3 second."

    Méthode : détection de scènes par différence de pixels (select='gt(scene,0.35)').
    Contrairement aux I-frames (keyframes d'encodage insérées toutes les ~0.5s sans
    rapport avec les vrais changements de plan), cette approche détecte uniquement les
    vraies coupures visuelles.

    Args:
        video_path  : chemin vers le fichier vidéo
        min_seconds : durée minimale requise en secondes (défaut 3.0)

    Returns:
        True  → au moins un plan dure ≥ min_seconds (vidéo exploitable)
        False → tous les plans durent < min_seconds (rejeter cette vidéo)
    """
    ffmpeg_exe = _get_ffmpeg_exe()
    duration   = _get_video_duration(video_path)

    # Si la vidéo est trop courte en elle-même, inutile d'analyser
    if duration < min_seconds:
        logger.info(f"check_min_shot_duration : durée totale {duration:.1f}s < {min_seconds}s → REJET")
        return False

    # Détecter les coupures de scènes réelles par différence de pixels
    # select='gt(scene,0.35)' → frame sélectionnée si changement > 35% par rapport à la précédente
    # showinfo → écrit pts_time de chaque frame sélectionnée dans stderr
    cmd = [
        ffmpeg_exe,
        "-i", video_path,
        "-vf", "select=gt(scene\\,0.35),showinfo",
        "-vsync", "0",
        "-f", "null",
        "-",
    ]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
        output = result.stderr

        # pts_time des frames où une coupure a été détectée = timestamps de début de chaque plan
        cut_times = [float(m) for m in re.findall(r"pts_time:([\d.]+)", output)]

        if not cut_times:
            # Aucune coupure détectée → vidéo continue sur toute sa durée → OK
            logger.info(
                f"check_min_shot_duration : aucune coupure détectée sur {duration:.1f}s → OK"
            )
            return True

        # Construire la liste complète des bornes de plans : 0 + timestamps de cuts + durée totale
        boundaries = sorted(set([0.0] + cut_times + [duration]))
        intervals  = [boundaries[i + 1] - boundaries[i] for i in range(len(boundaries) - 1)]
        max_shot   = max(intervals)

        ok = max_shot >= min_seconds
        logger.info(
            f"check_min_shot_duration : {len(cut_times)} coupure(s) détectée(s), "
            f"plan max={max_shot:.2f}s (seuil={min_seconds}s) → {'OK' if ok else 'REJET'}"
        )
        return ok

    except Exception as e:
        logger.warning(f"check_min_shot_duration : erreur analyse ({e}) — supposé OK")
        return True  # en cas d'échec technique, ne pas bloquer le pipeline


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


def _measure_frame_metrics(image_path: str, pct: float) -> dict[str, float]:
    """
    Mesure des signaux locaux simples pour départager plusieurs frames valides.

    Objectif : privilégier une frame nette, correctement exposée, avec plus de
    détails dans la zone haut-centre où se trouve souvent le visage.
    """
    img = Image.open(image_path).convert("L")
    width, height = img.size

    # Taille réduite pour garder un calcul léger et stable.
    sample = img.resize((256, max(64, int(256 * height / max(width, 1)))), Image.Resampling.BILINEAR)
    pixels = sample.load()
    sample_w, sample_h = sample.size

    total_diff = 0.0
    total_count = 0
    for y in range(sample_h - 1):
        for x in range(sample_w - 1):
            total_diff += abs(pixels[x, y] - pixels[x + 1, y])
            total_diff += abs(pixels[x, y] - pixels[x, y + 1])
            total_count += 2
    full_sharpness = total_diff / max(total_count, 1)

    # Crop haut-centre : proxy du visage / tête / épaules.
    left = int(sample_w * 0.25)
    right = int(sample_w * 0.75)
    top = int(sample_h * 0.05)
    bottom = int(sample_h * 0.52)
    face_crop = sample.crop((left, top, right, bottom))
    face_pixels = face_crop.load()
    face_w, face_h = face_crop.size

    face_diff = 0.0
    face_count = 0
    for y in range(face_h - 1):
        for x in range(face_w - 1):
            face_diff += abs(face_pixels[x, y] - face_pixels[x + 1, y])
            face_diff += abs(face_pixels[x, y] - face_pixels[x, y + 1])
            face_count += 2
    face_sharpness = face_diff / max(face_count, 1)

    mean_brightness = float(ImageStat.Stat(sample).mean[0])
    exposure_score = max(0.0, 1.0 - abs(mean_brightness - 145.0) / 145.0)

    # Bonus modéré pour les frames ni trop tôt ni trop tard dans la vidéo.
    timing_score = max(0.0, 1.0 - abs(pct - 0.40) / 0.35)

    return {
        "full_sharpness": full_sharpness,
        "face_sharpness": face_sharpness,
        "exposure_score": exposure_score,
        "timing_score": timing_score,
    }


def _normalize_metric(values: list[float]) -> list[float]:
    """Normalise une liste dans [0, 1] pour comparer les candidates entre elles."""
    if not values:
        return []
    min_v = min(values)
    max_v = max(values)
    if abs(max_v - min_v) < 1e-9:
        return [1.0 for _ in values]
    return [(v - min_v) / (max_v - min_v) for v in values]


def _pick_best_person_candidate(candidates: list[dict]) -> dict:
    """
    Sélectionne la meilleure frame parmi plusieurs frames où un personnage est visible.

    Score conservateur : priorité à la netteté haut-centre, puis à la netteté
    globale, puis à l'exposition, avec un léger bonus temporel.
    """
    face_norm = _normalize_metric([c["metrics"]["face_sharpness"] for c in candidates])
    full_norm = _normalize_metric([c["metrics"]["full_sharpness"] for c in candidates])

    for idx, candidate in enumerate(candidates):
        metrics = candidate["metrics"]
        score = (
            0.50 * face_norm[idx]
            + 0.25 * full_norm[idx]
            + 0.15 * metrics["exposure_score"]
            + 0.10 * metrics["timing_score"]
        )
        candidate["score"] = score
        logger.info(
            "  Candidate frame %s%% : score=%.3f | face=%.2f | full=%.2f | exposure=%.2f | timing=%.2f",
            int(candidate["pct"] * 100),
            score,
            metrics["face_sharpness"],
            metrics["full_sharpness"],
            metrics["exposure_score"],
            metrics["timing_score"],
        )

    return max(candidates, key=lambda c: c["score"])


def extract_best_frame(video_path: str, output_path: str | None = None) -> str:
    """
     Extrait la meilleure frame d'une vidéo — optimisée pour une génération visage plus stable.

    Stratégie :
      1. Récupère la durée totale de la vidéo via ffprobe.
      2. Essaie successivement des frames à 15%, 30%, 50%, 70% de la durée.
        3. Pour chacune, appelle Gemini Vision (_detect_person_in_image) pour vérifier
            si un personnage est visible.
        4. Parmi les frames valides, calcule un score local orienté cohérence visage
            (netteté haut-centre, netteté globale, exposition, timing) et choisit la meilleure.
        5. Limite à _MAX_GEMINI_CALLS appels Gemini au total (économie de quota).
        6. Fallback : retourne la frame à 50% si aucun personnage n'est détecté.

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
    person_candidates: list[dict] = []

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
                person_candidates.append({
                    "pct": pct,
                    "ts": ts,
                    "path": temp,
                    "metrics": _measure_frame_metrics(temp, pct),
                })
        else:
            logger.debug(f"  Frame à {pct*100:.0f}% : quota Gemini atteint, skip détection")

    if person_candidates:
        best_candidate = _pick_best_person_candidate(person_candidates)
        best_path = best_candidate["path"]
        if os.path.abspath(best_path) != os.path.abspath(output_path):
            shutil.copy(best_path, output_path)
        _cleanup_scan_temps(temp_paths, keep=best_path if best_path == output_path else None)
        if best_path != output_path and os.path.exists(best_path):
            os.remove(best_path)
        logger.info(
            f"Frame retenue après scoring : {output_path} "
            f"(source {int(best_candidate['pct'] * 100)}% | score={best_candidate['score']:.3f})"
        )
        return output_path

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
