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


def _escape_md(text: str) -> str:
    """Échappe les caractères spéciaux MarkdownV2 dans du contenu dynamique."""
    import re
    return re.sub(r'([_*\[\]()~`>#+\-=|{}.!\\])', r'\\\1', str(text))


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
        # —— état image (existant) ——
        "image_path":     None,
        "public_url":     None,
        "caption":        None,
        "concept":        None,
        "step":           None,
        "last_prompt":    None,
        "image_filename": None,
        "wildcard_used":  None,
        # —— état vidéo (V3) ——
        "media_type":       "image",   # "image" | "video"
        "video_path":       None,
        "video_public_url": None,
        "video_filename":   None,
        "video_type":       None,      # "reel" | "story"
        # —— état intermédiaire (Kling en attente/échoué) ——
        "_intermediate":      False,
        "madison_image_path": None,
        "source_video_path":  None,
        "scene_json":         None,
    }


# ================================================================
# Fonction publique — appelée depuis main.py
# ================================================================

async def send_for_validation(
    image_path: str,
    caption: str,
    wildcard_used: str | None = None,
    content_type: str = "feed",
    destination: str = "instagram",
) -> None:
    """
    Envoie l'image générée + caption sur Telegram pour validation humaine.
    Sauvegarde l'état dans pending_state.json.

    Appelée depuis main.py via asyncio.run(send_for_validation(...)).
    Le pending_state doit être mis à jour par main.py AVANT d'appeler cette fonction.
    """
    logger.info(f"Envoi Telegram pour validation : {image_path}")

    _TYPE_LABELS = {"feed": "🖼️ Feed", "story": "📱 Story", "reel": "🎬 Reel"}
    _DEST_LABELS = {"instagram": "Instagram", "tiktok": "TikTok", "both": "Instagram \\+ TikTok"}
    type_label = _TYPE_LABELS.get(content_type, content_type)
    dest_label = _DEST_LABELS.get(destination, _escape_md(destination))

    wildcard_line = (
        f"🎲 *Élément surprise :* _{_escape_md(wildcard_used)}_\n"
        if wildcard_used else ""
    )

    text = (
        f"📸 *Nouveau post {_escape_md(INFLUENCER_NAME)}* — en attente de validation\n\n"
        f"{wildcard_line}"
        f"🗂 Type : {type_label} \\| 📤 Destination : {dest_label}\n\n"
        f"*Caption :*\n{_escape_md(caption)}\n\n"
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


async def send_video_for_validation(video_path: str, caption: str, video_type: str) -> None:
    """
    Envoie la vidéo générée + caption sur Telegram avec les boutons de publication.
    Sauvegarde l'état dans pending_state.json.

    Args:
        video_path : chemin local de la vidéo
        caption    : caption générée
        video_type : "reel" (personnage + Motion Control) ou "story" (ambiance)
    """
    logger.info(f"Envoi Telegram vidéo pour validation : {video_path} | type={video_type}")

    if video_type == "reel":
        type_label = "Reel"
        dest_label = "Instagram Reels / TikTok"
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("📸 Instagram Reels",  callback_data="pub_video_reel"),
                InlineKeyboardButton("🎵 TikTok",             callback_data="pub_video_tiktok"),
            ],
            [
                InlineKeyboardButton("📸🎵 Les deux",          callback_data="pub_video_both"),
                InlineKeyboardButton("❌ Ignorer",            callback_data="pub_video_ignore"),
            ],
        ])
    else:  # story
        type_label = "Story"
        dest_label = "Instagram"
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ Publier Story Instagram", callback_data="pub_video_story"),
                InlineKeyboardButton("❌ Ignorer",               callback_data="pub_video_ignore"),
            ],
        ])

    text = (
        f"🎬 *Nouveau {type_label} {_escape_md(INFLUENCER_NAME)}* \u2014 en attente de validation\n\n"
        f"🗂 Type : 🎬 {type_label} \\| 📤 Destination : {_escape_md(dest_label)}\n\n"
        f"*Caption :*\n{_escape_md(caption)}"
    )

    async with Bot(token=TELEGRAM_BOT_TOKEN) as bot:
        with open(video_path, "rb") as vid:
            await bot.send_video(
                chat_id=TELEGRAM_CHAT_ID,
                video=vid,
                caption=text,
                reply_markup=keyboard,
                parse_mode=ParseMode.MARKDOWN_V2,
            )

    logger.info("Message vidéo Telegram envoyé avec succès")


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
        f"👋 Bonjour \\! Je suis le bot de gestion de *{INFLUENCER_NAME}*\\.\n\n"
        f"*Commandes disponibles :*\n"
        f"/status — État du système\n"
        f"/validate — Publier l'image en attente\n"
        f"/modify \\[instruction\\] — Régénérer avec modification\n"
        f"/generate — Nouveau concept aléatoire\n"
        f"/schedule — Calendrier des 4 prochains posts\n"
        f"/manualGeneration — Générer depuis une image ou vidéo source\n"
        f"/retryKling — Relancer Kling si la dernière vidéo a échoué\n"
        f"/run — Lancement manuel avancé\n"
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

    if state.get("_intermediate"):
        madison_name = _escape_md(os.path.basename(state.get("madison_image_path") or "?"))
        pending_str = (
            f"\u26a0\ufe0f *Pipeline interrompu* \u2014 Kling a échoué\.\n"
            f"   Image Madison générée : `{madison_name}`\n"
            f"   \u2192 /retryKling pour relancer automatiquement\n"
            f"   \u2192 /manualGeneration pour une nouvelle source"
        )
    elif state.get("video_path"):
        vid_type = _escape_md(state.get("video_type") or "vidéo")
        caption_preview = _escape_md(str(state.get('caption', ''))[:80])
        pending_str = (
            f"\u2705 Vidéo \({vid_type}\) en attente de validation\n"
            f"   Caption : {caption_preview}\.\.\."
        )
    elif has_pending:
        caption_preview = _escape_md(str(state.get('caption', ''))[:80])
        pending_str = (
            f"\u2705 Image en attente de validation\n"
            f"   Caption : {caption_preview}\.\.\."
        )
    else:
        pending_str = "\u26aa Aucun contenu en attente"

    text = (
        f"📊 *Status {_escape_md(INFLUENCER_NAME)}*\n\n"
        f"{pending_str}\n\n"
        f"📅 Prochain post auto : {_escape_md(next_str)}\n"
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

async def cmd_retry_kling(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Relance l'étape Kling depuis l'état intermédiaire — /retryKling"""
    if not _is_authorized(update):
        return

    state = load_pending_state()
    if not state.get("_intermediate"):
        await _send_error(update, "Aucun état intermédiaire détecté \u2014 pas de Kling \u00e0 relancer.")
        return

    madison_path = state.get("madison_image_path")
    source_video = state.get("source_video_path")

    if not madison_path or not os.path.exists(madison_path):
        await _send_error(update, f"Image Madison introuvable : {madison_path or '(non défini)'}")
        return
    if not source_video or not os.path.exists(source_video):
        await _send_error(update, f"Vidéo source introuvable : {source_video or '(non défini)'}")
        return

    import tempfile
    params = {
        "madison_image_path": madison_path,
        "source_video_path":  source_video,
        "scene_json":         state.get("scene_json") or {},
        "step":               state.get("step"),
    }
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(params, f, ensure_ascii=False)
        params_path = f.name

    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    proc = subprocess.Popen(
        [sys.executable, script_path, "--resume-kling", "--override-params", params_path],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    logger.info(f"/retryKling lancé — PID {proc.pid} | madison={madison_path}")
    await update.message.reply_text(
        f"\U0001f504 Retry Kling lancé \\(PID {_escape_md(str(proc.pid))}\\)\\. "
        f"La vidéo sera envoyée ici dès qu'elle est prête\\. "
        f"Temps estimé : 10\\-15 minutes\\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )

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
                f"*Step {_escape_md(item['step'])}* — {_escape_md(item['eta'])}\n"
                f"  Format : `{_escape_md(item['format'])}` \\| Type : {_escape_md(item['type'])} \\| Hashtags : {hashtag_icon}\n"
                f"  _{_escape_md(item['note'])}_\n"
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
    RUN_MANUAL_GEN_SOURCE,
    RUN_MANUAL_GEN_PROMPT,
    RUN_INPAINT_SOURCE,
    RUN_INPAINT_PROMPT,
) = range(11)


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

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("🖼️ Image Pinterest",  callback_data="pinterest"),
            InlineKeyboardButton("🖼️ Image Génératif", callback_data="generatif"),
        ],
        [
            InlineKeyboardButton("🪄 Générer (prompt + source)",    callback_data="manual_gen"),
            InlineKeyboardButton("✂️ Inpainting (remplacer perso)", callback_data="manual_inpaint"),
        ],
        [
            InlineKeyboardButton("🎬🏚️ Vidéo Local",      callback_data="video_local"),
            InlineKeyboardButton("🎬📍 Vidéo Pinterest",  callback_data="video_pinterest"),
        ],
        [
            InlineKeyboardButton("🎬 Higgsfield (bientôt)", callback_data="video_higgsfield"),
        ],
    ])
    await update.message.reply_text(
        "🎬 *Lancement manuel — /run*\n\nQuel workflow ?",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return RUN_WORKFLOW


_VIDEO_WORKFLOWS = {"video_local", "video_pinterest", "video_higgsfield"}


async def run_choose_workflow(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Étape 1 : workflow choisi.
    - Workflows vidéo : lancement direct (pas de personnalisation manuelle)
    - Workflows image : proposer mode aléatoire/manuel
    """
    query = update.callback_query
    await query.answer()
    workflow = query.data
    ctx.user_data["run_workflow"] = workflow
    ctx.user_data["run_override"] = {}

    # Workflows vidéo → lancer directement
    if workflow in _VIDEO_WORKFLOWS:
        if workflow == "video_higgsfield":
            await query.edit_message_text(
                "🎬 *Workflow Higgsfield* — non implémenté \(V3 futur\)\.",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            return ConversationHandler.END

        await query.edit_message_text(
            f"✅ Workflow *{_escape_md(workflow)}* sélectionné\n\n"
            "🎬 Pipeline vidéo lancé\. Résultat dans quelques minutes\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        await _launch_run_pipeline(ctx)
        return ConversationHandler.END

    # Workflows manuels directs (source + prompt) — pas de personnalisation mood/location
    if workflow == "manual_gen":
        await query.edit_message_text(
            "🎨 *Génération \(prompt \+ source\)*\n\n"
            "*Comment ça marche :*\n"
            "📄 Tu envoies une image source \(décor, ambiance\)\n"
            "📝 Tu décris la scène souhaitée\n"
            "Gemini génère une *nouvelle image* avec l'influenceuse en s'inspirant de la source\n\n"
            "⚠️ Le fond n'est pas conservé — pour remplacer une personne, utilise Inpainting\n\n"
            "──────────────────\n\n"
            "📎 Envoie maintenant l'image source \(photo en pièce jointe\)\n"
            "ou /cancel pour annuler",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return RUN_MANUAL_GEN_SOURCE

    if workflow == "manual_inpaint":
        from config import INFLUENCER_REF_FACE_PATH as _rface, INFLUENCER_REF_BODY_PATH as _rbody
        refs_status = (
            f"  • `ref_face` : {'OK' if os.path.exists(_rface) else '❌ MANQUANT'} "
            f"\n  • `ref_body` : {'OK' if os.path.exists(_rbody) else '❌ MANQUANT'}"
        )
        await query.edit_message_text(
            "✂️ *Inpainting \(remplacer personnage\)*\n\n"
            "*Comment ça marche :*\n"
            "📄 Tu envoies une photo avec une personne\n"
            "rembg détecte et masque la personne automatiquement\n"
            "Gemini remplace en *préservant le décor, la lumière et la composition*\n\n"
            f"*Références influenceuse :*\n{refs_status}\n\n"
            "──────────────────\n\n"
            "📎 Envoie maintenant l'image source \(photo avec un personnage à remplacer\)\n"
            "ou /cancel pour annuler",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        return RUN_INPAINT_SOURCE

    # Workflows image → proposer mode
    keyboard = _make_keyboard(["aléatoire", "manuel"], cols=2)
    await query.edit_message_text(
        f"Workflow : *{_escape_md(workflow)}*\n\nMode ?",
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


# ================================================================
# Handlers /run — mode manuel gen \& inpaint
# ================================================================

async def run_manual_gen_source(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Reçoit la photo source pour le mode generate."""
    if not _is_authorized(update):
        return ConversationHandler.END
    if not update.message.photo:
        await _send_error(update, "Veuillez envoyer une photo en pièce jointe.")
        return RUN_MANUAL_GEN_SOURCE
    photo   = update.message.photo[-1]
    tg_file = await photo.get_file()
    dest = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "temp",
        f"run_gen_src_{photo.file_unique_id}.jpg",
    )
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    await tg_file.download_to_drive(dest)
    logger.info(f"Image source (gen) reçue : {dest}")
    ctx.user_data.setdefault("run_override", {})["source_path"] = dest
    await update.message.reply_text(
        "✅ Image source reçue\\.\n\n"
        "📝 *Décris maintenant la scène souhaitée :*\n"
        "_Exemple : Madison assise dans un café parisien, lumière dorée, manteau beige_\n\n"
        "ou /cancel pour annuler",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return RUN_MANUAL_GEN_PROMPT


async def run_manual_gen_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Reçoit le prompt et lance le pipeline manual_gen."""
    if not _is_authorized(update):
        return ConversationHandler.END
    prompt = update.message.text.strip()
    ctx.user_data.setdefault("run_override", {})["prompt"] = prompt
    ctx.user_data["run_workflow"] = "manual_gen"
    await update.message.reply_text(
        f"✅ *Lancement génération*\n\n"
        f"🎨 Prompt : _{_escape_md(prompt[:150])}_\n\n"
        "🚀 Pipeline lancé\. Résultat dans 1\\-2 minutes\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    await _launch_run_pipeline(ctx)
    return ConversationHandler.END


async def run_inpaint_source(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Reçoit la photo source pour le mode inpaint."""
    if not _is_authorized(update):
        return ConversationHandler.END
    if not update.message.photo:
        await _send_error(update, "Veuillez envoyer une photo en pièce jointe.")
        return RUN_INPAINT_SOURCE
    photo   = update.message.photo[-1]
    tg_file = await photo.get_file()
    dest = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "temp",
        f"run_inpaint_src_{photo.file_unique_id}.jpg",
    )
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    await tg_file.download_to_drive(dest)
    logger.info(f"Image source (inpaint) reçue : {dest}")
    ctx.user_data.setdefault("run_override", {})["source_path"] = dest
    await update.message.reply_text(
        "✅ Image source reçue\\.\n\n"
        "📝 *Prompt personnalisé ?* \\(optionnel\\)\n"
        "_Par défaut : l'influenceuse remplace le personnage en gardant décor \\+ lumière_\n\n"
        "Envoie ton prompt ou tape */skip* pour utiliser le prompt par défaut\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return RUN_INPAINT_PROMPT


async def run_inpaint_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Reçoit le prompt optionnel et lance le pipeline manual_inpaint."""
    if not _is_authorized(update):
        return ConversationHandler.END
    text = update.message.text.strip() if update.message.text else ""
    is_skip = (text.lower() in ("/skip", "skip"))
    ctx.user_data.setdefault("run_override", {})["prompt"] = "" if is_skip else text
    ctx.user_data["run_workflow"] = "manual_inpaint"
    prompt_line = (
        "🎨 Prompt : _par défaut_"
        if is_skip
        else f"🎨 Prompt : _{_escape_md(text[:150])}_"
    )
    await update.message.reply_text(
        f"✅ *Lancement inpainting*\n\n"
        f"{prompt_line}\n\n"
        "🚀 Pipeline lancé\. Résultat dans 1\\-2 minutes\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    await _launch_run_pipeline(ctx)
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
            RUN_WORKFLOW:          [CallbackQueryHandler(run_choose_workflow)],
            RUN_MODE:              [CallbackQueryHandler(run_choose_mode)],
            RUN_LOCATION:          [CallbackQueryHandler(run_choose_location)],
            RUN_OUTFIT:            [CallbackQueryHandler(run_choose_outfit)],
            RUN_MOOD:              [CallbackQueryHandler(run_choose_mood)],
            RUN_POSE:              [CallbackQueryHandler(run_choose_pose)],
            RUN_LIGHTING:          [CallbackQueryHandler(run_choose_lighting)],
            RUN_MANUAL_GEN_SOURCE: [MessageHandler(filters.PHOTO, run_manual_gen_source)],
            RUN_MANUAL_GEN_PROMPT: [MessageHandler(filters.TEXT & ~filters.COMMAND, run_manual_gen_prompt)],
            RUN_INPAINT_SOURCE:    [MessageHandler(filters.PHOTO, run_inpaint_source)],
            RUN_INPAINT_PROMPT:    [
                MessageHandler(filters.TEXT & ~filters.COMMAND, run_inpaint_prompt),
                CommandHandler("skip", run_inpaint_prompt),
            ],
        },
        fallbacks=[CommandHandler("cancel", run_cancel), CommandHandler("run", cmd_run)],
        per_message=False,
        allow_reentry=True,
    )


# ================================================================
# Commande /manualGeneration — ConversationHandler
# ================================================================

(
    MANUAL_TYPE,
    MANUAL_IMAGE_SOURCE,
    MANUAL_VIDEO_RECEIVE,
) = range(3)


async def cmd_manual_generation(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Point d'entrée /manualGeneration — demande le type de contenu."""
    if not _is_authorized(update):
        return ConversationHandler.END

    keyboard = InlineKeyboardMarkup([[
        InlineKeyboardButton("\U0001f5bc\ufe0f Image",  callback_data="manual_image"),
        InlineKeyboardButton("\U0001f3ac Vidéo",        callback_data="manual_video"),
    ]])
    await update.message.reply_text(
        "\U0001f3a8 *Génération manuelle*\n\nQuel type de contenu ?",
        reply_markup=keyboard,
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    return MANUAL_TYPE


async def manual_choose_type(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Type choisi — orienter vers la bonne demande de source."""
    query = update.callback_query
    await query.answer()

    if query.data == "manual_image":
        await query.edit_message_text(
            "\U0001f5bc\ufe0f *Génération image*\n\n"
            "Envoyez la source :\n"
            "\u2022 \U0001f4f7 Photo en pièce jointe\n"
            "\u2022 \U0001f517 URL d'une épingle Pinterest\n\n"
            "ou /cancel pour annuler",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        ctx.user_data["manual_media_type"] = "image"
        return MANUAL_IMAGE_SOURCE

    # manual_video
    await query.edit_message_text(
        "\U0001f3ac *Génération vidéo \\(Kling Motion Control\\)*\n\n"
        "Envoyez la vidéo source en pièce jointe \\(\.mp4, \.mov\\)\n\n"
        "\u26a0\ufe0f Pipeline automatique :\n"
        "  1\\. Extraction du meilleur frame\n"
        "  2\\. Génération de l'image influenceuse\n"
        "  3\\. Transfert de mouvement via Kling\n\n"
        "Temps estimé : 10\\-15 minutes\n\n"
        "ou /cancel pour annuler",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    ctx.user_data["manual_media_type"] = "video"
    return MANUAL_VIDEO_RECEIVE


async def manual_receive_image(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Reçoit une photo uploadée et lance le pipeline image."""
    if not _is_authorized(update):
        return ConversationHandler.END

    photo = update.message.photo[-1]   # taille max disponible
    tg_file = await photo.get_file()

    dest = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "temp",
        f"manual_image_{photo.file_unique_id}.jpg",
    )
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    await tg_file.download_to_drive(dest)
    logger.info(f"Image reçue via Telegram : {dest}")

    await update.message.reply_text(
        "\U0001f4e5 Image reçue \u2014 lancement du pipeline\.\.\.\n"
        "Résultat dans 1\\-2 minutes\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    _launch_manual_pipeline(dest, "manual_image")
    return ConversationHandler.END


async def manual_receive_url(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Reçoit un texte \u2014 URL Pinterest ou chemin local."""
    if not _is_authorized(update):
        return ConversationHandler.END

    text = update.message.text.strip()

    if text.startswith("https://") or text.startswith("http://"):
        if "pinterest" not in text.lower():
            await _send_error(
                update,
                "URL non supportée\. Seules les URLs Pinterest sont acceptées \u2014 "
                "ou envoyez directement une photo\.",
            )
            return MANUAL_IMAGE_SOURCE
        await update.message.reply_text(
            "\U0001f517 URL Pinterest détectée \u2014 téléchargement de l'image source\.\.\.\n"
            "Résultat dans quelques minutes\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
        _launch_manual_pipeline(text, "manual_image", is_url=True)
    elif os.path.exists(text):
        _launch_manual_pipeline(text, "manual_image")
        await update.message.reply_text(
            "\U0001f680 Pipeline lancé depuis chemin local\. Résultat dans quelques minutes\.",
            parse_mode=ParseMode.MARKDOWN_V2,
        )
    else:
        await _send_error(
            update,
            f"Source non reconnue : `{_escape_md(text[:100])}`\n"
            "Envoyez une photo ou une URL Pinterest\.",
        )
        return MANUAL_IMAGE_SOURCE

    return ConversationHandler.END


async def manual_receive_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Reçoit une vidéo uploadée et lance le pipeline vidéo."""
    if not _is_authorized(update):
        return ConversationHandler.END

    if update.message.video:
        tg_file = await update.message.video.get_file()
        ext = ".mp4"
        unique_id = update.message.video.file_unique_id
    elif update.message.document and (update.message.document.mime_type or "").startswith("video/"):
        tg_file = await update.message.document.get_file()
        raw_name = update.message.document.file_name or "video.mp4"
        ext = os.path.splitext(raw_name)[1] or ".mp4"
        unique_id = update.message.document.file_unique_id
    else:
        await _send_error(update, "Format non reconnu\. Envoyez un fichier \.mp4 ou \.mov\.")
        return MANUAL_VIDEO_RECEIVE

    dest = os.path.join(
        os.path.dirname(os.path.abspath(__file__)), "temp",
        f"manual_video_{unique_id}{ext}",
    )
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    await tg_file.download_to_drive(dest)
    logger.info(f"Vidéo reçue via Telegram : {dest}")

    await update.message.reply_text(
        "\U0001f4e5 Vidéo reçue \u2014 lancement du pipeline\.\.\.\n"
        "\u23f3 Temps estimé : 10\\-15 minutes \\(Kling Motion Control\\)\.\n"
        "La vidéo finale vous sera envoyée ici pour validation\.",
        parse_mode=ParseMode.MARKDOWN_V2,
    )
    _launch_manual_pipeline(dest, "manual_video")
    return ConversationHandler.END


def _launch_manual_pipeline(
    source: str,
    workflow: str,
    is_url: bool = False,
) -> None:
    """
    Lance main.py --workflow manual_image|manual_video avec la source donnée.
    --no-persist : les runs manuels n'affectent pas history.json.
    """
    import tempfile

    params = {"source_path": source, "is_url": is_url}
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".json", delete=False, encoding="utf-8"
    ) as f:
        json.dump(params, f, ensure_ascii=False)
        params_path = f.name

    script_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py")
    proc = subprocess.Popen(
        [
            sys.executable, script_path,
            "--workflow", workflow,
            "--override-params", params_path,
            "--no-persist",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    logger.info(f"Pipeline manuel lancé \u2014 PID {proc.pid} | workflow={workflow} | source={source[:80]}")


async def manual_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    ctx.user_data.clear()
    await update.message.reply_text("\u274c /manualGeneration annulé.")
    return ConversationHandler.END


def _build_manual_gen_handler() -> ConversationHandler:
    """ConversationHandler pour /manualGeneration."""
    return ConversationHandler(
        entry_points=[CommandHandler("manualGeneration", cmd_manual_generation)],
        states={
            MANUAL_TYPE: [
                CallbackQueryHandler(manual_choose_type, pattern="^manual_(image|video)$"),
            ],
            MANUAL_IMAGE_SOURCE: [
                MessageHandler(filters.PHOTO, manual_receive_image),
                MessageHandler(filters.TEXT & ~filters.COMMAND, manual_receive_url),
            ],
            MANUAL_VIDEO_RECEIVE: [
                MessageHandler(filters.VIDEO, manual_receive_video),
                MessageHandler(
                    filters.Document.MimeType("video/mp4") |
                    filters.Document.MimeType("video/quicktime") |
                    filters.Document.MimeType("video/x-msvideo"),
                    manual_receive_video,
                ),
            ],
        },
        fallbacks=[CommandHandler("cancel", manual_cancel)],
        per_message=False,
    )


# ================================================================
# Démarrage du service bot (point d'entrée systemd)
# ================================================================

# ================================================================
# Callbacks publication vidéo
# ================================================================

async def handle_pub_video(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """
    Gère les boutons de publication vidéo envoyés par send_video_for_validation().
    Callback data : pub_video_reel | pub_video_tiktok | pub_video_both |
                    pub_video_story | pub_video_ignore
    """
    query = update.callback_query
    await query.answer()

    if not _is_authorized(update):
        return

    action = query.data
    state  = load_pending_state()

    if action == "pub_video_ignore":
        clear_pending_state()
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text("❌ Vidéo ignorée.")
        logger.info("Vidéo ignorée par l'utilisateur")
        return

    video_public_url = state.get("video_public_url")
    video_filename   = state.get("video_filename", "")
    video_path       = state.get("video_path", "")
    caption          = state.get("caption", "")

    if not video_public_url:
        try:
            await query.edit_message_reply_markup(reply_markup=None)
        except Exception:
            pass
        await query.message.reply_text("❌ Aucune vidéo en attente dans le pending_state.")
        return

    try:
        await query.edit_message_reply_markup(reply_markup=None)
    except Exception:
        pass
    await query.message.reply_text("⏳ Publication en cours...")

    errors = []

    try:
        if action in ("pub_video_reel", "pub_video_both"):
            from instagram_publisher import publish_reel
            result = publish_reel(video_public_url, caption, video_filename)
            media_id = result.get("id", "?")
            await query.message.reply_text(
                f"✅ *Reel publié sur Instagram\!*\nMedia ID : `{_escape_md(media_id)}`",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            logger.info(f"Reel Instagram publié — media_id={media_id}")
    except Exception as e:
        logger.error(f"Erreur publication Reel Instagram : {e}")
        errors.append(f"Instagram Reel : {e}")

    try:
        if action in ("pub_video_tiktok", "pub_video_both"):
            from tiktok_publisher import publish_video
            publish_id = publish_video(video_path, caption)
            await query.message.reply_text(
                f"✅ *Vidéo publiée sur TikTok\!*\nPublish ID : `{_escape_md(publish_id)}`",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            logger.info(f"Vidéo TikTok publiée — publish_id={publish_id}")
    except Exception as e:
        logger.error(f"Erreur publication TikTok : {e}")
        errors.append(f"TikTok : {e}")

    try:
        if action == "pub_video_story":
            from instagram_publisher import publish_story_video
            result = publish_story_video(video_public_url, video_filename)
            media_id = result.get("id", "?")
            await query.message.reply_text(
                f"✅ *Story publiée sur Instagram\!*\nMedia ID : `{_escape_md(media_id)}`",
                parse_mode=ParseMode.MARKDOWN_V2,
            )
            logger.info(f"Story Instagram publiée — media_id={media_id}")
    except Exception as e:
        logger.error(f"Erreur publication Story Instagram : {e}")
        errors.append(f"Instagram Story : {e}")

    if errors:
        await query.message.reply_text(
            "❌ Erreurs lors de la publication :\n" + "\n".join(errors)
        )
    else:
        clear_pending_state()


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
    app.add_handler(CommandHandler("retryKling", cmd_retry_kling))
    app.add_handler(_build_run_handler())
    app.add_handler(_build_manual_gen_handler())

    # Callbacks publication vidéo (indépendants de la conversation /run)
    _VIDEO_PUB_ACTIONS = (
        "pub_video_reel", "pub_video_tiktok", "pub_video_both",
        "pub_video_story", "pub_video_ignore",
    )
    app.add_handler(
        CallbackQueryHandler(
            handle_pub_video,
            pattern="^(" + "|".join(_VIDEO_PUB_ACTIONS) + ")$",
        )
    )

    async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Log et notifie l'utilisateur en cas d'erreur dans un handler."""
        logger.error(f"Exception dans un handler : {context.error}", exc_info=context.error)
        if isinstance(update, Update) and update.effective_chat:
            try:
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"❌ Erreur interne : {context.error}",
                )
            except Exception:
                pass

    app.add_error_handler(error_handler)

    logger.info("Handlers enregistrés — démarrage du polling...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    start_bot()
