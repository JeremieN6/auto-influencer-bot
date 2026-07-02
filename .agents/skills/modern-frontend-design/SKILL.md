---
name: modern-frontend-design
description: 'Design or redesign modern frontend interfaces for landing pages, SaaS apps, auth flows, dashboards, and marketing pages. Use when asked to modernize UI, improve visual hierarchy, choose typography or color direction, add motion, create a distinctive layout, or avoid generic AI-slop design.'
argument-hint: 'Describe the page, component, audience, and constraints'
user-invocable: true
---

# Modern Frontend Design

## Outcome

Use this skill to turn a vague design request into a concrete interface direction that is visually specific, implementation-ready, and adapted to the existing product.

The expected output is:
- A clear visual concept in plain language
- Layout and hierarchy decisions tied to the product goal
- A concrete typography, color, spacing, and motion direction
- A UI implementation plan that fits the current stack
- A short validation pass for responsiveness and polish

## When To Use

Use this skill when the task involves:
- Redesigning a landing page, dashboard, form flow, pricing page, or feature section
- Making an interface feel more premium, modern, or intentional
- Replacing a generic or boilerplate-looking layout
- Choosing fonts, colors, background treatments, motion, or composition
- Creating a new frontend surface from scratch
- Refreshing an existing page without breaking the current design system

Do not use this skill for:
- Pure bug fixing with no design change
- Backend-only work
- Copywriting-only tasks
- Strict design-system maintenance where no visual deviation is allowed

## Inputs To Gather

Before changing code, identify:
1. The surface being designed: landing page, authenticated app, settings page, checkout, blog, form, or component set
2. The primary user action: convert, compare, upload, configure, purchase, read, or manage
3. Brand constraints: existing logo, palette, tone, industry expectations, screenshots, or no constraints
4. Current UI maturity: solid design system to preserve, partial direction to extend, or generic baseline to replace
5. Content density: narrative-heavy, data-heavy, or action-heavy
6. Technical constraints: framework, CSS strategy, existing component library, performance limits, and mobile requirements

If the request is underspecified, make the smallest reasonable assumption set and state it briefly.

## Core Design Principles

1. Avoid interchangeable layouts. The page should have a recognizable visual idea, not a safe default shell.
2. Preserve existing visual language when it is already coherent. Extend before replacing.
3. If the current UI is weak or generic, introduce a stronger concept through typography, composition, atmosphere, and rhythm.
4. Use expressive type choices. Do not default to Inter, Roboto, Arial, or the system stack unless the existing product already depends on them.
5. Define color intentionally. Avoid purple-on-white defaults and avoid relying on flat single-color backgrounds.
6. Use motion sparingly but meaningfully: staged reveals, focus shifts, hover states, and transitions that support hierarchy.
7. Design for both desktop and mobile from the start, not as a cleanup step.
8. Keep implementation pragmatic. Bold does not mean fragile.

## Decision Rules

### 1. Existing System Or New Direction

- If the product already has a clear design system, preserve its spacing logic, component language, and tone.
- If the UI is inconsistent or generic, define a fresh but compatible direction and apply it consistently to the touched surface.

### 2. Marketing Surface Or Product Surface

- For marketing pages, favor strong first impression, narrative sequencing, proof, and scroll rhythm.
- For product surfaces, favor speed of comprehension, action clarity, density control, and repeatable components.

### 3. Content-Led Or Action-Led

- If content is the product, invest in readable measure, section pacing, editorial hierarchy, and illustration of ideas.
- If action is the product, reduce friction, foreground the primary action, and remove decorative noise around critical steps.

### 4. High Freedom Or Tight Constraints

- If brand freedom is high, establish a distinct concept with custom typography, palette roles, and stronger layout moves.
- If constraints are tight, create freshness through composition, spacing, contrast, and motion rather than a full visual reset.

## Procedure

### 1. Read The Immediate Surface

Inspect the local route, component, page structure, and styles that already control the requested area.

Look for:
- Existing layout patterns worth preserving
- Repeated spacing or sizing tokens
- Typography and color choices already in use
- Components that can be restyled instead of duplicated
- Clear weaknesses such as cramped sections, weak hierarchy, or generic cards

### 2. Write A One-Sentence Creative Direction

Define the page in one sentence before designing it.

Examples:
- Calm, editorial SaaS landing page with strong data credibility
- Sharp, high-contrast product UI that makes upload and generation feel instant
- Warm, premium conversion page with tactile cards and restrained motion

If you cannot describe the direction in one sentence, the design is still too vague.

