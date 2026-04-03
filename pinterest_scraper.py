"""
pinterest_scraper.py — Scraping Pinterest via Playwright.

Responsabilités :
- Rechercher sur Pinterest avec un concept (location + lighting + keywords)
- Extraire les URLs des 10-20 premières images en mémoire
- Sélectionner une image avec un personnage humain détecté (Gemini Vision)
- Gérer les anti-blocages : délais, rotation UA, retry sur 429/login redirect
- Notifier Telegram en cas d'échec complet et lever une exception propre

Utilisé par : workflows/workflow_pinterest.py
"""

import asyncio
import os
import random
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

import requests
from playwright.async_api import async_playwright, BrowserContext, Page

from config import GEMINI_API_KEY, GEMINI_MODEL_VISION, OUTPUTS_DIR
from logger import get_logger

logger = get_logger(__name__)

# ================================================================
# Constantes
# ================================================================

# Liste d'User-Agents navigateurs réels (rotation anti-blocage)
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_3_1) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.3.1 Safari/605.1.15",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:123.0) Gecko/20100101 Firefox/123.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
]

PINTEREST_SEARCH_URL = "https://www.pinterest.com/search/pins/?q={query}"
MAX_IMAGES_TO_COLLECT = 20          # Nombre max de URLs collectées en mémoire
MAX_DOWNLOAD_ATTEMPTS = 3           # Tentatives download par image
MAX_PIPELINE_RETRIES  = 3           # Tentatives de pipeline complet
DELAY_MIN             = 2.0         # Délai min entre actions (secondes)
DELAY_MAX             = 5.0         # Délai max entre actions (secondes)
DELAY_ON_429          = (30, 60)    # Pause sur HTTP 429 (secondes)

# Keywords garantissant un personnage dans les résultats Pinterest.
# Remplacent le rôle de "insta" comme filtre implicite de contenu créateur.
# Source : analyse clusters visuels Pinterest — "Instagram Baddie / Pretty Girl / Selfie"
_PERSON_KEYWORDS = [
    # Cluster principal — instagram model / selfie aesthetic
    "pretty girl aesthetic",
    "pretty girl selfie",
    "instagram model",
    "instagram baddie",
    "baddie aesthetic girl",
    "model aesthetic girl",
    "cute girl aesthetic",
    "hot girl aesthetic",
    # Cluster mirror selfie / lifestyle
    "mirror selfie girl",
    "iphone mirror selfie",
    "aesthetic mirror selfie",
    "casual selfie aesthetic",
    "lifestyle girl aesthetic",
    # Cluster général
    "girl aesthetic",
    # Ajouts — clusters identifiés via analyse visuelle Pinterest
    "beautiful girl aesthetic",
    "hot girl selfie",
    "model girl selfie",
    "instagram baddie aesthetic",
]

# Keywords orientés vidéo — utilisés par workflow_video_pinterest.py
# Construisent des requêtes du type : "video beach aesthetic" + "girl aesthetic"
_VIDEO_PERSON_KEYWORDS = [
    "aesthetic",
    "mirror selfie",
    "crop top outfit",
    "baddie outfit",
    "beach vibes",
    "girl aesthetic",
    "outfit video",
    "casual look",
    "summer aesthetic",
    "lifestyle video",
]


# ================================================================
# Helpers
# ================================================================

def _random_delay(min_s: float = DELAY_MIN, max_s: float = DELAY_MAX) -> None:
    delay = random.uniform(min_s, max_s)
    logger.debug(f"Pause {delay:.1f}s...")
    time.sleep(delay)


def _upgrade_image_quality(url: str) -> str:
    """
    Transforme les URLs thumbnails Pinterest en haute qualité.
    /236x/ → /736x/
    /474x/ → /736x/
    """
    for size in ("/236x/", "/474x/", "/170x/"):
        if size in url:
            return url.replace(size, "/736x/")
    return url


