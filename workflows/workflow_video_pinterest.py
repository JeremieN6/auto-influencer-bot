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

from PIL import Image as _PILImage

from caption_generator import generate_caption
from concept_generator import build_caption_prompt, get_current_calendar_step
from frame_extractor import check_min_shot_duration, extract_best_frame
from image_generator import ImageSafetyError, generate_image, image_to_json, inject_madison_body, validate_body_proportions
from logger import get_logger, log_section, log_step
from prompts import PROMPT_JSON_TO_IMAGE

logger = get_logger(__name__)

TOTAL_STEPS = 5


# ================================================================
# Scraping vidéo Pinterest
# ================================================================

def _build_video_query(variables: dict, pool_type: str = "reel") -> str:
    """
    Construit une requête Pinterest orientée vidéo.

    Args:
        pool_type : "reel" → pinterest_video_tags_reel (pool avec personnage)
                    "story" → pinterest_video_tags_story (pool ambiance sans personnage)

    Format : pool_tag (+ keyword person optionnel si pool reel)
    """
    pool_key = "pinterest_video_tags_reel" if pool_type == "reel" else "pinterest_video_tags_story"
    video_tags = variables.get(pool_key) or variables.get("pinterest_video_tags", {})
    if not video_tags:
        raise ValueError(f"Clé '{pool_key}' absente dans variables.json")

    tag_values = list(video_tags.values())
    chosen_tag = random.choice(tag_values)

    # Règle : ≤ 4 mots par requête Pinterest (évite les requêtes trop spécifiques)
    words = chosen_tag.split()
    query = " ".join(words[:4])
    logger.info(f"Requête Pinterest vidéo ({pool_type}) : '{query}'")
    return query


async def _extract_video_url_from_pin(page, pin_url: str) -> str | None:
    """
    Visite une page de pin individuelle et extrait l'URL vidéo .mp4.

    Pinterest embarque les métadonnées du pin dans un JSON côté serveur
    (balise <script id="__PWS_DATA__"> ou __PWS_INITIAL_STATE__).
    Les URLs vidéo y sont présentes en clair, au format :
        v.pinimg.com/videos/mc/720p/XX/XX/XXXXX.mp4
    """
    import asyncio
    import re as _re
    try:
        await page.goto(pin_url, wait_until="domcontentloaded", timeout=20_000)
        await asyncio.sleep(random.uniform(1.5, 2.5))

        # Chercher dans le JSON embarqué (__PWS_DATA__ ou __PWS_INITIAL_STATE__)
        content = await page.content()
        matches = _re.findall(
            r'https?://v\d*\.pinimg\.com[^"\'<>\s\\]*\.mp4[^"\'<>\s\\]*',
            content
        )
        if matches:
            # Préférer les 720p, sinon prendre la première
            for m in matches:
                if "720p" in m or "1080p" in m:
                    return m
            return matches[0]
    except Exception as e:
        logger.warning(f"Erreur lecture pin {pin_url} : {e}")
    return None


