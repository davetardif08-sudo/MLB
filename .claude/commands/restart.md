# /restart — Redémarrer le serveur local (port 5003)

Redémarre proprement le serveur Flask MLB en local.

## Étapes (dans l'ordre strict)

1. Trouver les PIDs sur le port 5003 :
   ```
   netstat -ano 2>/dev/null | grep ":5003.*LISTEN"
   ```

2. Tuer chaque PID trouvé :
   ```
   python3 -c "import subprocess; subprocess.run(['taskkill', '/F', '/PID', 'PID_ICI'], shell=True)"
   ```
   ⚠️ Ne pas utiliser `taskkill /PID xxx /F` directement depuis Git Bash (bug de conversion de chemin)

3. Vérifier que le port est libre :
   ```
   netstat -ano 2>/dev/null | grep ":5003.*LISTEN" || echo "Port libre"
   ```

4. Relancer avec le venv :
   ```
   cd "C:\Users\DaveTardif\Documents\Claude\mlb-analyzer"
   venv/Scripts/python.exe app.py > /tmp/mlb_app.log 2>&1 &
   sleep 4
   netstat -ano 2>/dev/null | grep ":5003.*LISTEN" && echo "Serveur OK"
   ```

## Notes
- Toujours utiliser `venv/Scripts/python.exe` (pas `python`) pour avoir playwright disponible
- Si plusieurs PIDs sur 5003 → tuer TOUS avant de relancer