def _is_valid_pinterest_image(url: str) -> bool:
    """Filtre les URLs valides (images réelles, pas icônes ni assets statiques)."""
    return (
        "pinimg.com" in url
        and any(ext in url.lower() for ext in (".jpg", ".jpeg", ".png", ".webp"))
        and not any(skip in url for skip in ("75x75", "30x30", "favicon"))
    )


def _build_query(
    concept: dict,
    boost_person_kw: str | None = None,
    keyword_pool: list[str] | None = None,
) -> str:
    """
    Construit une requête Pinterest courte depuis le concept.

    MODE --relevant (keyword_pool fourni) :
        Utilise UNIQUEMENT un keyword du pool (ignore pinterest_tags location).
        Query = un seul keyword tiré aléatoirement, limité à 4 mots max.

    MODE standard (keyword_pool = None) :
        Structure : [pinterest_tag location] + [keyword personnage]
        Les mots dupliqués entre les deux parties sont automatiquement supprimés.

    Args:
        concept          : dict du concept courant (contient "location")
        boost_person_kw  : forcer un keyword personnage précis (fallback stratégie 2).
                           Si None, pioche dans keyword_pool ou _PERSON_KEYWORDS.
        keyword_pool     : pool de mots-clés alternatifs (mode --relevant).
                           Si fourni, utilise UNIQUEMENT ces keywords (ignore location).

    Returns:
        str : requête prête à encoder dans l'URL Pinterest
    """
    # ── MODE --relevant : utiliser UNIQUEMENT le keyword pool ──────
    if keyword_pool:
        keyword = boost_person_kw or random.choice(keyword_pool)
        # Limiter à 4 mots maximum
        words = keyword.split()[:4]
        query = " ".join(words)
        logger.info(f"Requête Pinterest (mode --relevant) : '{query}'")
        return query

    # ── MODE standard : combiner pinterest_tag + keyword ───────────
    import json
    from pathlib import Path

    variables_path = Path(__file__).parent / "data" / "variables.json"
    with open(variables_path, encoding="utf-8") as f:
        variables = json.load(f)
    pinterest_tags = variables.get("pinterest_tags", {})

    location_str = concept.get("location", "").lower()
    location_tag = pinterest_tags.get(location_str, location_str.split()[0] if location_str else "")

    person_kw = boost_person_kw or random.choice(_PERSON_KEYWORDS)

    # Déduplication : supprimer du person_kw les mots déjà présents dans location_tag
    location_words = set(location_tag.lower().split())
    person_words_deduped = [
        w for w in person_kw.split()
        if w.lower() not in location_words
    ]
    person_kw_clean = " ".join(person_words_deduped)

    # Combiner et limiter à 4 mots max
    combined = f"{location_tag} {person_kw_clean}".strip()
    words = combined.split()[:4]
    query = " ".join(words)

    logger.info(f"Requête Pinterest (mode standard) : '{query}'")
    return query


async def _collect_image_urls(page: Page, query: str) -> list[str]:
    """
    Navigue sur Pinterest, attend le rendu JS, retourne jusqu'à MAX_IMAGES_TO_COLLECT URLs.
    """
    encoded_q = urllib.parse.quote(query)
    url        = PINTEREST_SEARCH_URL.format(query=encoded_q)
    logger.info(f"Navigation → {url}")

    await page.goto(url, wait_until="domcontentloaded", timeout=30_000)

    # Attendre le rendu des pins (selector ou timeout)
    try:
        await page.wait_for_selector("img[src*='pinimg.com']", timeout=12_000)
    except Exception:
        logger.warning("Aucun pin détecté après 12s — possible blocage ou page vide")
        return []

    # Scroll léger pour charger plus de pins
    await page.evaluate("window.scrollTo(0, document.body.scrollHeight / 3)")
    await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    # Extraire toutes les <img> Pinterest
    raw_urls: list[str] = await page.evaluate("""
        () => {
            const imgs = document.querySelectorAll('img[src*="pinimg.com"]');
            return Array.from(imgs).map(img => img.src).filter(Boolean);
        }
    """)

    # Filtrer + dédupliquer + upgrader qualité
    seen: set[str] = set()
    cleaned: list[str] = []
    for u in raw_urls:
        hq = _upgrade_image_quality(u)
        if _is_valid_pinterest_image(hq) and hq not in seen:
            seen.add(hq)
            cleaned.append(hq)
        if len(cleaned) >= MAX_IMAGES_TO_COLLECT:
            break

    logger.info(f"{len(cleaned)} URLs collectées en mémoire (sur {len(raw_urls)} brutes)")
    return cleaned


