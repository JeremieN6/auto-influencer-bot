"""
main.py — Orchestrateur principal du pipeline.

Appelé automatiquement par le cron toutes les 4 jours.
Peut également être lancé manuellement pour tester.

FLUX COMPLET (workflow Pinterest V1) :
  1. Génération concept créatif (location / outfit / pose / mood / lighting)
  2. Récupération de l'étape calendrier courante (format, hashtags)
  3. Workflow Pinterest :
     a. Scraping Pinterest → image d'inspiration avec personnage
     b. Gemini Vision → JSON de scène
     c. Gemini → image finale avec influenceuse
  4. Claude → caption + hashtags
  5. Sauvegarde pending_state + envoi Telegram pour validation humaine
  → L'utilisateur valide via /validate dans Telegram
  → instagram_publisher.publish_post() est appelé depuis le bot

USAGE :
  python main.py                        → pipeline Pinterest (défaut)
  python main.py --workflow generatif   → pipeline génératif V2 (non implémenté)
  python main.py --dry-run              → exécute sans envoyer sur Telegram ni sauvegarder l'historique
"""

import argparse
import asyncio
import sys

from caption_generator import generate_caption
from concept_generator import (
    build_caption_prompt,
    generate_concept,
    get_current_calendar_step,
)
from logger import log, log_section, setup_logger
from telegram_bot import save_pending_state, send_for_validation


def run_pipeline(
    workflow: str = "pinterest",
    override_params: dict | None = None,
    dry_run: bool = False,
) -> dict:
    """
    Exécute le pipeline complet de génération de contenu.

    Args:
        workflow        : "pinterest" (V1) ou "generatif" (V2 — non implémenté)
        override_params : paramètres manuels depuis /run Telegram (V2)
        dry_run         : si True, pipeline sans envoi Telegram ni sauvegarde historique

    Returns:
        dict avec les clés : image_path, public_url, filename, caption, concept, step
    """
    log_section("main", f"PIPELINE DÉMARRÉ — workflow={workflow} | dry_run={dry_run}")

    # ── Étape 1 : Concept créatif ────────────────────────────────
    log("info", "main", "=== Étape 1/4 : Génération concept ===")
    concept = generate_concept(
        override_params=override_params,
        persist=not dry_run,
    )
    log("info", "main", (
        f"Concept : {concept['mood']} | {concept['outfit']} | "
        f"{concept['location']} | {concept['lighting']}"
    ))

    # ── Étape 2 : Étape calendrier ───────────────────────────────
    log("info", "main", "=== Étape 2/4 : Calendrier éditorial ===")
    step = get_current_calendar_step()
    log("info", "main", f"Format : {step['format']} | Type : {step['type']} | Hashtags : {step['hashtags']}")

    # ── Étape 3 : Workflow image ─────────────────────────────────
    log("info", "main", f"=== Étape 3/4 : Workflow {workflow} ===")

    if workflow == "pinterest":
        from workflows.workflow_pinterest import run as run_workflow
    elif workflow == "generatif":
        from workflows.workflow_generatif import run as run_workflow  # type: ignore[assignment]
    else:
        raise ValueError(f"Workflow inconnu : '{workflow}'. Valeurs acceptées : 'pinterest', 'generatif'")

    local_path, public_url, filename = run_workflow(concept)
    log("info", "main", f"Image générée : {local_path}")

    # ── Étape 4 : Caption ────────────────────────────────────────
    log("info", "main", "=== Étape 4/4 : Génération caption (Claude) ===")
    caption_prompt = build_caption_prompt(concept, step)
    caption        = generate_caption(caption_prompt)
    log("info", "main", f"Caption : {caption[:100]}...")

    # ── Sauvegarde état + envoi Telegram ─────────────────────────
    state = {
        "image_path":     local_path,
        "public_url":     public_url,
        "caption":        caption,
        "concept":        concept,
        "step":           step,
        "last_prompt":    caption_prompt,
        "image_filename": filename,
    }

    if dry_run:
        log("info", "main", "[DRY RUN] — Pas d'envoi Telegram, pas de sauvegarde historique")
        log("info", "main", f"[DRY RUN] Image : {local_path}")
        log("info", "main", f"[DRY RUN] Caption :\n{caption}")
    else:
        save_pending_state(state)
        log("info", "main", "pending_state sauvegardé")

        asyncio.run(send_for_validation(local_path, caption))
        log("info", "main", "Image envoyée sur Telegram — en attente de /validate")

    log_section("main", "PIPELINE TERMINÉ")
    return state


# ================================================================
# Point d'entrée CLI
# ================================================================

def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Pipeline d'automatisation Instagram pour influenceuse IA",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Exemples :
  python main.py                        # Pinterest V1 (défaut, cron)
  python main.py --workflow generatif   # Génératif V2 (non implémenté)
  python main.py --dry-run              # Test sans Telegram ni historique
        """,
    )
    parser.add_argument(
        "--workflow",
        choices=["pinterest", "generatif"],
        default="pinterest",
        help="Workflow à utiliser (défaut : pinterest)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="Exécuter sans envoyer sur Telegram ni modifier l'historique",
    )
    return parser.parse_args()


if __name__ == "__main__":
    setup_logger()
    args = _parse_args()

    try:
        run_pipeline(workflow=args.workflow, dry_run=args.dry_run)
        sys.exit(0)
    except KeyboardInterrupt:
        log("info", "main", "Pipeline interrompu par l'utilisateur")
        sys.exit(0)
    except Exception as e:
        log("error", "main", f"Erreur fatale pipeline : {e}")
        # Tentative de notification Telegram en cas d'erreur fatale
        try:
            import asyncio as _asyncio
            from telegram import Bot
            from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

            async def _notify_error() -> None:
                async with Bot(token=TELEGRAM_BOT_TOKEN) as bot:
                    await bot.send_message(
                        chat_id=TELEGRAM_CHAT_ID,
                        text=f"🔴 *Erreur pipeline main\\.py*\n\n`{str(e)[:500]}`",
                        parse_mode="MarkdownV2",
                    )

            _asyncio.run(_notify_error())
        except Exception as notify_err:
            log("error", "main", f"Notification Telegram d'erreur également échouée : {notify_err}")

        sys.exit(1)
