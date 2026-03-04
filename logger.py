"""
logger.py — Logs détaillés pour debugging de pipeline.

Chaque étape du pipeline est loggée avec timestamp, module source et statut.
Objectif : identifier rapidement où le pipeline bloque.

Note : une fois le système stable en production, réduire level à WARNING.
"""

import logging
import os
from config import LOG_PATH


def setup_logger() -> None:
    """
    Initialise le logger global.
    À appeler une seule fois au démarrage de main.py ou telegram_bot.py.
    """
    os.makedirs(os.path.dirname(LOG_PATH), exist_ok=True)

    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # Formatter commun
    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)-25s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Handler fichier — tout en DEBUG
    if not any(isinstance(h, logging.FileHandler) for h in root_logger.handlers):
        fh = logging.FileHandler(LOG_PATH, encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        root_logger.addHandler(fh)

    # Handler console — DEBUG pendant développement
    if not any(isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler)
               for h in root_logger.handlers):
        ch = logging.StreamHandler()
        ch.setLevel(logging.DEBUG)
        ch.setFormatter(fmt)
        root_logger.addHandler(ch)


def get_logger(module: str) -> logging.Logger:
    """
    Retourne un logger nommé pour le module.

    Usage dans chaque module :
        from logger import get_logger
        logger = get_logger(__name__)
        logger.info("Recherche lancée : beach sunset golden hour")
        logger.debug("20 URLs récupérées en mémoire")
        logger.warning("Aucun personnage détecté — re-tirage")
        logger.error("Gemini n'a pas retourné d'image")
    """
    return logging.getLogger(module)


def log(level: str, module: str, message: str) -> None:
    """
    Helper fonctionnel — rétrocompatible avec les appels dans les autres modules.

    Usage :
        from logger import log
        log("info",  "pinterest_scraper", "Recherche lancée")
        log("debug", "pinterest_scraper", "20 URLs récupérées")
        log("warn",  "image_generator",   "Retry Gemini — tentative 2/3")
        log("error", "main",              "Erreur pipeline : ...")
    """
    logger = logging.getLogger(module)
    level_lower = level.lower()

    if level_lower in ("warn", "warning"):
        logger.warning(message)
    elif hasattr(logger, level_lower):
        getattr(logger, level_lower)(message)
    else:
        logger.info(message)


# ================================================================
# Séparateurs visuels pour les logs — lisibilité pipeline
# ================================================================

def log_section(module: str, title: str) -> None:
    """Affiche un séparateur de section dans les logs."""
    border = "=" * 60
    get_logger(module).info(f"\n{border}\n  {title}\n{border}")


def log_step(module: str, step: int, total: int, description: str) -> None:
    """Affiche l'avancement d'une étape numérotée."""
    get_logger(module).info(f"[{step}/{total}] {description}")
