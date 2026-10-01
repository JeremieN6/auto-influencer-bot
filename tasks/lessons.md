# Lessons

## 2026-10-01 — Coûts Gemini qui continuent malgré la pause
- **Erreur** : on a cru que la pause Telegram ne coupait pas la génération. En réalité la pause tenait depuis le 11/08 (aucun appel Gemini dans `run.log` après le 10/08) ; les coûts venaient d'un autre consommateur du projet GCP.
- **Règle** : avant de corriger une fuite de coûts, vérifier dans les logs de prod la date du dernier appel API réel, puis lister tous les consommateurs de la clé/du projet.
- **Faille réelle corrigée au passage** : `--force` (commandes /run, /manualGeneration) et `--resume-kling` contournaient la pause. Une pause « coût » doit bloquer tous les chemins, avec un coupe-circuit au niveau du client API (`guard_gemini_client`), pas seulement à l'entrée du cron.