async def _scrape_pinterest_video(query: str) -> str:
    """
    Recherche sur Pinterest et télécharge la première vidéo .mp4 trouvée.
    Retourne le chemin local de la vidéo téléchargée.

    Stratégie :
      1. Page de recherche /search/videos/ → collecte des hrefs /pin/XXXXX/
      2. Visite de chaque page de pin individuellement
      3. Extraction URL .mp4 depuis le JSON embarqué (__PWS_DATA__)
      4. Téléchargement HTTP direct

    Pourquoi cette approche :
      La page de recherche Pinterest utilise du lazy-loading + HLS streams (m3u8)
      + blob URLs — les .mp4 ne sont jamais visibles dans le DOM ou les réponses
      réseau de la page de recherche. Les pages de pins individuelles embarquent
      en revanche les URLs directes en clair dans un <script> JSON côté serveur.
    """
    import urllib.parse
    import asyncio

    import requests
    from playwright.async_api import async_playwright

    from config import OUTPUTS_DIR
    from pinterest_scraper import USER_AGENTS

    PINTEREST_SEARCH_URL = "https://www.pinterest.com/search/videos/?q={query}"
    PINTEREST_BASE        = "https://www.pinterest.com"
    encoded_q             = urllib.parse.quote(query)
    search_url            = PINTEREST_SEARCH_URL.format(query=encoded_q)

    logger.info(f"Navigation Pinterest vidéo → {search_url}")

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        context = await browser.new_context(
            user_agent=random.choice(USER_AGENTS),
            viewport={"width": 1280, "height": 800},
        )
        page = await context.new_page()

        try:
            # ── Étape 1 : page de recherche → collecter les hrefs de pins ──
            await page.goto(search_url, wait_until="domcontentloaded", timeout=30_000)

            # Scroll pour charger plus de pins
            for _ in range(4):
                await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
                await asyncio.sleep(random.uniform(1.0, 1.8))

            pin_hrefs: list[str] = await page.evaluate("""
                () => {
                    const links = new Set();
                    document.querySelectorAll('a[href*="/pin/"]').forEach(a => {
                        const href = a.getAttribute('href');
                        if (href && /^\\/pin\\/\\d+/.test(href)) links.add(href);
                    });
                    return [...links].slice(0, 10);
                }
            """)

            logger.info(f"{len(pin_hrefs)} hrefs de pins collectés")

            if not pin_hrefs:
                raise RuntimeError(
                    f"Aucun pin trouvé sur Pinterest pour la requête : '{query}'\n"
                    "Vérifier que la page de recherche affiche bien des résultats."
                )

            # ── Étape 2 : visiter chaque pin pour extraire l'URL vidéo ──────
            video_url: str | None = None
            for href in pin_hrefs:
                pin_url = PINTEREST_BASE + href
                logger.info(f"Lecture pin : {pin_url}")
                video_url = await _extract_video_url_from_pin(page, pin_url)
                if video_url:
                    logger.info(f"URL vidéo trouvée : {video_url}")
                    break
                await asyncio.sleep(random.uniform(0.8, 1.5))

        finally:
            await browser.close()

    if not video_url:
        raise RuntimeError(
            f"Aucune vidéo .mp4 trouvée sur Pinterest pour la requête : '{query}'\n"
            "Essayer avec un autre tag vidéo ou vérifier que Pinterest affiche bien des vidéos."
        )

    # ── Étape 3 : téléchargement HTTP direct ────────────────────
    os.makedirs(OUTPUTS_DIR, exist_ok=True)

    from datetime import datetime
    headers = {"User-Agent": random.choice(USER_AGENTS), "Referer": "https://www.pinterest.com/"}

    try:
        resp = requests.get(video_url, headers=headers, timeout=60, stream=True)
        if resp.status_code != 200:
            raise RuntimeError(f"Download vidéo échoué ({resp.status_code}) : {video_url}")

        filename = f"pinterest_video_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}.mp4"
        path     = os.path.join(OUTPUTS_DIR, filename)

        with open(path, "wb") as f:
            for chunk in resp.iter_content(chunk_size=8192):
                f.write(chunk)

        size_mb = os.path.getsize(path) / (1024 * 1024)
        if size_mb < 0.1:
            os.remove(path)
            raise RuntimeError(f"Vidéo téléchargée trop petite ({size_mb:.2f} MB) — URL invalide ?")

        logger.info(f"Vidéo Pinterest téléchargée : {path} ({size_mb:.1f} MB)")
        return path

    except Exception as e:
        raise RuntimeError(f"Erreur download vidéo Pinterest : {e}") from e