def _download_image(url: str) -> str | None:
    """
    Télécharge une image depuis l'URL vers outputs/.
    Retourne le chemin local ou None en cas d'échec.
    """
    os.makedirs(OUTPUTS_DIR, exist_ok=True)
    try:
        headers = {"User-Agent": random.choice(USER_AGENTS)}
        resp    = requests.get(url, headers=headers, timeout=20)

        if resp.status_code == 429:
            wait = random.randint(*DELAY_ON_429)
            logger.warning(f"HTTP 429 sur download — pause {wait}s")
            time.sleep(wait)
            resp = requests.get(url, headers=headers, timeout=20)

        if resp.status_code != 200:
            logger.warning(f"Download échoué ({resp.status_code}) : {url}")
            return None

        ext      = ".jpg"
        filename = f"pinterest_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}{ext}"
        path     = os.path.join(OUTPUTS_DIR, filename)
        with open(path, "wb") as f:
            f.write(resp.content)
        logger.debug(f"Image téléchargée : {path} ({len(resp.content)} bytes)")
        return path

    except Exception as e:
        logger.error(f"Erreur download image : {e}")
        return None


def _detect_person_in_image(image_path: str) -> bool:
    """
    Appelle Gemini Vision pour détecter si un personnage humain est visible.
    Retourne True si personnage détecté, False sinon.

    Note Windows : .copy() libère immédiatement le handle fichier — évite WinError 32.
    """
    from google import genai
    from google.genai import types
    from prompts import PROMPT_PERSON_DETECTION
    from PIL import Image
    import io

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)

        # Charger + convertir en bytes (libère le handle)
        img = Image.open(image_path).copy()
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95)
        img_part = types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg")

        response = client.models.generate_content(
            model=GEMINI_MODEL_VISION,
            contents=[PROMPT_PERSON_DETECTION, img_part],
        )
        answer = response.text.strip().upper()
        logger.debug(f"Gemini détection personnage : '{answer}'")
        return answer.startswith("YES")
    except Exception as e:
        logger.error(f"Erreur Gemini détection personnage : {e}")
        return False


def _detect_upper_body_visible(image_path: str) -> bool:
    """
    Vérifie via Gemini Vision que le haut du corps est entièrement visible.

    Requis par Kling Motion Control : il rejette les vidéos où les épaules,
    le torse ou la taille ne sont pas visibles (erreur "No complete upper
    body detected in the video").

    Returns:
        True  → upper body complet visible → Kling acceptera la vidéo
        False → upper body coupé/absent   → fallback story (évite le rejet)
    """
    from google import genai
    from google.genai import types
    from prompts import PROMPT_UPPER_BODY_DETECTION
    from PIL import Image
    import io

    try:
        client = genai.Client(api_key=GEMINI_API_KEY)

        img = Image.open(image_path).copy()
        if img.mode in ("RGBA", "P", "LA"):
            img = img.convert("RGB")
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=95)
        img_part = types.Part.from_bytes(data=buf.getvalue(), mime_type="image/jpeg")

        response = client.models.generate_content(
            model=GEMINI_MODEL_VISION,
            contents=[PROMPT_UPPER_BODY_DETECTION, img_part],
        )
        answer = response.text.strip().upper()
        logger.debug(f"Gemini détection upper body : '{answer}'")
        return answer.startswith("YES")
    except Exception as e:
        logger.error(f"Erreur Gemini détection upper body : {e}")
        # En cas d'erreur de détection, on refuse le Motion Control pour éviter
        # un rejet Kling coûteux (crédits image Madison gaspillés)
        return False


