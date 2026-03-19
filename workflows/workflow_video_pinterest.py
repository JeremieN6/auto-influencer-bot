"""
workflows/workflow_video_pinterest.py — Workflow Vidéo 2 : source Pinterest.

DESCRIPTION :
  Source = scraping Pinterest avec keywords orientés vidéo (pinterest_video_tags).
  Même routeur personnage/pas de personnage que workflow_video_local.

KEYWORDS :
  Requête = "video " + pinterest_video_tag + keyword _VIDEO_PERSON_KEYWORDS (optionnel)
  Total max 5 mots.
  Exemples : "video mirror selfie aesthetic", "video beach aesthetic girl"

FLUX :
  [variables.json — tirage aléatoire tag vidéo parmi pinterest_video_tags]
        ↓
  [pinterest_scraper — recherche Pinterest + téléchargement vidéo .mp4]
        ↓
  [frame_extractor.py — Frame 1]
        ↓
  [Gemini Vision — détection personnage]
        ↓
  ┌── Personnage → même pipeline que workflow_video_local (Motion Control → Reel)
  └── Pas de personnage → vidéo brute → Story

Appelé par : main.py via --workflow video_pinterest
"""

import json
import os
import random
import shutil
from pathlib import Path

from caption_generator import generate_caption
from concept_generator import build_caption_prompt, get_current_calendar_step
from frame_extractor import extract_best_frame
from image_generator import generate_image, image_to_json, inject_madison_body
from logger import get_logger, log_section, log_step
from prompts import PROMPT_JSON_TO_IMAGE

logger = get_logger(__name__)

TOTAL_STEPS = 5


# ================================================================
# Scraping vidéo Pinterest
# ================================================================

def _build_video_query(variables: dict) -> str:
    """
    Construit une requête Pinterest orientée vidéo.

    Format : "video " + pinterest_video_tag (+ keyword person optionnel)
    Total max ~5 mots.
    """
    from pinterest_scraper import _VIDEO_PERSON_KEYWORDS

    video_tags = variables.get("pinterest_video_tags", {})
    if not video_tags:
        raise ValueError("Clé 'pinterest_video_tags' absente dans variables.json")

    tag_values = list(video_tags.values())
    chosen_tag = random.choice(tag_values)

    # Optionnellement ajouter un keyword personnage (50% du temps)
    person_kw = ""
    if random.random() < 0.5:
        person_kw = random.choice(_VIDEO_PERSON_KEYWORDS)
        # Déduplication
        tag_words    = set(chosen_tag.lower().split())
        person_words = [w for w in person_kw.split() if w.lower() not in tag_words]
        person_kw    = " ".join(person_words)

    query = f"{chosen_tag} {person_kw}".strip()
    logger.info(f"Requête Pinterest vidéo : '{query}'")
    return query


