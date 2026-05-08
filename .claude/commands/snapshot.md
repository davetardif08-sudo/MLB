# /snapshot — Forcer une sauvegarde du snapshot aujourd'hui

Déclenche manuellement l'auto-snapshot en production via l'endpoint cron.

## Étapes

1. Appelle `curl -s https://mlb.fly.dev/api/cron/auto-snapshot` et affiche la réponse JSON
2. Interprète le résultat :
   - `"snapshot_saved": true` → succès, affiche le nombre de picks sauvegardés
   - `"skipped": "already_done_today"` → déjà sauvegardé aujourd'hui, pas d'action nécessaire
   - `"skipped": "before_target_window"` → trop tôt (30 min avant le 1er match pas encore atteint), affiche l'heure cible
   - `"skipped": "no_picks_today"` → aucun pick en cache, l'analyse n'est peut-être pas terminée
   - `"ok": false` → erreur, afficher le message d'erreur

## Notes
- Le snapshot se sauvegarde normalement 30 min avant le premier match du jour
- Pour forcer même si déjà fait : supprimer le lock via `fly ssh console -a mlb` puis `rm /data/last_auto_snapshot.txt`