### 3. Choose The Visual System

Specify:
- Typography: heading font, body font, weight strategy, casing rules, and line-height approach
- Color roles: background, surface, text, accent, muted, border, and call-to-action
- Atmosphere: gradients, textures, shadows, shapes, or subtle patterns
- Motion: what moves, when it moves, and why
- Density: airy, compact, or mixed by section priority

Prefer CSS variables or equivalent theme tokens when touching shared styling.

### 4. Restructure For Hierarchy

Adjust the page so the layout reflects the product goal.

For landing pages:
Use this section order as the default skeleton for SaaS landing pages, adapting content per project:

1. **Barre de navigation** — logo, menu, CTA principal
2. **Hero section** — titre axé sur le bénéfice principal, sous-titre clair (PVU), CTA principal (et éventuellement une action secondaire)
3. **Visuel** — aperçu produit (dashboard screenshot ou vidéo/motion design) juste sous le hero
4. **Logos clients** *(optionnel)* — preuve sociale via logos connus. À ajouter seulement quand le projet a une traction suffisante (clients reconnus, early stage skip this section entièrement plutôt que de la remplir avec du faux contenu)
5. **Section bénéfices** — ce que fait le produit et les avantages principaux, formulé en bénéfices (pas en features techniques)
6. **Comment ça marche** — le process en 3 étapes, le plus clair possible
7. **Présentation des offres** — mettre en avant le plan principal à vendre, en parlant valeur/bénéfices plutôt que juste le prix
8. **Témoignages clients** — vrais témoignages avec visage, nom, fonction, vérifiables ; vidéo si possible
9. **FAQ** — répondre aux objections en amont, avec un CTA de contact pour les questions restantes
10. **CTA final** — bannière simple (titre fort + texte court + CTA) juste avant le footer, qui relance l'action principale. Cette section est importante : ne jamais la sauter, c'est le dernier point de conversion avant que l'utilisateur quitte la page
11. **Footer** — logo, menu, mentions légales

Not every section is mandatory for every project (e.g. skip témoignages if none exist yet), but the CTA final banner and hero should always be present.

For authenticated app pages:
- Make the primary task obvious in the first viewport
- Group secondary actions instead of flattening everything into a single panel wall
- Use cards, tables, and side panels only when they clarify the workflow

For forms and auth flows:
- Reduce peripheral clutter
- Keep the main action visually dominant
- Add trust cues, feedback states, and clear recovery paths

### 5. Make At Least One Bold Move

Each design should include one deliberate visual move that prevents it from feeling template-generated.

Examples:
- An asymmetric hero composition
- A strong type scale with compressed or serif display text
- A layered background treatment
- A differently shaped section transition
- A sharper contrast strategy for the main action area

Keep this move compatible with usability and implementation cost.

### 6. Implement In The Existing Stack

Translate the direction into the project's frontend stack without overbuilding.

Guidelines:
- Reuse existing components when structure is already correct
- Prefer small, local component changes before introducing new abstractions
- Centralize shared tokens when multiple sections depend on them
- Keep animations lightweight and avoid JS-heavy effects unless necessary
- Match the repo's framework and styling conventions

### 7. Run A Responsive And Polish Pass

Before considering the task complete, verify:
- Mobile layout remains intentional, not just stacked
- Headings wrap cleanly at common widths
- Spacing scale holds together across sections
- Interactive states are visible and consistent
- Contrast and readability are acceptable
- The design still feels coherent without large screens or hover interactions

## Completion Checks

The work is complete only if all of the following are true:
- The visual direction can be summarized in one sentence
- The UI feels specific to the product, not interchangeable with a starter template
- Typography and color choices are intentional and consistent
- The main action is obvious within seconds
- Background and surface treatment add atmosphere without obscuring content
- Motion, if present, supports hierarchy rather than adding noise
- Desktop and mobile both look designed
- The implementation respects the current codebase conventions

## Response Pattern

When using this skill, structure the work output around:
1. What is being redesigned and what goal it serves
2. The chosen visual concept
3. The main layout and hierarchy changes
4. The styling system decisions
5. The implementation plan or changes made
6. The final validation notes

## Example Prompts

- /modern-frontend-design Refresh the landing page hero for a CSV-to-PowerPoint SaaS aimed at consultants
- /modern-frontend-design Redesign the upload flow so it feels faster, clearer, and more premium on mobile and desktop
- /modern-frontend-design Modernize this dashboard without changing the underlying Vue component structure too much
- /modern-frontend-design Create a distinct pricing page direction that feels confident and not template-based