def _cleanup_temp_image(path: str | None) -> None:
    """Supprime une image temporaire (rejet sans personnage)."""
    if path and os.path.exists(path):
        os.remove(path)
        logger.debug(f"Image temporaire supprimée : {path}")


# ================================================================
# Fonction principale (async interne)
# ================================================================

async def _scrape_async(concept: dict, keyword_pool: list[str] | None = None) -> tuple[str, str, str]:
    """
    Scrape Pinterest, sélectionne une image avec personnage.

    Returns:
        (local_path, source_url, search_query)

    Stratégie de retry :
      MODE --relevant (keyword_pool fourni) :
        1. Requête — keyword aléatoire du pool
        2. Fallback — keyword différent du pool
        3. Fallback ultime — keyword différent du pool

      MODE standard (keyword_pool = None) :
        1. Requête standard (location tag + person keyword aléatoire)
        2. Fallback — person keyword différent
        3. Fallback ultime — person keyword seul sans contexte location
    """
    # Construire les stratégies en fonction du mode (--relevant ou standard)
    if keyword_pool:
        # Mode --relevant : utiliser UNIQUEMENT le keyword_pool
        pool_sample = random.sample(keyword_pool, min(3, len(keyword_pool)))
        strategies = [
            {
                "label":          f"requête --relevant (pool keyword 1)",
                "person_kw":      pool_sample[0] if len(pool_sample) > 0 else None,
                "force_location": None,
            },
            {
                "label":          f"fallback --relevant (pool keyword 2)",
                "person_kw":      pool_sample[1] if len(pool_sample) > 1 else pool_sample[0],
                "force_location": None,
            },
            {
                "label":          f"fallback ultime --relevant (pool keyword 3)",
                "person_kw":      pool_sample[2] if len(pool_sample) > 2 else pool_sample[0],
                "force_location": None,
            },
        ]
    else:
        # Mode standard : combiner location + _PERSON_KEYWORDS
        strategies = [
            {
                "label":          "requête standard (location + person keyword)",
                "person_kw":      None,
                "force_location": None,
            },
            {
                "label":          "fallback — person keyword différent",
                "person_kw":      random.choice(_PERSON_KEYWORDS),
                "force_location": None,
            },
            {
                "label":          "fallback ultime — person keyword seul sans contexte location",
                "person_kw":      "pretty girl aesthetic",
                "force_location": "",
            },
        ]

    async with async_playwright() as pw:
        consecutive_failures = 0

        for strategy in strategies:
            user_agent = random.choice(USER_AGENTS)
            logger.info(f"Stratégie : {strategy['label']} | UA : {user_agent[:50]}...")

            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent=user_agent,
                viewport={"width": 1280, "height": 900},
                locale="en-US",
            )

            try:
                page = await context.new_page()

                # Gérer les popups cookie / login potentiels
                page.on("dialog", lambda d: asyncio.create_task(d.dismiss()))

                concept_for_query = concept.copy()
                if strategy.get("force_location") is not None:
                    concept_for_query["location"] = strategy["force_location"]

                query    = _build_query(
                    concept_for_query,
                    boost_person_kw=strategy["person_kw"],
                    keyword_pool=keyword_pool,
                )
                img_urls = await _collect_image_urls(page, query)

                if not img_urls:
                    consecutive_failures += 1
                    logger.warning(f"Aucune URL collectée ({strategy['label']}) — échec {consecutive_failures}/{MAX_PIPELINE_RETRIES}")
                    if consecutive_failures >= MAX_PIPELINE_RETRIES:
                        raise RuntimeError("Échec Pinterest : aucune URL collectée après 3 tentatives")
                    await browser.close()
                    await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))
                    continue

                consecutive_failures = 0

                # Mélanger pour varier la sélection
                random.shuffle(img_urls)

                # Essayer chaque URL jusqu'à trouver un personnage
                for idx, img_url in enumerate(img_urls):
                    logger.info(f"Essai image {idx + 1}/{len(img_urls)} : {img_url[:80]}...")
                    _random_delay()

                    temp_path = _download_image(img_url)
                    if not temp_path:
                        logger.warning(f"Download raté pour l'image {idx + 1} — skip")
                        continue

                    if _detect_person_in_image(temp_path):
                        logger.info(f"Personnage détecté ! Image retenue : {temp_path}")
                        await browser.close()
                        return temp_path, img_url, query

                    logger.warning(f"Aucun personnage — rejet de l'image {idx + 1}")
                    _cleanup_temp_image(temp_path)

                logger.warning(f"Liste épuisée ({len(img_urls)} images) — passage à la stratégie suivante")

            except RuntimeError:
                await browser.close()
                raise
            except Exception as e:
                consecutive_failures += 1
                logger.error(f"Erreur scraping ({strategy['label']}) : {e}")
                if consecutive_failures >= MAX_PIPELINE_RETRIES:
                    await browser.close()
                    raise RuntimeError(f"Échec Pinterest après {MAX_PIPELINE_RETRIES} tentatives : {e}")
            finally:
                try:
                    await browser.close()
                except Exception:
                    pass

            await asyncio.sleep(random.uniform(DELAY_MIN, DELAY_MAX))

    raise RuntimeError("Aucune image avec personnage trouvée après toutes les stratégies Pinterest")


