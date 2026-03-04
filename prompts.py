"""
prompts.py — Tous les prompts du système centralisés ici.

Convention de nommage :
  PROMPT_<NOM_FONCTIONNEL>

Note : Les prompts issus du dossier /PROMPTS/ sont également migrés ici
pour avoir une source unique de vérité. Les fichiers .md restent comme
référence lisible mais ne sont pas utilisés directement par le code.
"""

# ================================================================
# PROMPT 1 — Fiche référence 3 angles
# Usage : une seule fois lors de la création d'une nouvelle influenceuse.
# ================================================================
PROMPT_REF_SHEET = """
Create a professional character reference sheet of this exact character.
Perfect character consistency and exact 1:1 likeness across all 3 panels.
Vertical composite sheet — three equal horizontal rows.
Pure white (#FFFFFF) background. 3 close-ups: Front View, 45-Degree Angle, Side Profile.
True-to-life photography: real skin pores, fine lines, natural color variation.
Professional portrait photography, organic depth of field, no digital smoothing.
4K quality. No duplicated panels. No inconsistent features.
"""

# ================================================================
# PROMPT 2 — Image to JSON
# Usage : workflow_pinterest V1 (analyse de l'image Pinterest scrappée)
#         workflow_backup (analyse d'une image source manuelle)
# ================================================================
PROMPT_IMAGE_TO_JSON = """
You are an advanced Computer Vision & Data Serialization Engine.
Analyze the image. Capture 100% of visual data: SCENE, ENVIRONMENT, POSE, OBJECTS, COMPOSITION.
Do NOT describe facial features — they come from the character reference sheet.
Return ONLY a valid JSON object, no markdown fencing:

{
  "meta": { "image_quality": "", "image_type": "", "aspect_ratio": "" },
  "character_reference": {
    "instruction": "Use attached reference sheet as ground truth for facial features."
  },
  "global_context": {
    "scene_description": "", "time_of_day": "", "weather_atmosphere": "",
    "lighting": { "source": "", "direction": "", "quality": "", "color_temp": "" }
  },
  "color_palette": { "dominant_hex_estimates": [], "accent_colors": [], "contrast_level": "" },
  "composition": { "camera_angle": "", "framing": "", "depth_of_field": "", "focal_point": "" },
  "subject": {
    "pose": { "body_position": "", "gesture": "", "head_angle": "", "body_angle": "", "expression_mood": "" },
    "clothing": { "outfit_description": "", "style": "", "colors": [], "fabric_details": [], "accessories": [] },
    "position_in_frame": "", "prominence": ""
  },
  "objects": [],
  "text_ocr": { "present": false, "content": [] },
  "semantic_relationships": []
}

CRITICAL: No physical appearance in subject section (no hair/eyes/skin/age).
aspect_ratio must be one of: 1:1, 2:3, 3:2, 3:4, 4:3, 4:5, 5:4, 9:16, 16:9, 21:9.
"""

# ================================================================
# PROMPT 3 — JSON to PRO Image
# Usage : workflow_pinterest V1, workflow_generatif V2
# Le JSON de scène est injecté dynamiquement avant l'envoi à Gemini.
# ================================================================
PROMPT_JSON_TO_IMAGE = """
Generate a professional image featuring the character in the attached reference sheet.
Use the JSON below as the scene description. Respect all compositional, lighting,
and environmental details. The character's face and appearance must match the reference sheet exactly.
[CHARACTER REFERENCE IMAGE ATTACHED]

Scene JSON:
{scene_json}
"""

# ================================================================
# PROMPT 4 — Détection personnage (workflow Pinterest V1)
# Usage : filtre les images Pinterest sans personnage humain visible.
# ================================================================
PROMPT_PERSON_DETECTION = """
Does this image contain a human person clearly visible?
Answer only with YES or NO. No explanation.
"""

# ================================================================
# PROMPT 5 — Génération JSON de scène depuis variables (workflow génératif V2)
# Usage : workflow_generatif.py — Claude imagine la scène depuis les paramètres.
# {parameters_injected_dynamically} est remplacé à l'appel.
# ================================================================
PROMPT_GENERATIVE_SCENE = """
You are a creative photography director. Based on the following parameters,
generate a complete scene description in JSON format as if you were describing
a real photograph. Be specific, creative and visually coherent.
The JSON must follow this exact structure:

{parameters}

Return ONLY the valid JSON object. No markdown fencing. No explanation.
The scene must feel natural, authentic, and Instagram-worthy.
Do NOT describe any facial features — only the scene, environment, pose, clothing, lighting.
"""

# ================================================================
# PROMPT 6 — Génération caption (Claude API)
# Usage : caption_generator.py — appelé depuis concept_generator.build_caption_prompt()
# Les placeholders sont remplis dans build_caption_prompt().
# ================================================================
PROMPT_CAPTION_TEMPLATE = """\
Tu es le ghostwriter de {influencer_name}, influenceuse lifestyle/skincare.
Génère une caption Instagram naturelle, première personne, ton décontracté et authentique.

Concept du post : {concept_description}
Type de post : {post_type}
Hashtags : {include_hashtags}

Structure :
- 1 ligne d'accroche émotionnelle ou situationnelle
- 2-3 lignes naturelles, pas promotionnel
- 1 CTA léger optionnel
{hashtag_instruction}

Réponds uniquement avec la caption, sans guillemets ni commentaires.\
"""

# ================================================================
# PROMPT 7 — Validation saisie libre /run (V2)
# Usage : caption_generator.validate_custom_input()
# Vérifie que la saisie "Autre" est cohérente avec le style de l'influenceuse.
# ================================================================
PROMPT_VALIDATE_INPUT = """
You are a content moderation assistant for an Instagram influencer bot.
The influencer style is: {influencer_style}

The user submitted this custom parameter for "{field}": "{value}"

Evaluate if this input is:
1. Coherent with the influencer's style and niche
2. Reasonable length (not too long or too short)
3. Not offensive or completely off-topic

Answer with JSON only:
{{"valid": true/false, "reason": "short explanation if invalid"}}
"""

# ================================================================
# BLOCS HASHTAGS — par niche
# Ajouter un bloc par niche/influenceuse si nécessaire.
# ================================================================
HASHTAG_BLOCK_SKINCARE = (
    "#skincare #glowup #selfcare #morningroutine #clearskin "
    "#skincareaddict #wellness #beautytips #lifestyle #naturalskin"
)

HASHTAG_BLOCK_FITNESS = (
    "#fitness #workout #fitlife #gym #motivation "
    "#healthylifestyle #fitnessgoals #bodypositive #training #activewear"
)

HASHTAG_BLOCK_TRAVEL = (
    "#travel #wanderlust #explore #adventure #travelgram "
    "#instatravel #passionpassport #travellife #aroundtheworld #travelblogger"
)
