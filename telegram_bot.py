"""
telegram_bot.py — Interface Telegram : commandes V1 + scaffold V2.

ARCHITECTURE :
  Ce module joue deux rôles distincts :

  1. Fonction send_for_validation() — appelée depuis main.py (process cron)
     → Envoie l'image + caption sur Telegram pour validation humaine
     → Sauvegarde l'état dans data/pending_state.json (partage inter-process)

  2. Service bot (systemd) — lancé via `python telegram_bot.py`
     → Polling Telegram API
     → Gère les commandes utilisateur : /status /validate /modify /generate /schedule
     → Lit pending_state depuis data/pending_state.json

COMMANDES V1 :
  /start     → message d'accueil
  /status    → état du système + prochain post schedulé
  /validate  → approuver et publier sur Instagram
  /modify    → régénérer l'image avec une instruction supplémentaire (/modify instruction)
  /generate  → forcer un nouveau concept aléatoire (déclenche le pipeline)
  /schedule  → afficher le calendrier des 4 prochains posts

COMMANDES V2 (scaffold) :
  /run       → lancement manuel avec choix workflow + paramètres

Prérequis : TELEGRAM_BOT_TOKEN et TELEGRAM_CHAT_ID dans .env
"""

import asyncio
import json
import os
import subprocess
import sys

from telegram import Bot, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, ContextTypes

from config import (
    INFLUENCER_NAME,
    PENDING_STATE_PATH,
    TELEGRAM_BOT_TOKEN,
    TELEGRAM_CHAT_ID,
)
from logger import get_logger, setup_logger

logger = get_logger(__name__)


# ================================================================
# État partagé (inter-process via JSON)
# ================================================================