async def _scrape_pinterest_video(query: str) -> str:
    """
    Recherche sur Pinterest et télécharge la première vidéo .mp4 trouvée.
    Retourne le chemin local de la vidéo téléchargée.
    """
    import urllib.parse
    import time

    import requests
    from playwright.async_api import async_playwright

    from config import OUTPUTS_DIR
    from pinterest_scraper import USER_AGENTS

    PINTEREST_SEARCH_URL = "https://www.pinterest.com/search/videos/?q={query}"
    encoded_q = urllib.parse.quote(query)
    url        = PINTEREST_SEARCH_URL.format(query=encoded_q)

    logger.info(f"Navigation Pinterest vidéo → {url}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

            # Attendre le chargement des vidéos
            try:
                await page.wait_for_selector("video[src]", timeout=12_000)
            except Exception:
                logger.warning("Aucune balise <video> trouvée — tentative via attribut data-src")

            # Scroll léger
            import asyncio
            await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 3)")
            await asyncio.sleep(random.uniform(2.0, 4.0))

            # Extraire les URLs vidéo
            video_urls: list[str] = await page.evaluate("""
                () => {
                    const sources = [];
                    // Balises video
                    document.querySelectorAll('video[src]').forEach(v => {
                        if (v.src && v.src.includes('.mp4')) sources.push(v.src);
                    });
                    // Balises source dans video
                    document.querySelectorAll('video source[src]').forEach(s => {
                        if (s.src && s.src.includes('.mp4')) sources.push(s.src);
                    });
                    // data-video-src sur les pins
                    document.querySelectorAll('[data-video-src]').forEach(el => {
                        const src = el.getAttribute('data-video-src');
                        if (src && src.includes('.mp4')) sources.push(src);
                    });
                    return [...new Set(sources)];
                }
            """)

            logger.info(f"{len(video_urls)} URL(s) vidéo collectées")

        finally:
            await browser.close()

    if not video_urls:
        raise RuntimeError(
            f"Aucune vidéo .mp4 trouvée sur Pinterest pour la requête : '{query}'\n"
            "Essayer avec un autre tag vidéo ou vérifier que Pinterest affiche bien des vidéos."
        )

    # Télécharger la première vidéo
    os.makedirs(OUTPUTS_DIR, exist_ok=True)

    from datetime import datetime
    headers = {"User-Agent": random.choice(USER_AGENTS)}

    for video_url in video_urls[:3]:   # essayer les 3 premières URLs
        try:
            resp = requests.get(video_url, headers=headers, timeout=60, stream=True)
            if resp.status_code != 200:
                logger.warning(f"Download vidéo échoué ({resp.status_code}) : {video_url}")
                continue

            ext      = ".mp4"
            filename = f"pinterest_video_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}{ext}"
            path     = os.path.join(OUTPUTS_DIR, filename)

            with open(path, "wb") as f:
                for chunk in resp.iter_content(chunk_size=8192):
                    f.write(chunk)

            size_mb = os.path.getsize(path) / (1024 * 1024)
            if size_mb < 0.1:
                logger.warning(f"Vidéo trop petite ({size_mb:.2f} MB) — ignorée")
                os.remove(path)
                continue

            logger.info(f"Vidéo Pinterest téléchargée : {path} ({size_mb:.1f} MB)")
            return path

        except Exception as e:
            logger.warning(f"Erreur download vidéo : {e}")
            continue

    raise RuntimeError(
        f"Échec download de toutes les vidéos Pinterest pour : '{query}'"
    )


# ================================================================
# Helpers (réutilisés depuis workflow_video_local)
# ================================================================

def _expose_video_via_nginx(video_path: str) -> tuple[str, str]:
    from config import NGINX_BASE_URL, NGINX_OUTPUT_DIR
    filename   = Path(video_path).name
    nginx_path = os.path.join(NGINX_OUTPUT_DIR, filename)
    public_url = f"{NGINX_BASE_URL}/{filename}"
    os.makedirs(NGINX_OUTPUT_DIR, exist_ok=True)
    if os.path.abspath(video_path) != os.path.abspath(nginx_path):
        shutil.copy(video_path, nginx_path)
    logger.info(f"Vidéo exposée via nginx : {public_url}")
    return filename, public_url


def _build_video_caption_prompt(scene_json: dict, step: dict, video_type: str) -> str:
    location = ""
    outfit   = ""
    mood     = ""
    try:
        loc      = scene_json.get("location", {})
        location = loc.get("description") or loc.get("place") or loc.get("setting") or "aesthetic location"
        wardrobe = scene_json.get("subject", {}).get("wardrobe", {})
        outfit   = wardrobe.get("top") or wardrobe.get("top_garment") or "casual outfit"
        mood     = scene_json.get("mood") or scene_json.get("atmosphere") or "confident"
    except Exception:
        pass

    concept_hint = {"location": location, "outfit": outfit, "mood": mood,
                    "lighting": scene_json.get("lighting", {}).get("quality", "natural light")}
    base_prompt  = build_caption_prompt(concept_hint, step)
    type_hint    = "[Instagram Reel — motion content, dynamic energy]" if video_type == "reel" \
                   else "[Instagram Story — ambiance vidéo]"
    return f"{base_prompt}\n\n{type_hint}"


# ================================================================
# Point d'entrée
# ================================================================