# ================================================================
# Point d'entrée public (synchrone)
# ================================================================

def scrape_pinterest_image(
    concept: dict,
    keywords: list[str] | None = None,
    keyword_pool: list[str] | None = None,
) -> tuple[str, str, str]:
    """
    Orchestre le scraping Pinterest.

    Args:
        concept      : dict généré par concept_generator.generate_concept()
        keywords     : ignoré — conservé pour compatibilité descendante.
        keyword_pool : pool de mots-clés à utiliser à la place de _PERSON_KEYWORDS
                       (activé via --relevant dans main.py).

    Returns:
        (local_path, source_url, search_query)
        - local_path   : chemin local de l'image retenue dans outputs/
        - source_url   : URL Pinterest originale de l'image (i.pinimg.com)
        - search_query : requête exacte tapée sur Pinterest

    Raises:
        RuntimeError : si aucune image valide n'est trouvée après toutes les stratégies
    """
    logger.info("=== Pinterest scraper démarré ===")
    logger.info(f"Concept : {concept}")
    if keyword_pool:
        logger.info(f"Mode relevant — pool de {len(keyword_pool)} keywords")

    local_path, source_url, search_query = asyncio.run(_scrape_async(concept, keyword_pool=keyword_pool))
    logger.info(f"=== Pinterest scraper terminé → {local_path} ===")
    return local_path, source_url, search_query


# ================================================================
# Extraction image depuis une épingle individuelle (URL unique)
# ================================================================