def save_pending_state(state: dict) -> None:
    """Persiste le pending_state dans data/pending_state.json."""
    os.makedirs(os.path.dirname(PENDING_STATE_PATH), exist_ok=True)
    with open(PENDING_STATE_PATH, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    logger.debug(f"pending_state sauvegardé : {PENDING_STATE_PATH}")


def load_pending_state() -> dict:
    """Charge le pending_state depuis data/pending_state.json."""
    try:
        with open(PENDING_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return _empty_state()
    except json.JSONDecodeError:
        logger.error("pending_state.json corrompu — réinitialisation")
        return _empty_state()


def clear_pending_state() -> None:
    """Réinitialise le pending_state après publication."""
    save_pending_state(_empty_state())
    logger.info("pending_state réinitialisé")


def _empty_state() -> dict:
    return {
        "image_path":     None,
        "public_url":     None,
        "caption":        None,
        "concept":        None,
        "step":           None,
        "last_prompt":    None,
        "image_filename": None,
    }


# ================================================================
# Fonction publique — appelée depuis main.py
# ================================================================

async def send_for_validation(image_path: str, caption: str) -> None:
    """
    Envoie l'image générée + caption sur Telegram pour validation humaine.
    Sauvegarde l'état dans pending_state.json.

    Appelée depuis main.py via asyncio.run(send_for_validation(...)).
    Le pending_state doit être mis à jour par main.py AVANT d'appeler cette fonction.
    """
    logger.info(f"Envoi Telegram pour validation : {image_path}")

    text = (
        f"📸 *Nouveau post {INFLUENCER_NAME}* — en attente de validation\n\n"
        f"*Caption :*\n{caption}\n\n"
        f"──────────────────\n"
        f"✅ /validate — Publier sur Instagram\n"
        f"✏️  /modify \\[instruction\\] — Régénérer avec modification\n"
        f"🔄 /generate — Nouveau concept aléatoire\n"
        f"📅 /schedule — Voir le calendrier"
    )

    async with Bot(token=TELEGRAM_BOT_TOKEN) as bot:
        with open(image_path, "rb") as photo:
            await bot.send_photo(
                chat_id=TELEGRAM_CHAT_ID,
                photo=photo,
                caption=text,
                parse_mode=ParseMode.MARKDOWN_V2,
            )

    logger.info("Message Telegram envoyé avec succès")


# ================================================================
# Helpers commandes
# ================================================================

def _is_authorized(update: Update) -> bool:
    """Vérifie que la commande vient du bon chat."""
    return str(update.effective_chat.id) == str(TELEGRAM_CHAT_ID)


async def _send_error(update: Update, msg: str) -> None:
    await update.message.reply_text(f"❌ {msg}")


# ================================================================
# Commandes V1
# ================================================================

async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Accueil — /start"""
    if not _is_authorized(update):
        return
    text = (
        f"👋 Bonjour ! Je suis le bot de gestion de *{INFLUENCER_NAME}*.\n\n"
        f"*Commandes disponibles :*\n"
        f"/status — État du système\n"
        f"/validate — Publier l'image en attente\n"
        f"/modify \\[instruction\\] — Régénérer avec modification\n"
        f"/generate — Nouveau concept aléatoire\n"
        f"/schedule — Calendrier des 4 prochains posts\n"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """État du système — /status"""
    if not _is_authorized(update):
        return

    from concept_generator import get_schedule_preview, load_history
    from config import POSTING_INTERVAL_DAYS
    from datetime import datetime, timedelta

    state   = load_pending_state()
    history = load_history()

    # Prochain post auto
    last_run = None
    if history:
        last_entry = history[-1]
        try:
            last_run = datetime.fromisoformat(last_entry.get("generated_at", ""))
        except Exception:
            pass

    if last_run:
        next_run = last_run + timedelta(days=POSTING_INTERVAL_DAYS)
        delta    = next_run - datetime.now()
        if delta.total_seconds() > 0:
            days_left = delta.days
            next_str  = f"dans {days_left} jour(s) — {next_run.strftime('%d/%m/%Y %H:%M')}"
        else:
            next_str = "🔴 En retard — lancer manuellement /generate"
    else:
        next_str = "Aucun post enregistré — premier run à venir"

    has_pending = bool(state.get("image_path"))
    pending_str = (
        f"✅ Image en attente de validation\n"
        f"   Caption : {str(state.get('caption', ''))[:80]}..."
        if has_pending else "⚪ Aucune image en attente"
    )

    text = (
        f"📊 *Status {INFLUENCER_NAME}*\n\n"
        f"{pending_str}\n\n"
        f"📅 Prochain post auto : {next_str}\n"
        f"📈 Total posts historique : {len(history)}"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN_V2)


async def cmd_validate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Publier sur Instagram — /validate"""
    if not _is_authorized(update):
        return

    state = load_pending_state()
    if not state.get("image_path") or not state.get("public_url"):
        await _send_error(update, "Aucune image en attente de validation.")
        return

    await update.message.reply_text("⏳ Publication en cours sur Instagram...")

    try:
        from instagram_publisher import publish_post
        result = publish_post(
            public_url=state["public_url"],
            caption=state["caption"],
            image_filename=state["image_filename"],
        )
        media_id = result.get("id", "?")
        clear_pending_state()
        await update.message.reply_text(
            f"✅ *Publié sur Instagram\\!*\nMedia ID : `{media_id}`",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        logger.info(f"Publication Instagram validée — media_id={media_id}")

    except Exception as e:
        logger.error(f"Erreur publication Instagram : {e}")
        await _send_error(update, f"Erreur publication Instagram :\n{e}")


async def cmd_modify(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Régénérer l'image avec une instruction — /modify [instruction]"""
    if not _is_authorized(update):
        return

    state = load_pending_state()
    if not state.get("last_prompt"):
        await _send_error(update, "Aucun post en cours à modifier.")
        return

    # Extraire l'instruction depuis le message
    text_parts = update.message.text.split(maxsplit=1)
    if len(text_parts) < 2 or not text_parts[1].strip():
        await update.message.reply_text(
            "Usage : /modify <instruction>\n"
            "Exemple : /modify rendre la lumière plus chaude, ton soleil couchant"
        )
        return

    instruction = text_parts[1].strip()
    logger.info(f"Modification demandée : '{instruction}'")

    await update.message.reply_text(f"🎨 Régénération en cours avec : \"{instruction}\"...")

    try:
        from image_generator import generate_image

        # Ajouter l'instruction au prompt existant
        modified_prompt = state["last_prompt"] + f"\n\nModification demandée : {instruction}"

        local_path, public_url = generate_image(modified_prompt)
        filename = os.path.basename(local_path)

        # Mettre à jour le pending_state
        state.update({
            "image_path":     local_path,
            "public_url":     public_url,
            "image_filename": filename,
            "last_prompt":    modified_prompt,
        })
        save_pending_state(state)

        # Renvoyer sur Telegram pour validation
        caption = state.get("caption", "")
        await send_for_validation(local_path, caption)
        await update.message.reply_text("✅ Image régénérée — voir ci-dessus pour validation.")

    except Exception as e:
        logger.error(f"Erreur régénération image : {e}")
        await _send_error(update, f"Erreur régénération : {e}")


async def cmd_generate(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Forcer un nouveau concept aléatoire — /generate"""
    if not _is_authorized(update):
        return

    await update.message.reply_text(
        "🔄 Déclenchement d'un nouveau concept aléatoire...\n"
        "Le pipeline complet va tourner. Résultat dans quelques minutes."
    )

    try:
        # Déclencher le pipeline en subprocess (séparation de process)
        python_exe = sys.executable
        script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
        proc = subprocess.Popen(
            [python_exe, script_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        logger.info(f"Pipeline déclenché via subprocess — PID {proc.pid}")
        await update.message.reply_text(
            f"🚀 Pipeline démarré \\(PID {proc.pid}\\)\\. "
            f"Vous recevrez l'image ici pour validation\\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )

    except Exception as e:
        logger.error(f"Erreur déclenchement pipeline : {e}")
        await _send_error(update, f"Impossible de déclencher le pipeline : {e}")


async def cmd_schedule(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Afficher le calendrier des 4 prochains posts — /schedule"""
    if not _is_authorized(update):
        return

    try:
        from concept_generator import get_schedule_preview
        preview = get_schedule_preview(n=4)

        lines = [f"📅 *Calendrier éditorial — 4 prochains posts*\n"]
        for item in preview:
            hashtag_icon = "🏷️" if item["hashtags"] else "🚫"
            lines.append(
                f"*Step {item['step']}* — {item['eta']}\n"
                f"  Format : `{item['format']}` | Type : {item['type']} | Hashtags : {hashtag_icon}\n"
                f"  _{item['note']}_\n"
            )
        await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.MARKDOWN_V2)

    except Exception as e:
        logger.error(f"Erreur commande /schedule : {e}")
        await _send_error(update, f"Erreur schedule : {e}")


# ================================================================
# Commande V2 — /run (scaffold)
# ================================================================
# TODO V2 :
# Implémenter une ConversationHandler multi-étapes pour :
#   1. Demander le workflow (Pinterest / Génératif)
#   2. Demander mode (aléatoire / manuel)
#   3. Si manuel → questions successives (lieu, tenue, mood, pose, lighting)
#      avec options depuis variables.json + "Autre (saisie libre)"
#   4. Pour chaque "Autre" → appeler validate_custom_input()
#   5. Déclencher le pipeline avec override_params (persist=False)
#
# IMPORTANT : /run ne modifie PAS history.json ni le calendrier éditorial.

async def cmd_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """[V2] Lancement manuel avec choix workflow et paramètres — /run"""
    if not _is_authorized(update):
        return
    await update.message.reply_text(
        "⚙️ La commande /run sera disponible en V2\\.\n\n"
        "En attendant, utilisez /generate pour un concept aléatoire automatique\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )


# ================================================================
# Démarrage du service bot (point d'entrée systemd)
# ================================================================

def start_bot() -> None:
    """
    Lance le bot Telegram en mode polling.
    À appeler directement : python telegram_bot.py
    Configuré comme service systemd sur le VPS.
    """
    setup_logger()
    logger.info(f"=== Telegram Bot {INFLUENCER_NAME} démarré ===")

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",    cmd_start))
    app.add_handler(CommandHandler("status",   cmd_status))
    app.add_handler(CommandHandler("validate", cmd_validate))
    app.add_handler(CommandHandler("modify",   cmd_modify))
    app.add_handler(CommandHandler("generate", cmd_generate))
    app.add_handler(CommandHandler("schedule", cmd_schedule))
    app.add_handler(CommandHandler("run",      cmd_run))   # V2 scaffold

    logger.info("Handlers enregistrés — démarrage du polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    start_bot()
