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

from telegram import Bot, InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)

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
# Commande /run — ConversationHandler multi-étapes
# ================================================================

# États de la conversation
(
    RUN_WORKFLOW,
    RUN_MODE,
    RUN_LOCATION,
    RUN_OUTFIT,
    RUN_MOOD,
    RUN_POSE,
    RUN_LIGHTING,
) = range(7)


def _load_variables() -> dict:
    """Charge variables.json pour construire les claviers de choix."""
    import json
    from pathlib import Path
    path = Path(__file__).parent / "data" / "variables.json"
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def _make_keyboard(options: list[str], cols: int = 2) -> InlineKeyboardMarkup:
    """Crée un InlineKeyboardMarkup depuis une liste d'options."""
    rows = [options[i:i + cols] for i in range(0, len(options), cols)]
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton(o, callback_data=o) for o in row] for row in rows]
    )


async def cmd_run(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Lancement manuel avec choix workflow et paramètres — /run"""
    if not _is_authorized(update):
        return ConversationHandler.END

    keyboard = _make_keyboard(["pinterest", "generatif"], cols=2)
    await update.message.reply_text(
        "🎬 *Lancement manuel — /run*\n\nQuel workflow ?",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return RUN_WORKFLOW


async def run_choose_workflow(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Étape 1 : workflow choisi → demander mode."""
    query = update.callback_query
    await query.answer()
    ctx.user_data["run_workflow"] = query.data
    ctx.user_data["run_override"] = {}

    keyboard = _make_keyboard(["aléatoire", "manuel"], cols=2)
    await query.edit_message_text(
        f"Workflow : *{query.data}*\n\nMode ?",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return RUN_MODE


async def run_choose_mode(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Étape 2 : mode choisi → lancer directement ou poser les questions."""
    query = update.callback_query
    await query.answer()

    if query.data == "aléatoire":
        await query.edit_message_text(
            f"✅ Workflow *{ctx.user_data['run_workflow']}* — mode aléatoire\n\n"
            "🚀 Pipeline lancé\. Résultat dans quelques minutes\."  ,
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        await _launch_run_pipeline(ctx)
        return ConversationHandler.END

    # Mode manuel → demander la location
    variables = _load_variables()
    keyboard = _make_keyboard(variables["locations"])
    await query.edit_message_text(
        "📍 *Lieu ?*",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return RUN_LOCATION


async def run_choose_location(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    ctx.user_data["run_override"]["location"] = query.data

    variables = _load_variables()
    keyboard = _make_keyboard(variables["outfits"])
    await query.edit_message_text(
        f"📍 Lieu : *{query.data}*\n\n👗 *Tenue ?*",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return RUN_OUTFIT


async def run_choose_outfit(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    ctx.user_data["run_override"]["outfit"] = query.data

    variables = _load_variables()
    keyboard = _make_keyboard(variables["moods"])
    await query.edit_message_text(
        f"👗 Tenue : *{query.data}*\n\n😊 *Mood ?*",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return RUN_MOOD


async def run_choose_mood(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    ctx.user_data["run_override"]["mood"] = query.data

    variables = _load_variables()
    keyboard = _make_keyboard(variables["poses"])
    await query.edit_message_text(
        f"😊 Mood : *{query.data}*\n\n🧍 *Pose ?*",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return RUN_POSE


async def run_choose_pose(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    ctx.user_data["run_override"]["pose"] = query.data

    variables = _load_variables()
    keyboard = _make_keyboard(variables["lighting"])
    await query.edit_message_text(
        f"🧍 Pose : *{query.data}*\n\n💡 *Lumière ?*",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return RUN_LIGHTING


async def run_choose_lighting(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    await query.answer()
    ctx.user_data["run_override"]["lighting"] = query.data

    override = ctx.user_data["run_override"]
    workflow = ctx.user_data["run_workflow"]

    summary = (
        f"✅ *Concept confirmé*\n\n"
        f"Workflow : `{workflow}`\n"
        f"📍 Lieu      : {override['location']}\n"
        f"👗 Tenue     : {override['outfit']}\n"
        f"😊 Mood      : {override['mood']}\n"
        f"🧍 Pose      : {override['pose']}\n"
        f"💡 Lumière   : {override['lighting']}\n\n"
        "🚀 Pipeline lancé\. Résultat dans quelques minutes\."
    )
    await query.edit_message_text(summary, parse_mode=ParseMode.MARKDOWN_V2)
    await _launch_run_pipeline(ctx)
    return ConversationHandler.END


async def run_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Annuler la conversation /run en cours."""
    ctx.user_data.clear()
    await update.message.reply_text("❌ /run annulé.")
    return ConversationHandler.END


async def _launch_run_pipeline(ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Lance le pipeline via subprocess en passant les override_params
    dans un fichier JSON temporaire.
    IMPORTANT : n'affecte pas history.json ni le calendrier éditorial
    (persist=False géré par --no-persist dans main.py).
    """
    import json
    import tempfile

    workflow       = ctx.user_data.get("run_workflow", "pinterest")
    override_params = ctx.user_data.get("run_override") or {}

    # Écrire les override_params dans un fichier temporaire
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(override_params, f, ensure_ascii=False)
        params_path = f.name

    python_exe  = sys.executable
    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")

    cmd = [python_exe, script_path, "--workflow", workflow, "--override-params", params_path]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    logger.info(f"/run pipeline lancé — PID {proc.pid} | workflow={workflow} | override={override_params}")


def _build_run_handler() -> ConversationHandler:
    """Construit et retourne le ConversationHandler pour /run."""
    return ConversationHandler(
        entry_points=[CommandHandler("run", cmd_run)],
        states={
            RUN_WORKFLOW: [CallbackQueryHandler(run_choose_workflow)],
            RUN_MODE:     [CallbackQueryHandler(run_choose_mode)],
            RUN_LOCATION: [CallbackQueryHandler(run_choose_location)],
            RUN_OUTFIT:   [CallbackQueryHandler(run_choose_outfit)],
            RUN_MOOD:     [CallbackQueryHandler(run_choose_mood)],
            RUN_POSE:     [CallbackQueryHandler(run_choose_pose)],
            RUN_LIGHTING: [CallbackQueryHandler(run_choose_lighting)],
        },
        fallbacks=[CommandHandler("cancel", run_cancel)],
        per_message=False,
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
    app.add_handler(_build_run_handler())

    logger.info("Handlers enregistrés — démarrage du polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    start_bot()
