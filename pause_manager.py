"""
pause_manager.py — petit utilitaire pour gérer une pause globale du pipeline.

Fonctionnalités minimales :
- `set_paused(True|False, reason, by)` : active/désactive la pause (persistée dans data/pause_state.json)
- `is_paused()` : bool
- `get_pause_info()` : dict avec les métadonnées si présent

Le fichier de pause est défini par `config.PAUSE_STATE_PATH`.
"""
from __future__ import annotations

import json
import os
from datetime import datetime
from typing import Optional

from config import PAUSE_STATE_PATH


def _ensure_parent_dir() -> None:
    os.makedirs(os.path.dirname(PAUSE_STATE_PATH), exist_ok=True)


def set_paused(paused: bool, reason: Optional[str] = None, by: Optional[str] = None) -> None:
    """Active ou désactive la pause globale.

    Si `paused` est True, écrit un fichier JSON avec metadata.
    Si False, supprime le fichier (retour à l'état normal).
    """
    _ensure_parent_dir()
    if paused:
        info = {
            "paused": True,
            "since": datetime.now().isoformat(),
            "reason": reason or "",
            "by": by or "",
        }
        with open(PAUSE_STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2, ensure_ascii=False)
    else:
        try:
            os.remove(PAUSE_STATE_PATH)
        except FileNotFoundError:
            pass


def is_paused() -> bool:
    """Retourne True si le fichier de pause existe et indique paused=True."""
    try:
        with open(PAUSE_STATE_PATH, encoding="utf-8") as f:
            info = json.load(f)
        return bool(info.get("paused"))
    except Exception:
        return False


def get_pause_info() -> dict:
    """Retourne les métadonnées de pause si présentes, sinon {"paused": False}.
    Useful for show status/notifications.
    """
    try:
        with open(PAUSE_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"paused": False}
