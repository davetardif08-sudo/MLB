# /deploy — Déployer MLB Analyzer sur Fly.io

Déploie les changements locaux sur Fly.io en production.

## Étapes

1. Affiche les fichiers modifiés (`git status`)
2. Demande un message de commit à l'utilisateur (ou génère-en un basé sur les changements)
3. `git add -A`
4. `git commit -m "<message>"`
5. `git push`
6. `fly deploy --app mlb` (ou le nom de l'app Fly.io)
7. Confirme que le déploiement est réussi

## Notes
- Toujours vérifier `git diff` avant de committer pour s'assurer que les changements sont corrects
- Si le déploiement échoue, afficher les logs avec `fly logs -a mlb`
- L'app tourne sur https://mlb.fly.dev/