async def _scrape_pin_image_async(pin_url: str) -> str:
    """
    Extrait et télécharge l'image haute qualité depuis une URL d'épingle Pinterest.

    Stratégies (par ordre) :
      1. __PWS_DATA__ JSON embarqué — clé "736x" ou "originals"
      2. Balise og:image
      3. Première <img> pinimg du DOM

    Returns:
        str : chemin local vers l'image téléchargée dans outputs/
    """
    import json as _json

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            ua = random.choice(USER_AGENTS)
            context = await browser.new_context(
                user_agent=ua,
                locale="fr-FR",
                timezone_id="Europe/Paris",
            )
            page = await context.new_page()
            logger.info(f"Navigation vers l'épingle : {pin_url}")
            await page.goto(pin_url, wait_until="domcontentloaded", timeout=30_000)
            _random_delay(1.5, 3.0)

            image_url: str | None = None

            # Méthode 1 : __PWS_DATA__ JSON
            try:
                pws_raw = await page.evaluate("""
                    () => {
                        const el = document.getElementById('__PWS_DATA__');
                        return el ? el.textContent : null;
                    }
                """)
                if pws_raw:
                    def _find_image_in_obj(obj: object, depth: int = 0) -> str | None:
                        if depth > 15:
                            return None
                        if isinstance(obj, dict):
                            for size_key in ("736x", "originals", "474x"):
                                val = obj.get(size_key)
                                if isinstance(val, dict):
                                    url = val.get("url", "")
                                    if url and "pinimg.com" in url:
                                        return url
                            for v in obj.values():
                                result = _find_image_in_obj(v, depth + 1)
                                if result:
                                    return result
                        elif isinstance(obj, list):
                            for item in obj:
                                result = _find_image_in_obj(item, depth + 1)
                                if result:
                                    return result
                        return None
                    image_url = _find_image_in_obj(_json.loads(pws_raw))
                    if image_url:
                        logger.info(f"Image URL depuis __PWS_DATA__ : {image_url}")
            except Exception as e:
                logger.debug(f"__PWS_DATA__ parsing échoué : {e}")

            # Méthode 2 : og:image
            if not image_url:
                try:
                    og = await page.get_attribute('meta[property="og:image"]', "content")
                    if og and "pinimg.com" in og:
                        image_url = _upgrade_image_quality(og)
                        logger.info(f"Image URL depuis og:image : {image_url}")
                except Exception:
                    pass

            # Méthode 3 : première <img> pinimg dans le DOM
            if not image_url:
                try:
                    dom_urls: list[str] = await page.evaluate("""
                        () => Array.from(document.querySelectorAll('img[src*="pinimg.com"]'))
                            .map(i => i.src)
                            .filter(s => !s.includes('75x75') && !s.includes('30x30'))
                    """)
                    if dom_urls:
                        image_url = _upgrade_image_quality(dom_urls[0])
                        logger.info(f"Image URL depuis DOM : {image_url}")
                except Exception:
                    pass

            await context.close()
        finally:
            await browser.close()

    if not image_url:
        raise ValueError(f"Aucune image trouvée sur l'épingle : {pin_url}")

    # Téléchargement
    filename = f"pin_{datetime.now().strftime('%Y%m%d_%H%M%S')}.jpg"
    dest     = os.path.join(OUTPUTS_DIR, filename)
    os.makedirs(OUTPUTS_DIR, exist_ok=True)

    resp = requests.get(
        image_url, timeout=20,
        headers={"User-Agent": random.choice(USER_AGENTS)},
    )
    if resp.status_code != 200:
        raise RuntimeError(
            f"Téléchargement image pin échoué ({resp.status_code}) : {image_url}"
        )

    with open(dest, "wb") as f:
        f.write(resp.content)

    logger.info(f"Image pin téléchargée : {dest} ({len(resp.content) // 1024} KB)")
    return dest


def scrape_image_from_pin_url(pin_url: str) -> str:
    """
    Télécharge l'image depuis une URL d'épingle Pinterest individuelle.

    Args:
        pin_url : URL de l'épingle (ex: https://fr.pinterest.com/pin/123456789/)

    Returns:
        str : chemin local vers l'image téléchargée dans outputs/

    Raises:
        ValueError  : si aucune image n'est trouvée sur la page
        RuntimeError: si le téléchargement échoue
    """
    logger.info(f"=== scrape_image_from_pin_url : {pin_url} ===")
    local_path = asyncio.run(_scrape_pin_image_async(pin_url))
    logger.info(f"=== Image pin extraite : {local_path} ===")
    return local_path
