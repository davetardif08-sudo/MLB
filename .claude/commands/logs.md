# /logs — Voir les logs Fly.io en direct

Affiche les logs récents de l'app MLB Analyzer en production.

## Étapes

1. Exécute `fly logs -a mlb` pour streamer les logs en direct
2. Si l'utilisateur veut filtrer (ex: erreurs seulement, auto-snapshot, etc.), utilise `fly logs -a mlb | grep "<filtre>"`

## Filtres utiles
- `auto-snapshot` → voir les sauvegardes automatiques
- `ERROR` ou `Erreur` → voir les erreurs
- `lanceurs` → voir le chargement des lanceurs partants
- `startup` → voir le démarrage du serveur