def run(concept: dict | None = None) -> tuple[str, str, str, str, str]:
    """
    Exécute le workflow vidéo Pinterest complet.

    Returns:
        (local_video_path, public_url, filename, caption, video_type)
        - video_type : "reel" ou "story"
    """
    import asyncio
    import json as _json

    from pathlib import Path

    log_section(__name__, "WORKFLOW VIDÉO PINTEREST")
    step = get_current_calendar_step()

    # ── Étape 1/5 : Construire la requête + scraper Pinterest ───
    log_step(__name__, 1, TOTAL_STEPS, "Scraping vidéo Pinterest")

    with open(Path(__file__).parent.parent / "data" / "variables.json", encoding="utf-8") as f:
        variables = _json.load(f)

    query      = _build_video_query(variables)
    video_path = asyncio.run(_scrape_pinterest_video(query))
    logger.info(f"Vidéo Pinterest récupérée : {video_path}")

    # ── Étape 2/5 : Extraction frame intelligente ───────────────
    log_step(__name__, 2, TOTAL_STEPS, "Extraction frame intelligente (scan multi-timestamps)")
    frame_path = extract_best_frame(video_path)
    logger.info(f"Frame extraite : {frame_path}")

    # ── Étape 3/5 : Détection personnage ────────────────────────
    log_step(__name__, 3, TOTAL_STEPS, "Détection personnage (Gemini Vision)")
    from pinterest_scraper import _detect_person_in_image
    has_person = _detect_person_in_image(frame_path)
    logger.info(f"Personnage détecté : {has_person}")

    if has_person:
        # ── Branche Personnage → Motion Control ─────────────────
        log_step(__name__, 4, TOTAL_STEPS, "Analyse scène + génération image Madison")

        scene_json = image_to_json(frame_path)
        logger.info(f"JSON de scène extrait — clés : {list(scene_json.keys())}")

        scene_json = inject_madison_body(scene_json)
        logger.info("Bloc corps Madison injecté")

        # Nettoyer la frame temporaire (plus utile après analyse)
        if os.path.exists(frame_path):
            os.remove(frame_path)
            logger.debug(f"Frame temporaire supprimée : {frame_path}")

        prompt_text = PROMPT_JSON_TO_IMAGE.format(
            scene_json=json.dumps(scene_json, indent=2, ensure_ascii=False)
        )
        madison_image_path, _ = generate_image(prompt_text)
        logger.info(f"Image Madison générée : {madison_image_path}")

        log_step(__name__, 5, TOTAL_STEPS, "Kling Motion Control")
        from kling_generator import build_motion_prompt, generate_video_motion_control
        motion_prompt    = build_motion_prompt(scene_json)
        final_video_path = generate_video_motion_control(
            character_image_path=madison_image_path,
            source_video_path=video_path,
            motion_prompt=motion_prompt,
        )

        filename, public_url = _expose_video_via_nginx(final_video_path)
        caption = generate_caption(_build_video_caption_prompt(scene_json, step, "reel"))

        logger.info(f"=== Workflow Vidéo Pinterest terminé (reel) : {final_video_path} ===")
        return final_video_path, public_url, filename, caption, "reel", madison_image_path

    else:
        # ── Branche Ambiance → Story ─────────────────────────────
        log_step(__name__, 4, TOTAL_STEPS, "Flux ambiance : vidéo brute")
        log_step(__name__, 5, TOTAL_STEPS, "Génération caption ambiance")

        # Nettoyer la frame temporaire
        if os.path.exists(frame_path):
            os.remove(frame_path)
            logger.debug(f"Frame temporaire supprimée : {frame_path}")

        filename, public_url = _expose_video_via_nginx(video_path)

        concept_hint = {"location": "aesthetic scene", "mood": "chill ambiance",
                        "outfit": "", "lighting": "natural light"}
        caption_prompt = (
            build_caption_prompt(concept_hint, step)
            + "\n\n[Instagram Story — ambiance vidéo, pas de personnage]"
        )
        caption = generate_caption(caption_prompt)

        logger.info(f"=== Workflow Vidéo Pinterest terminé (story) : {video_path} ===")
        return video_path, public_url, filename, caption, "story", ""