def _crop_to_portrait_9_16(image_path: str) -> str:
    """
    Si l'image générée est landscape, la recadre en portrait 9:16 (centre-crop).
    Retourne le chemin de l'image (inchangé si déjà portrait).

    Kling Motion Control utilise les dimensions de l'image character pour définir
    l'aspect ratio de la vidéo de sortie → une image landscape produit une vidéo
    landscape, quelle que soit l'orientation de la vidéo source.
    """
    img = _PILImage.open(image_path)
    w, h = img.size
    if h >= w:
        return image_path  # déjà portrait ou carré

    target_h = h
    target_w = int(h * 9 / 16)
    left  = (w - target_w) // 2
    cropped = img.crop((left, 0, left + target_w, target_h))
    cropped.save(image_path, "JPEG", quality=95)
    logger.info(f"Image recadrée portrait 9:16 : {w}x{h} → {target_w}x{target_h} ({image_path})")
    return image_path


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

def run(concept: dict | None = None, pool_type: str = "reel") -> tuple[str, str, str, str, str, str, str]:
    """
    Exécute le workflow vidéo Pinterest complet.

    Args:
        pool_type : "reel" → pinterest_video_tags_reel (pool avec personnage)
                    "story" → pinterest_video_tags_story (pool ambiance POV)

    Returns:
        (local_video_path, public_url, filename, caption, video_type,
         madison_image_path, source_video_path)
        - video_type : "reel" ou "story" (peut différer de pool_type si fallback)
    """
    import asyncio
    import json as _json

    from pathlib import Path

    log_section(__name__, "WORKFLOW VIDÉO PINTEREST")
    step = get_current_calendar_step()

    with open(Path(__file__).parent.parent / "data" / "variables.json", encoding="utf-8") as f:
        variables = _json.load(f)

    # ── Scraping + détection personnage (avec retry si pool reel) ──
    # Quand pool_type == "reel", on veut une vidéo avec personnage.
    # Pinterest ne garantit pas qu'un résultat contient un humain,
    # même avec des mots-clés orientés "girl". On retente donc
    # jusqu'à MAX_REEL_RETRIES fois avec une nouvelle requête.
    MAX_REEL_RETRIES = 3
    has_person    = False
    video_path    = ""
    frame_path    = ""
    queries_tried : list[str] = []

    for attempt in range(1, MAX_REEL_RETRIES + 1):
        # ── Étape 1/5 : Construire la requête + scraper Pinterest ───
        log_step(__name__, 1, TOTAL_STEPS, f"Scraping vidéo Pinterest (tentative {attempt}/{MAX_REEL_RETRIES})")

        query      = _build_video_query(variables, pool_type=pool_type)
        queries_tried.append(query)
        video_path = asyncio.run(_scrape_pinterest_video(query))
        logger.info(f"Vidéo Pinterest récupérée : {video_path}")

        # ── Vérification plans continus (requis par Kling ≥ 3s) ─────
        if not check_min_shot_duration(video_path, min_seconds=3.0):
            logger.warning(
                "Vidéo Pinterest rejetée : montage trop rapide (aucun plan ≥ 3s) — "
                "Kling refuserait cette source."
            )
            if attempt < MAX_REEL_RETRIES:
                # Supprimer uniquement si on va retenter — la branche story en a besoin sinon
                if os.path.exists(video_path):
                    os.remove(video_path)
                continue
            else:
                # Dernière tentative aussi rejetée : garder la vidéo pour le fallback story
                # (la branche story publie la vidéo brute, Kling n'est pas impliqué)
                logger.warning("Toutes les vidéos avaient des cuts trop rapides — fallback story (vidéo conservée)")
                break

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
            # ── Vérification upper body (requis par Kling Motion Control) ───────
            from pinterest_scraper import _detect_upper_body_visible
            has_upper_body = _detect_upper_body_visible(frame_path)
            logger.info(f"Upper body complet visible : {has_upper_body}")

            if not has_upper_body:
                logger.warning(
                    "Upper body non visible dans la vidéo source — "
                    "fallback branche story (évite rejet Kling : 'No complete upper body detected')"
                )
                has_person = False

        if has_person:
            break  # vidéo exploitable trouvée

        # Pool story : pas besoin de personnage, on accepte directement
        if pool_type == "story":
            break

        # Pool reel : pas de personnage → nettoyer et retenter
        if attempt < MAX_REEL_RETRIES:
            logger.warning(
                f"Pool reel : aucun personnage détecté (tentative {attempt}/{MAX_REEL_RETRIES}) — "
                f"nouvelle vidéo..."
            )
            # Nettoyer les fichiers temporaires de cette tentative
            for tmp in [frame_path, video_path]:
                if tmp and os.path.exists(tmp):
                    os.remove(tmp)
        else:
            logger.warning(
                f"Pool reel : aucun personnage après {MAX_REEL_RETRIES} tentatives — "
                f"fallback story"
            )

    if has_person:
        # ── Branche Personnage → Motion Control ─────────────────
        if pool_type == "story":
            logger.warning(
                "Calendrier attendait story (ambiance), got reel (personnage détecté) — "
                "adaptation destination : sera publié en reel"
            )
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
        # Générer directement en portrait 9:16 pour éviter un crop agressif (→ rejet Kling)
        try:
            madison_image_path, _ = generate_image(prompt_text, aspect_ratio="9:16")
        except ImageSafetyError:
            # Gemini refuse l'image Madison même après prompt sanitisé → basculer
            # en flux ambiance (vidéo source brute, type=story) plutôt qu'abandonner.
            logger.warning(
                "IMAGE_SAFETY persistant sur génération image Madison — "
                "fallback flux ambiance (vidéo source brute, type=story)"
            )
            filename, public_url = _expose_video_via_nginx(video_path)
            concept_hint = {"location": "aesthetic scene", "mood": "chill ambiance",
                            "outfit": "", "lighting": "natural light"}
            caption_prompt = (
                build_caption_prompt(concept_hint, step)
                + "\n\n[Instagram Story — ambiance vidéo, pas de personnage]"
            )
            caption = generate_caption(caption_prompt)
            return video_path, public_url, filename, caption, "story", "", video_path, "N/A (IMAGE_SAFETY)", queries_tried
        logger.info(f"Image Madison générée : {madison_image_path}")
        madison_image_path = _crop_to_portrait_9_16(madison_image_path)  # safety net si Gemini ignore le ratio

        # ── Validation proportions + retry unique ────────────────────
        body_ok = validate_body_proportions(madison_image_path)
        body_status = "✓ OK"
        if not body_ok:
            logger.warning("Proportions insuffisantes — 1 retry génération image...")
            try:
                madison_image_path, _ = generate_image(prompt_text, aspect_ratio="9:16")
                madison_image_path = _crop_to_portrait_9_16(madison_image_path)
                body_ok    = validate_body_proportions(madison_image_path)
                body_status = "⚠ Retry — ✓ OK" if body_ok else "⚠ Retry — non validé"
            except ImageSafetyError:
                logger.warning("IMAGE_SAFETY sur retry — fallback flux ambiance")
                filename, public_url = _expose_video_via_nginx(video_path)
                concept_hint = {"location": "aesthetic scene", "mood": "chill ambiance",
                                "outfit": "", "lighting": "natural light"}
                caption_prompt = (
                    build_caption_prompt(concept_hint, step)
                    + "\n\n[Instagram Story — ambiance vidéo, pas de personnage]"
                )
                caption = generate_caption(caption_prompt)
                return video_path, public_url, filename, caption, "story", "", video_path, "N/A (IMAGE_SAFETY retry)", queries_tried
        logger.info(f"Corps Madison : {body_status}")

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
        return final_video_path, public_url, filename, caption, "reel", madison_image_path, video_path, body_status, queries_tried

    else:
        # ── Branche Ambiance → Story ─────────────────────────────────
        if pool_type == "reel":
            logger.warning(
                "Calendrier attendait reel (personnage), got story (aucun personnage détecté) — "
                "adaptation destination : sera publié en story"
            )
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
        return video_path, public_url, filename, caption, "story", "", video_path, "N/A (flux ambiance)", queries_tried
