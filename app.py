"""
MLB Analyzer — Serveur web Flask (port 5003)
"""

import json
import os
import time
import threading
from datetime import datetime, timedelta
from pathlib import Path

# ─── Fuseau horaire Montréal ──────────────────────────────────────────────────
# Railway tourne en UTC. La saison MLB = avril–octobre = EDT (UTC-4).
# On fixe UTC-4 pour tout l'app (hors-saison peu importe, MLB pas actif).
_MTL_OFFSET = timedelta(hours=4)

def _now_mtl() -> datetime:
    """Retourne l'heure actuelle en heure de Montréal (EDT = UTC-4)."""
    return datetime.utcnow() - _MTL_OFFSET

def _today_mtl() -> str:
    """Retourne la date d'aujourd'hui en format YYYY-MM-DD, heure Montréal."""
    return _now_mtl().strftime("%Y-%m-%d")
from flask import Flask, render_template, jsonify, request, redirect, url_for
from flask_login import LoginManager, UserMixin, login_user, logout_user, login_required, current_user

app = Flask(__name__, template_folder='templates', static_folder='static')
app.config['TEMPLATES_AUTO_RELOAD'] = True
app.config['SECRET_KEY'] = os.environ.get('SECRET_KEY', 'mlb-dev-secret-key')

# ─── Flask-Login Configuration ───────────────────────────────────────────────────
login_manager = LoginManager()
login_manager.init_app(app)
login_manager.login_view = 'login'

class User(UserMixin):
    """Simple user model for login"""
    def __init__(self, username):
        self.id = username
        self.username = username

@login_manager.user_loader
def load_user(username):
    """Load user from session"""
    return User(username)

# Credentials depuis env vars (defaults : admin / password)
_LOGIN_USERNAME = os.environ.get('APP_USERNAME', 'admin')
_LOGIN_PASSWORD = os.environ.get('APP_PASSWORD', 'password')

# ─── Cache en mémoire ─────────────────────────────────────────────────────────

_cache = {
    "data":      None,
    "timestamp": None,
    "status":    "idle",
    "error":     None,
    "date":      None,
    "mode":      "standard",
}
_lock = threading.Lock()

_SCRAPE_TTL = 2 * 3600  # 2 heures (cotes stables, évite scraping fréquent sur Railway)
_scrape_cache: tuple | None = None  # (list[Match], timestamp)
_scrape_lock = threading.Lock()

DEFAULT_BANKROLL     = None  # Pas de bankroll fixe — on utilise max_nightly directement
DEFAULT_KELLY_FRAC   = 0.25
DEFAULT_MAX_NIGHTLY  = 10.0   # Mise maximale par soir en $

# Répertoire persistant : /data sur Fly.io (volume monté), sinon dossier local
_DATA_DIR           = os.environ.get("DATA_DIR", os.path.dirname(__file__))
_SNAPSHOT_PATH      = os.path.join(_DATA_DIR, "snapshot.json")
_SNAPSHOTS_DIR      = os.path.join(_DATA_DIR, "snapshots")
_AUTO_SNAPSHOT_LOCK = os.path.join(_DATA_DIR, "last_auto_snapshot.txt")


def _check_date_rollover():
    """Invalide le cache si on a changé de jour."""
    today = _today_mtl()
    with _lock:
        cached_date = _cache.get("date")
        if cached_date and cached_date != today and _cache["status"] == "ready":
            _cache["status"] = "idle"
            _cache["data"]   = None
            _cache["date"]   = None
    with _scrape_lock:
        pass  # scrape_cache sera expiré naturellement par TTL


def _scrape_cached(headless: bool = True):
    """Retourne les matchs depuis le cache ou re-scrape si périmé."""
    global _scrape_cache
    now = time.time()

    with _scrape_lock:
        if _scrape_cache is not None:
            data, ts = _scrape_cache
            if now - ts < _SCRAPE_TTL:
                print(f"  >> Cache scrape frais ({int(now - ts)}s)")
                return data

        print("  >> Re-scrape Mise-O-Jeu MLB...")
        from scraper import scrape_sync
        result = scrape_sync(headless=headless)

        if not result:
            print("  >> Résultat vide — réessai dans 8s...")
            time.sleep(8)
            result2 = scrape_sync(headless=headless)
            if result2:
                print(f"  >> Réessai réussi ({len(result2)} matchs)")
                result = result2
            else:
                print("  >> Réessai échoué")

        if result:
            _scrape_cache = (result, time.time())

        return result or []


# ─── Routes principales ───────────────────────────────────────────────────────

@app.route('/')
@login_required
def index():
    return render_template('index.html')


@app.route('/api/status')
def api_status():
    with _lock:
        return jsonify({
            "status":    _cache["status"],
            "timestamp": _cache["timestamp"],
            "error":     _cache["error"],
        })


def _start_analysis_thread(bankroll, kelly_frac, max_nightly, top_n, mode="standard"):
    """Lance l'analyse en arrière-plan (thread daemon).

    Enregistre TOUJOURS les 2 modes (standard + conservateur) pour comparaison historique.
    Le paramètre `mode` détermine seulement ce qui s'affiche au dashboard.
    """
    def _run():
        with _lock:
            _cache["status"] = "loading"
            _cache["error"]  = None
            _cache["mode"]   = mode

        try:
            matches = _scrape_cached()
            if not matches:
                # Aucune source disponible — cache vide mais valide pour ne pas bloquer le dashboard
                print("[app] Aucun match disponible (MLB.com et Loto-Québec vides)")
                empty_payload = _build_payload([], [], bankroll, kelly_frac, max_nightly,
                                               all_picks=[], mode=mode)
                empty_payload["carousel_matches"] = []
                with _lock:
                    _cache["status"]    = "ready"
                    _cache["data"]      = empty_payload
                    _cache["timestamp"] = _now_mtl().strftime("%H:%M:%S")
                    _cache["date"]      = _today_mtl()
                return

            # Filtrer les matchs avec cotes pour l'analyse (les autres iront au carousel)
            matches_with_odds = [m for m in matches if m.bet_groups]
            print(f"[app] {len(matches)} matchs total, {len(matches_with_odds)} avec cotes pour analyse")

            if not matches_with_odds:
                # Pas de cotes disponibles — afficher seulement le carousel
                print("[app] Aucune cote disponible — affichage carousel uniquement")
                empty_payload = _build_payload([], matches, bankroll, kelly_frac, max_nightly,
                                               all_picks=[], mode=mode)
                with _lock:
                    _cache["status"]    = "ready"
                    _cache["data"]      = empty_payload
                    _cache["timestamp"] = _now_mtl().strftime("%H:%M:%S")
                    _cache["date"]      = _today_mtl()
                return

            # Pour l'analyse, on n'utilise que les matchs avec cotes
            matches_for_analysis = matches_with_odds

            from analyzer import OddsAnalyzer
            from predictions import record_opportunity

            analyzer = OddsAnalyzer()
            analyzed = analyzer.analyze_matches(matches_for_analysis)

            # ── Générer et enregistrer STANDARD + CONSERVATEUR en parallèle ──
            all_modes_data = {}
            for current_mode in ["standard", "conservative"]:
                opps = analyzer.get_top_opportunities(
                    analyzed, n=top_n,
                    bankroll=bankroll,
                    kelly_fraction=kelly_frac,
                    mode=current_mode,
                )
                # Toutes les prédictions (sans filtres O/U stricts)
                all_picks = analyzer.get_top_opportunities(
                    analyzed, n=top_n,
                    bankroll=bankroll,
                    kelly_fraction=kelly_frac,
                    info_mode=True,
                    mode=current_mode,
                )
                # Si aucun pari ne passe les filtres stricts, utiliser les meilleures prédictions
                if not opps and all_picks:
                    opps = all_picks

                # Enregistrer tous les picks pour ce mode
                try:
                    opp_keys = {(o.match.date, o.match.home_team, o.match.away_team, o.bet_type, o.selection_label)
                               for o in opps}
                    extra_picks = [p for p in all_picks
                                  if (p.match.date, p.match.home_team, p.match.away_team, p.bet_type, p.selection_label)
                                     not in opp_keys]
                    all_to_record = opps + extra_picks
                    for opp in all_to_record:
                        record_opportunity({
                            "date":           opp.match.date,
                            "time":           opp.match.time,
                            "home_team":      opp.match.home_team,
                            "away_team":      opp.match.away_team,
                            "bet_type":       opp.bet_type,
                            "selection":      opp.selection_label,
                            "odds":           opp.odds,
                            "fair_prob":      opp.fair_prob,
                            "math_prob":      opp.math_prob,
                            "value_score":    opp.value_score,
                            "recommendation": opp.recommendation,
                            "sport":          "baseball",
                            "kelly_fraction": opp.kelly_fraction,
                            "analysis_mode":  current_mode,
                        })
                except Exception:
                    pass

                # Stocker les données pour le mode affiché
                if current_mode == mode:
                    all_modes_data[mode] = {
                        "opps": opps,
                        "all_picks": all_picks,
                    }

            # Utiliser les données du mode affiché pour le payload
            display_data = all_modes_data.get(mode, {"opps": [], "all_picks": []})
            payload = _build_payload(display_data["opps"], matches, bankroll, kelly_frac, max_nightly,
                                     all_picks=display_data["all_picks"], mode=mode)

            with _lock:
                _cache["status"]    = "ready"
                _cache["data"]      = payload
                _cache["timestamp"] = _now_mtl().strftime("%H:%M:%S")
                _cache["date"]      = _today_mtl()

        except Exception as e:
            print(f"[app] Erreur analyse: {e}")
            with _lock:
                _cache["status"] = "error"
                _cache["error"]  = str(e)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


@app.route('/api/analyze')
def api_analyze():
    """Lance une analyse complète et retourne les opportunités."""
    _check_date_rollover()

    kelly_frac  = float(request.args.get('kelly', DEFAULT_KELLY_FRAC))
    max_nightly = float(request.args.get('max_nightly', DEFAULT_MAX_NIGHTLY))
    bankroll    = max_nightly
    top_n       = int(request.args.get('top', 20))
    mode        = request.args.get('mode', 'standard')
    if mode not in ('standard', 'conservative'):
        mode = 'standard'

    with _lock:
        status       = _cache["status"]
        data         = _cache["data"]
        cached_mode  = _cache.get("mode", "standard")

    # Changement de mode → forcer un nouveau run
    if status == "ready" and data and cached_mode != mode:
        status = None
        data = None

    # Données prêtes → retour immédiat (même si le scrape est périmé)
    if status == "ready" and data:
        # Si le scrape cache est périmé, lancer un refresh en arrière-plan
        with _scrape_lock:
            stale = (_scrape_cache is None or
                     time.time() - _scrape_cache[1] > _SCRAPE_TTL)
        if stale:
            with _lock:
                already_loading = _cache["status"] == "loading"
            if not already_loading:
                print("  >> Scrape périmé — refresh arrière-plan lancé")
                with _lock:
                    _cache["status"] = "loading"
                _start_analysis_thread(bankroll, kelly_frac, max_nightly, top_n, mode=mode)
        return jsonify(data)

    # Analyse déjà en cours → 202 (le frontend va poller)
    if status == "loading":
        return jsonify({"status": "loading", "message": "Analyse en cours..."}), 202

    # Aucune donnée — lancer et attendre max 8s, puis retourner 202
    t = _start_analysis_thread(bankroll, kelly_frac, max_nightly, top_n, mode=mode)
    t.join(timeout=8)

    with _lock:
        if _cache["status"] == "ready" and _cache["data"]:
            return jsonify(_cache["data"])
        elif _cache["status"] == "error":
            return jsonify({"error": _cache["error"]}), 500
        else:
            return jsonify({"status": "loading", "message": "Analyse en cours..."}), 202


@app.route('/api/data')
def api_data():
    """Retourne les données en cache ou déclenche une analyse."""
    _check_date_rollover()
    with _lock:
        if _cache["status"] == "ready" and _cache["data"]:
            data = dict(_cache["data"])
            # Ajouter tous les matchs du jour pour le carousel (même sans cotes)
            # Le carousel affichera les matchs sans cotes en gris ou desactivés
            try:
                all_matches = _scrape_cached()
                if all_matches:
                    # Créer des opportunities pour les matchs sans cotes (pour le carousel)
                    carousel_matches = []
                    seen_keys = set()
                    for opp in data.get("opportunities", []):
                        key = (opp["home_team"], opp["away_team"])
                        seen_keys.add(key)
                        carousel_matches.append(opp)

                    # Ajouter les matchs sans cotes
                    for m in all_matches:
                        key = (m.home_team, m.away_team)
                        if key not in seen_keys:
                            carousel_matches.append({
                                "match": f"{m.away_team} @ {m.home_team}",
                                "away_team": m.away_team,
                                "home_team": m.home_team,
                                "date": m.date,
                                "time": m.time,
                                "odds": None,
                                "recommendation": "—",
                                "event_url": m.event_url,
                            })

                    data["carousel_matches"] = carousel_matches
            except Exception:
                pass

            return jsonify(data)
    return api_analyze()


@app.route('/api/live_picks')
def api_live_picks():
    """Retourne uniquement les paris Excellent (pour notifications)."""
    with _lock:
        if _cache["status"] == "ready" and _cache["data"]:
            picks = [p for p in _cache["data"].get("opportunities", [])
                     if "Excellent" in p.get("recommendation", "")]
            return jsonify({"picks": picks, "count": len(picks)})
    return jsonify({"picks": [], "count": 0})


@app.route('/api/kelly')
def api_kelly():
    """Calcule les mises Kelly pour une bankroll donnée."""
    bankroll   = float(request.args.get('bankroll', DEFAULT_MAX_NIGHTLY))
    kelly_frac = float(request.args.get('fraction', DEFAULT_KELLY_FRAC))

    with _lock:
        if not (_cache["status"] == "ready" and _cache["data"]):
            return jsonify({"error": "Pas de données disponibles — lancez /api/analyze d'abord"}), 400

        opps = _cache["data"].get("opportunities", [])

    from kelly import kelly_bet, edge_percent

    bets = []
    for opp in opps:
        fp   = opp.get("fair_prob", 0.5)
        odds = opp.get("odds", 2.0)
        bet  = kelly_bet(fp, odds, bankroll, kelly_frac)
        if bet > 0:
            bets.append({
                "match":      opp.get("match", ""),
                "selection":  opp.get("selection_label", ""),
                "odds":       odds,
                "fair_prob":  fp,
                "bet_amount": bet,
                "potential":  round(bet * odds, 2),
                "edge_pct":   edge_percent(fp, odds),
                "recommendation": opp.get("recommendation", ""),
            })

    bets.sort(key=lambda x: x["bet_amount"], reverse=True)
    total_wagered = sum(b["bet_amount"] for b in bets)

    return jsonify({
        "bankroll":      bankroll,
        "kelly_fraction": kelly_frac,
        "total_wagered": round(total_wagered, 2),
        "bets":          bets,
    })


@app.route('/api/history')
def api_history():
    """Retourne l'historique des prédictions."""
    n = int(request.args.get('n', 50))
    try:
        from predictions import get_history, get_stats
        history = get_history(n=n, sport="baseball")
        stats   = get_stats(sport="baseball")
        return jsonify({"history": history, "stats": stats})
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/save-snapshot', methods=['POST'])
def api_save_snapshot():
    """Sauvegarde les paris affichés dans snapshot.json et snapshots/YYYY-MM-DD.json."""
    body          = request.json or {}
    picks         = body.get("picks", [])
    all_opps      = body.get("all_opps", [])
    combos        = body.get("combos", [])
    low_value_night = body.get("low_value_night", False)
    if not picks and not all_opps:
        return jsonify({"error": "Aucun pari fourni"}), 400

    def _clean(p, is_bet: bool):
        # Conserver les données météo pour analyse d'impact
        w = p.get("weather") or {}
        weather_snap = {
            "run_adjustment": w.get("run_adjustment", 0.0),
            "temp_c":         w.get("temp_c"),
            "wind_kmh":       w.get("wind_kmh"),
            "wind_component": w.get("wind_component"),
            "is_dome":        w.get("is_dome", False),
        } if w else {}
        return {
            "key":            p.get("key", ""),
            "match":          p.get("match", ""),
            "home_team":      p.get("home_team", ""),
            "away_team":      p.get("away_team", ""),
            "selection":      p.get("selection", ""),
            "bet_type":       p.get("bet_type", ""),
            "odds":           p.get("odds"),
            "fair_prob":      p.get("fair_prob"),
            "mise":           p.get("mise") if is_bet else 0,
            "recommendation": p.get("recommendation", ""),
            "factor_scores":  p.get("factor_scores", {}),
            "weather":        weather_snap,
            "is_bet":         is_bet,
            "away_logo":      p.get("away_logo", ""),
            "home_logo":      p.get("home_logo", ""),
        }

    today    = _today_mtl()

    # Dédupliquer : all_opps contient déjà les picks, éviter les doublons
    bet_keys = {p.get("key", "") for p in picks}
    extra_opps = [p for p in all_opps if p.get("key", "") not in bet_keys]

    snapshot = {
        "saved_at":        _now_mtl().isoformat(),
        "date":            today,
        "time":            _now_mtl().strftime("%H:%M"),
        "low_value_night": low_value_night,
        "picks":           [_clean(p, True)  for p in picks] +
                           [_clean(p, False) for p in extra_opps],
        "combos":          combos,
    }

    with open(_SNAPSHOT_PATH, "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    os.makedirs(_SNAPSHOTS_DIR, exist_ok=True)
    with open(os.path.join(_SNAPSHOTS_DIR, f"{today}.json"), "w", encoding="utf-8") as f:
        json.dump(snapshot, f, ensure_ascii=False, indent=2)

    print(f"  >> Snapshot MLB sauvegardé : {len(snapshot['picks'])} paris à {snapshot['time']}")
    return jsonify({"ok": True, "saved": len(snapshot["picks"]), "time": snapshot["time"]})


# ─── Cron auto-snapshot (request-driven, idempotent) ──────────────────────────

@app.route("/api/cron/auto-snapshot", methods=["GET", "POST"])
def api_cron_auto_snapshot():
    """Endpoint cron déclenché par UptimeRobot (~5 min). Idempotent.

    Logique :
    1. Lit la lockfile → skip si déjà fait aujourd'hui
    2. Récupère les opportunités du jour depuis le cache
    3. Calcule l'heure cible = premier match - 30 min
    4. Si now >= target ET pas fait → sauvegarde snapshot
    5. Écrit la lockfile (idempotence)

    Avantage vs thread Python : exécuté à chaque requête HTTP, donc tolère
    le sleep des plateformes (Fly.io auto_stop_machines).
    """
    today   = _today_mtl()
    now_mtl = _now_mtl()

    # 1. Lock : déjà fait aujourd'hui ?
    if os.path.exists(_AUTO_SNAPSHOT_LOCK):
        try:
            with open(_AUTO_SNAPSHOT_LOCK) as f:
                last_date = f.read().strip()
            if last_date == today:
                return jsonify({
                    "ok":      True,
                    "skipped": "already_done_today",
                    "last_date": last_date,
                    "now":     now_mtl.strftime("%Y-%m-%d %H:%M MTL"),
                })
        except Exception:
            pass

    # 2. Picks du jour depuis le cache en mémoire
    cached_data  = (_cache.get("data") or {})
    all_opps     = cached_data.get("opportunities") or []
    today_picks  = [p for p in all_opps if p.get("date") == today]

    if not today_picks:
        # Si le cache est vide, lancer une analyse en arrière-plan pour le peupler
        if _cache.get("status") not in ("running", "ready"):
            try:
                _start_analysis_thread(
                    bankroll=DEFAULT_BANKROLL,
                    kelly_frac=DEFAULT_KELLY_FRAC,
                    max_nightly=DEFAULT_MAX_NIGHTLY,
                    top_n=50,
                )
            except Exception:
                pass
        return jsonify({
            "ok":      True,
            "skipped": "no_picks_today",
            "cache_status": _cache.get("status"),
            "now":     now_mtl.strftime("%Y-%m-%d %H:%M MTL"),
        })

    # 3. Heure du premier match → calcul de la fenêtre cible (30 min avant)
    times = sorted({p.get("time") for p in today_picks if p.get("time")})
    if not times:
        return jsonify({"ok": True, "skipped": "no_match_times"})

    first_time = times[0]  # ex. "19:07"
    try:
        h, m = map(int, first_time.split(":"))
        target = now_mtl.replace(hour=h, minute=m, second=0, microsecond=0) - timedelta(minutes=30)
    except Exception as e:
        return jsonify({"ok": False, "error": f"format heure invalide: {first_time} — {e}"}), 400

    if now_mtl < target:
        return jsonify({
            "ok":      True,
            "skipped": "before_target_window",
            "now":     now_mtl.strftime("%H:%M"),
            "target":  target.strftime("%H:%M"),
            "first_match": first_time,
            "minutes_until": int((target - now_mtl).total_seconds() // 60),
        })

    # 4. Déclenchement : construire et sauvegarder le snapshot
    try:
        snapshot = {
            "saved_at":   now_mtl.isoformat(),
            "date":       today,
            "time":       now_mtl.strftime("%H:%M"),
            "auto":       True,
            "first_match": first_time,
            "picks": [
                {
                    "key":            p.get("key", ""),
                    "match":          p.get("match", ""),
                    "home_team":      p.get("home_team", ""),
                    "away_team":      p.get("away_team", ""),
                    "selection":      p.get("selection_label", p.get("selection", "")),
                    "bet_type":       p.get("bet_type", ""),
                    "odds":           p.get("odds"),
                    "fair_prob":      p.get("fair_prob"),
                    "value_score":    p.get("value_score"),
                    "mise":           p.get("mise"),
                    "recommendation": p.get("recommendation", ""),
                    "is_bet":         bool(p.get("mise")),
                    "factor_scores":  p.get("factor_scores", {}),
                    "weather":        p.get("weather", {}),
                }
                for p in today_picks
            ],
        }

        # Snapshot courant (snapshot.json)
        os.makedirs(_DATA_DIR, exist_ok=True)
        with open(_SNAPSHOT_PATH, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)

        # Snapshot historique (snapshots/YYYY-MM-DD.json)
        os.makedirs(_SNAPSHOTS_DIR, exist_ok=True)
        daily_path = os.path.join(_SNAPSHOTS_DIR, f"{today}.json")
        with open(daily_path, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, ensure_ascii=False, indent=2)

        print(f"  [auto-snapshot] {len(today_picks)} picks sauvegardés à {snapshot['time']} MTL (1er match: {first_time})")

        # 5. Lock pour idempotence
        try:
            with open(_AUTO_SNAPSHOT_LOCK, "w") as f:
                f.write(today)
        except Exception as lock_err:
            print(f"  [auto-snapshot] Lock write erreur: {lock_err}")

        return jsonify({
            "ok":             True,
            "snapshot_saved": True,
            "picks_count":    len(today_picks),
            "first_match":    first_time,
            "snapshot_time":  snapshot["time"],
        })

    except Exception as e:
        print(f"  [auto-snapshot] ERREUR: {e}")
        return jsonify({"ok": False, "error": str(e)}), 500


def _resolve_combos(combos_raw: list, mlb_results: dict) -> list:
    """Résout les outcomes pour une liste de combos MLB."""
    out = []
    for combo in combos_raw:
        raw_picks = combo.get("picks", [])
        if len(raw_picks) < 2:
            continue
        # Enrichir les picks avec home/away si manquant
        for p in raw_picks:
            p.setdefault("home_team", combo.get("home_team", ""))
            p.setdefault("away_team", combo.get("away_team", ""))
        resolved_picks = []
        for p in raw_picks:
            outcome = _resolve_pick(p, mlb_results)
            rp = dict(p)
            rp["outcome"] = outcome
            resolved_picks.append(rp)
        outcomes = [rp["outcome"] for rp in resolved_picks]
        if all(o == "win" for o in outcomes):
            combo_outcome = "win"
        elif any(o == "loss" for o in outcomes):
            combo_outcome = "loss"
        elif all(o == "pending" for o in outcomes):
            combo_outcome = "pending"
        else:
            combo_outcome = "partial"
        out.append({
            "match":         combo.get("match", ""),
            "short_match":   combo.get("short_match", ""),
            "combo_type":    combo.get("combo_type", "Combo"),
            "label":         combo.get("label", ""),
            "combined_odds": combo.get("combined_odds", 1.0),
            "avg_score":     combo.get("avg_score", 50),
            "combo_outcome": combo_outcome,
            "picks": [
                {"bet_type": rp.get("bet_type"), "selection": rp.get("selection"),
                 "odds": rp.get("odds"), "outcome": rp.get("outcome")}
                for rp in resolved_picks
            ],
        })
    return out


@app.route('/api/yesterday')
def api_yesterday():
    """Résultats des paris d'hier depuis le snapshot + résultats MLB via statsapi."""
    from datetime import date, timedelta
    today = _today_mtl()

    # Chercher le snapshot le plus récent antérieur à aujourd'hui
    snap = None
    if os.path.isdir(_SNAPSHOTS_DIR):
        past = sorted(
            [f for f in os.listdir(_SNAPSHOTS_DIR) if f.endswith(".json") and f[:10] < today],
            reverse=True,
        )
        if past:
            with open(os.path.join(_SNAPSHOTS_DIR, past[0]), encoding="utf-8") as f:
                snap = json.load(f)

    if snap is None:
        if not os.path.exists(_SNAPSHOT_PATH):
            return jsonify({"error": "Aucun snapshot disponible — sauvegarde d'abord le tableau avec 💾"}), 404
        with open(_SNAPSHOT_PATH, encoding="utf-8") as f:
            snap = json.load(f)
        if snap.get("date", "") == today:
            return jsonify({"error": "snapshot_today"}), 404

    snap_date   = snap.get("date", "")
    mlb_results = _fetch_mlb_results(snap_date)

    enriched = []
    wins = losses = pushes = pending = 0
    roi = 0.0

    for pick in snap.get("picks", []):
        outcome  = _resolve_pick(pick, mlb_results)
        is_bet   = pick.get("is_bet", bool(pick.get("mise", 0)))
        mise     = pick.get("mise") or 0.0
        profit   = 0.0
        if outcome == "win":
            if is_bet:
                profit = mise * (pick.get("odds", 2.0) - 1.0)
                wins += 1
        elif outcome == "loss":
            if is_bet:
                profit = -mise
                losses += 1
        elif outcome == "push":
            if is_bet:
                pushes += 1
        else:
            if is_bet:
                pending += 1
        roi += profit

        # Générer les logos s'ils ne sont pas dans le snapshot (rétrocompatibilité)
        away_logo = pick.get("away_logo") or _get_mlb_team_logo(pick.get("away_team", ""))
        home_logo = pick.get("home_logo") or _get_mlb_team_logo(pick.get("home_team", ""))

        enriched.append({
            **pick,
            "outcome": outcome,
            "profit": round(profit, 2),
            "is_bet": is_bet,
            "away_logo": away_logo,
            "home_logo": home_logo,
        })

    combo_results = _resolve_combos(snap.get("combos", []), mlb_results)

    return jsonify({
        "date":          snap_date,
        "snapshot_time": snap.get("time"),
        "picks":         enriched,
        "combos":        combo_results,
        "wins":          wins,
        "losses":        losses,
        "pushes":        pushes,
        "pending":       pending,
        "total":         len(enriched),
        "roi":           round(roi, 2),
    })


_mlb_results_cache: dict = {}  # date → mlb_results dict


def _reformat_date_statsapi(iso_date: str) -> str:
    """Convertit YYYY-MM-DD en MM/DD/YYYY pour statsapi."""
    try:
        from datetime import datetime
        dt = datetime.strptime(iso_date, "%Y-%m-%d")
        return dt.strftime("%m/%d/%Y")
    except Exception:
        return iso_date


def _normalize_team_name(name: str) -> str:
    """Normalise un nom d'équipe en enlevant les parenthèses et accents.
    Toronto (Blue Jays) → toronto blue jays
    """
    import re
    # Enlever les parenthèses et leur contenu
    name = re.sub(r'\s*\([^)]*\)', '', name)
    # Minuscules
    return name.lower().strip()


def _fetch_mlb_results(date_str: str) -> dict:
    """Résultats MLB pour une date (cache en mémoire)."""
    if date_str in _mlb_results_cache:
        return _mlb_results_cache[date_str]
    import statsapi
    results = {}
    try:
        from mlb_stats import _find_team_id
        # Convertir YYYY-MM-DD en MM/DD/YYYY pour statsapi
        formatted_date = _reformat_date_statsapi(date_str)
        for g in statsapi.schedule(date=formatted_date):
            if g.get("status") not in ("Final", "Game Over", "Completed Early"):
                continue
            home    = _normalize_team_name(g.get("home_name", ""))
            away    = _normalize_team_name(g.get("away_name", ""))
            home_id = g.get("home_id")
            away_id = g.get("away_id")
            hs      = g.get("home_score", 0) or 0
            as_     = g.get("away_score", 0) or 0
            entry   = {"home_score": hs, "away_score": as_,
                       "total": hs + as_, "winner": "home" if hs > as_ else "away"}
            # Index par statsapi name (normalized)
            results[home] = entry
            results[away] = entry
            # Index par team_id (robuste contre les noms MO-J)
            if home_id:
                results[str(home_id)] = entry
            if away_id:
                results[str(away_id)] = entry
    except Exception:
        pass
    _mlb_results_cache[date_str] = results
    return results


def _resolve_pick(pick: dict, mlb_results: dict) -> str:
    """Retourne 'win', 'loss', 'push' ou 'pending'."""
    import re
    home_raw = (pick.get("home_team") or "")
    away_raw = (pick.get("away_team") or "")
    home     = home_raw.lower()
    away     = away_raw.lower()

    # 1. Lookup par team_id (robuste contre noms MO-J avec parenthèses)
    game = None
    try:
        from mlb_stats import _find_team_id
        home_id = _find_team_id(home_raw)
        away_id = _find_team_id(away_raw)
        if home_id:
            game = mlb_results.get(str(home_id))
        if not game and away_id:
            game = mlb_results.get(str(away_id))
    except Exception:
        pass
    # 2. Fallback par nom normalisé (enlever parenthèses)
    if not game:
        home_normalized = _normalize_team_name(home_raw)
        away_normalized = _normalize_team_name(away_raw)
        game = mlb_results.get(home_normalized) or mlb_results.get(away_normalized)
    if not game:
        return "pending"

    sel   = (pick.get("selection") or "").lower()
    btype = (pick.get("bet_type") or "").lower()
    hs, as_, total = game["home_score"], game["away_score"], game["total"]

    if any(k in btype for k in ("gagnant", "moneyline", "victoire", "winner", "2 issues")):
        # La sélection contient le nom de l'équipe choisie (ex: "Milwaukee (Brewers)")
        # Vérifier si c'est l'équipe locale ou visiteur
        def _team_matches(team_name: str, sel_text: str) -> bool:
            t = team_name.lower()
            # Extraire les mots significatifs (ignorer parenthèses et ponctuation)
            words = re.findall(r'[a-záàâäéèêëíìîïóòôöúùûü]+', t)
            return any(w in sel_text for w in words if len(w) > 2)

        if _team_matches(home_raw, sel):
            return "win" if game["winner"] == "home" else "loss"
        if _team_matches(away_raw, sel):
            return "win" if game["winner"] == "away" else "loss"

    if any(k in btype for k in ("total", "plus/moins", "over", "under")):
        # Détecter si c'est un total d'équipe (nom d'équipe dans le bet_type)
        # ex: "Cincinnati (Reds) Total de points plus/moins 4.0" → score de Cincinnati
        def _team_matches_bt(team_name: str, bt_text: str) -> bool:
            words = re.findall(r'[a-záàâäéèêëíìîïóòôöúùûü]+', team_name.lower())
            return any(w in bt_text for w in words if len(w) > 2)

        if _team_matches_bt(home_raw, btype):
            score = hs    # total de l'équipe locale
        elif _team_matches_bt(away_raw, btype):
            score = as_   # total de l'équipe visiteur
        else:
            score = total  # total du match complet (O/U standard)

        # La ligne est dans btype ("plus/moins 3.5") ou dans sel ("Moins de 8.5")
        m = re.search(r'(\d+\.?\d*)', sel) or re.search(r'(\d+\.?\d*)', btype)
        if m:
            line = float(m.group(1))
            if "plus" in sel or "over" in sel:
                return "win" if score > line else ("push" if score == line else "loss")
            if "moins" in sel or "under" in sel:
                return "win" if score < line else ("push" if score == line else "loss")
    return "pending"


@app.route('/api/stats')
def api_stats():
    """Agrège tous les snapshots passés pour produire les statistiques de performance."""
    from datetime import date
    today = _today_mtl()

    if not os.path.isdir(_SNAPSHOTS_DIR):
        return jsonify({"error": "Aucun snapshot disponible"}), 404

    past_files = sorted(
        [f for f in os.listdir(_SNAPSHOTS_DIR) if f.endswith(".json") and f[:10] <= today]
    )
    if not past_files:
        return jsonify({"error": "Aucun snapshot disponible"}), 404

    # all_picks = toutes les prédictions (bet + non-bet) pour la calibration
    # bet_picks = seulement les paris misés pour les stats financières
    all_picks       = []
    all_combos      = []
    low_value_dates = set()   # dates où low_value_night était True
    for fname in past_files:
        with open(os.path.join(_SNAPSHOTS_DIR, fname), encoding="utf-8") as f:
            snap = json.load(f)
        snap_date   = snap.get("date", fname[:10])
        # low_value_night explicite OU dérivé : pas de pick "Excellent" = soirée faible
        snap_picks  = snap.get("picks", [])
        has_excellent = any("Excellent" in (p.get("recommendation") or "") for p in snap_picks)
        is_low = snap.get("low_value_night") or (not has_excellent)
        if is_low:
            low_value_dates.add(snap_date)
        mlb_results = _fetch_mlb_results(snap_date)
        for combo in snap.get("combos", []):
            for resolved_combo in _resolve_combos([combo], mlb_results):
                all_combos.append({**resolved_combo, "date": snap_date})
        for pick in snap.get("picks", []):
            outcome  = _resolve_pick(pick, mlb_results)
            is_bet   = pick.get("is_bet", bool(pick.get("mise", 0)))
            mise     = (pick.get("mise") or 0.0) if is_bet else 0.0
            odds     = pick.get("odds") or 2.0
            profit   = (mise * (odds - 1.0) if outcome == "win"
                        else -mise if outcome == "loss" else 0.0) if is_bet else 0.0
            all_picks.append({
                **pick,
                "date":    snap_date,
                "outcome": outcome,
                "profit":  round(profit, 2),
                "mise":    mise,
                "is_bet":  is_bet,
            })

    # Séparation : financier vs calibration
    bet_picks = [p for p in all_picks if p["is_bet"]]
    resolved     = [p for p in all_picks   if p["outcome"] != "pending"]  # calibration
    bet_resolved = [p for p in bet_picks   if p["outcome"] != "pending"]  # financier

    def _agg(picks):
        w = sum(1 for p in picks if p["outcome"] == "win")
        l = sum(1 for p in picks if p["outcome"] == "loss")
        pu = sum(1 for p in picks if p["outcome"] == "push")
        roi = sum(p["profit"] for p in picks)
        mise_tot = sum(p["mise"] for p in picks)
        n = w + l + pu
        return {"total": n, "wins": w, "losses": l, "pushes": pu,
                "win_rate": round(w / (w + l) * 100, 1) if (w + l) > 0 else None,
                "roi": round(roi, 2),
                "roi_pct": round(roi / mise_tot * 100, 1) if mise_tot > 0 else None}

    # ── Bilan global (paris misés seulement) ─────────────────────────
    global_stats = _agg(bet_resolved)
    global_stats["pending"]    = sum(1 for p in bet_picks if p["outcome"] == "pending")
    global_stats["days"]       = len(past_files)
    global_stats["n_preds"]    = len(resolved)   # total prédictions pour calibration

    # ── Par type de pari (paris misés) ───────────────────────────────
    by_type: dict = {}
    for p in bet_resolved:
        bt = (p.get("bet_type") or "Autre")
        bt = "Moneyline / Gagnant" if any(k in bt.lower() for k in ("gagnant","moneyline","victoire","2 issues")) \
             else "Total (O/U)" if any(k in bt.lower() for k in ("total","plus/moins")) \
             else bt
        by_type.setdefault(bt, []).append(p)
    by_type_agg = {k: _agg(v) for k, v in by_type.items()}

    # ── Par fourchette de cotes (paris misés) ────────────────────────
    ranges = [("1.50 – 1.79", 1.50, 1.79), ("1.80 – 2.09", 1.80, 2.09),
              ("2.10 – 2.49", 2.10, 2.49), ("2.50 – 3.00", 2.50, 3.00), ("3.00+", 3.00, 99)]
    by_odds = {}
    for label, lo, hi in ranges:
        picks = [p for p in bet_resolved if lo <= (p.get("odds") or 0) < hi
                 or (label == "3.00+" and (p.get("odds") or 0) >= 3.00)]
        if picks:
            by_odds[label] = _agg(picks)

    # ── Par recommandation (paris misés) ─────────────────────────────
    by_rec: dict = {}
    for p in bet_resolved:
        rec = p.get("recommendation") or "Autre"
        rec = "Excellent ★★★" if "excellent" in rec.lower() else \
              "Bon ★★" if rec.lower().startswith("bon") else \
              "À éviter" if "viter" in rec.lower() else "Neutre"
        by_rec.setdefault(rec, []).append(p)
    by_rec_agg = {k: _agg(v) for k, v in by_rec.items()}

    # ── Calibration prob (TOUTES les prédictions — bet + non-bet) ────
    buckets = [(i/10, (i+1)/10) for i in range(4, 9)]
    calibration = []
    for lo, hi in buckets:
        picks = [p for p in resolved if lo <= (p.get("fair_prob") or 0) < hi]
        if picks:
            w = sum(1 for p in picks if p["outcome"] == "win")
            calibration.append({
                "label":     f"{int(lo*100)}–{int(hi*100)}%",
                "predicted": round((lo + hi) / 2 * 100, 1),
                "actual":    round(w / len(picks) * 100, 1),
                "count":     len(picks),
            })

    # ── Par équipe (paris misés) ──────────────────────────────────────
    by_team: dict = {}
    for p in bet_resolved:
        sel = (p.get("selection") or "").lower()
        home = (p.get("home_team") or "").lower()
        away = (p.get("away_team") or "").lower()
        team = p.get("home_team") if home in sel else \
               p.get("away_team") if away in sel else \
               p.get("home_team") or "Inconnu"
        by_team.setdefault(team, []).append(p)
    by_team_agg = sorted(
        [{"team": k, **_agg(v)} for k, v in by_team.items() if _agg(v)["total"] >= 2],
        key=lambda x: x["roi"], reverse=True
    )[:15]

    # ── Par jour (graphique — paris misés seulement) ──────────────────
    by_date_raw: dict = {}
    for p in bet_picks:
        by_date_raw.setdefault(p["date"], []).append(p)
    by_date = []
    cumul = 0.0
    for d in sorted(by_date_raw.keys()):
        day_picks = by_date_raw[d]
        day_resolved = [p for p in day_picks if p["outcome"] != "pending"]
        roi = sum(p["profit"] for p in day_resolved)
        cumul += roi
        wins = sum(1 for p in day_resolved if p["outcome"] == "win")
        losses = sum(1 for p in day_resolved if p["outcome"] == "loss")
        win_rate = round(wins / (wins + losses) * 100, 1) if (wins + losses) > 0 else None
        by_date.append({
            "date":       d,
            "roi":        round(roi, 2),
            "cumul":      round(cumul, 2),
            "wins":       wins,
            "losses":     losses,
            "win_rate":   win_rate,
            "n":          len(day_resolved),
            "low_value":  d in low_value_dates,
        })

    # ── Stats Combos ─────────────────────────────────────────────────
    combos_resolved = [c for c in all_combos if c["combo_outcome"] in ("win", "loss")]
    combo_wins   = sum(1 for c in combos_resolved if c["combo_outcome"] == "win")
    combo_losses = len(combos_resolved) - combo_wins
    combo_stats  = {
        "total":    len(all_combos),
        "resolved": len(combos_resolved),
        "wins":     combo_wins,
        "losses":   combo_losses,
        "win_rate": round(combo_wins / len(combos_resolved) * 100, 1) if combos_resolved else None,
        "pending":  sum(1 for c in all_combos if c["combo_outcome"] == "pending"),
    }

    # ── Stats Météo ───────────────────────────────────────────────────
    # Analyse : l'ajustement météo prédit corrèle-t-il avec les résultats réels?
    # On se concentre sur les paris Total O/U (les seuls affectés par la météo)
    def _is_total(p):
        return any(k in (p.get("bet_type") or "").lower()
                   for k in ("total", "plus/moins"))

    def _is_over(p):
        sel = (p.get("selection") or "").lower()
        return "plus" in sel or "over" in sel

    # Picks avec données météo sauvegardées
    weather_picks = [p for p in all_picks
                     if p.get("weather") and not p["weather"].get("is_dome")
                     and p["outcome"] in ("win", "loss")
                     and _is_total(p)]

    weather_stats = {}
    if weather_picks:
        # Bracketing par ajustement météo prédit
        brackets = [
            ("Très favorable Over",  0.5,  99,  True),   # adj >= +0.5, pari Over
            ("Favorable Over",       0.2,  0.5, True),
            ("Neutre",              -0.2,  0.2, None),    # tous paris
            ("Favorable Under",     -99,  -0.2, False),   # adj < -0.2, pari Under
        ]
        weather_by_bracket = {}
        for label, lo, hi, expected_over in brackets:
            matches = []
            for p in weather_picks:
                adj = p["weather"].get("run_adjustment", 0.0) or 0.0
                if lo <= adj < hi or (hi == 99 and adj >= lo):
                    # Pour "Neutre" : tous les paris
                    # Pour autres : vérifier si le pari est dans la bonne direction
                    if expected_over is None:
                        matches.append(p)
                    elif expected_over and _is_over(p):
                        matches.append(p)
                    elif not expected_over and not _is_over(p):
                        matches.append(p)
            if matches:
                w = sum(1 for p in matches if p["outcome"] == "win")
                l = len(matches) - w
                wr = round(w / len(matches) * 100, 1)
                hi_str = '+∞' if hi == 99 else f'{hi:+.1f}'
                weather_by_bracket[label] = {
                    "n": len(matches), "wins": w, "losses": l, "win_rate": wr,
                    "adj_range": f"{lo:+.1f} à {hi_str}",
                }

        # Impact global : corrélation adj vs résultat
        # Pour les Over : est-ce que les matchs avec adj>0 gagnent plus souvent?
        over_picks  = [p for p in weather_picks if _is_over(p)]
        under_picks = [p for p in weather_picks if not _is_over(p)]

        def _corr(picks):
            if len(picks) < 3:
                return None
            # Picks avec adj positif (météo dans la bonne direction du pari)
            aligned  = [p for p in picks
                        if (_is_over(p) and (p["weather"].get("run_adjustment") or 0) > 0.1)
                        or (not _is_over(p) and (p["weather"].get("run_adjustment") or 0) < -0.1)]
            contrary = [p for p in picks
                        if (_is_over(p) and (p["weather"].get("run_adjustment") or 0) < -0.1)
                        or (not _is_over(p) and (p["weather"].get("run_adjustment") or 0) > 0.1)]
            if not aligned or not contrary:
                return None
            wr_aligned  = sum(1 for p in aligned  if p["outcome"] == "win") / len(aligned)
            wr_contrary = sum(1 for p in contrary if p["outcome"] == "win") / len(contrary)
            return {
                "aligned_n":  len(aligned),  "aligned_wr":  round(wr_aligned  * 100, 1),
                "contrary_n": len(contrary), "contrary_wr": round(wr_contrary * 100, 1),
                "delta":      round((wr_aligned - wr_contrary) * 100, 1),
            }

        weather_stats = {
            "total_with_data": len(weather_picks),
            "by_bracket":      weather_by_bracket,
            "over_correlation":  _corr(over_picks),
            "under_correlation": _corr(under_picks),
            "note": ("Données insuffisantes — la météo sera analysée après "
                     "20+ paris Total avec données météo sauvegardées."
                     if len(weather_picks) < 10 else ""),
        }
    else:
        weather_stats = {
            "total_with_data": 0,
            "note": "Aucune donnée météo dans les snapshots. Sauvegarde un snapshot aujourd'hui pour commencer le tracking.",
        }

    # ── Par mode d'analyse (standard vs conservateur) ──────────────────
    # Pour chaque pick : chercher "analysis_mode" dans les snapshots
    by_mode: dict = {}
    by_mode_daily: dict = {"standard": [], "conservative": []}  # Pour graphique cumulatif

    for mode_name in ("standard", "conservative"):
        picks_in_mode = [p for p in bet_resolved
                        if p.get("analysis_mode") == mode_name]
        if picks_in_mode:
            by_mode[mode_name] = _agg(picks_in_mode)

        # Cumul journalier par mode
        picks_all_in_mode = [p for p in all_picks
                            if p.get("analysis_mode") == mode_name]
        daily_cumul = {"standard": {}, "conservative": {}}
        daily_wins = {"standard": {}, "conservative": {}}
        for p in picks_all_in_mode:
            d = p.get("date", "2000-01-01")
            if d not in daily_cumul[mode_name]:
                daily_cumul[mode_name][d] = 0
                daily_wins[mode_name][d] = 0
            if p["outcome"] != "pending":
                daily_cumul[mode_name][d] += 1
                if p["outcome"] == "win":
                    daily_wins[mode_name][d] += 1

        # Convertir en cumuls cumulatifs
        cumul_wins = 0
        cumul_total = 0
        for date_str in sorted(daily_cumul[mode_name].keys()):
            cumul_wins += daily_wins[mode_name].get(date_str, 0)
            cumul_total += daily_cumul[mode_name].get(date_str, 0)
            wr = round(cumul_wins / cumul_total * 100, 1) if cumul_total > 0 else 0
            by_mode_daily[mode_name].append({
                "date": date_str,
                "cumul_wins": cumul_wins,
                "cumul_total": cumul_total,
                "cumul_wr": wr,
            })

    return jsonify({
        "global":        global_stats,
        "by_type":       by_type_agg,
        "by_odds":       by_odds,
        "by_rec":        by_rec_agg,
        "calibration":   calibration,
        "by_team":       by_team_agg,
        "by_date":       by_date,
        "days":          len(past_files),
        "combo_stats":   combo_stats,
        "weather_stats": weather_stats,
        "by_mode":       by_mode,
        "by_mode_daily": by_mode_daily,
    })


@app.route('/api/analyze_v2')
def api_analyze_v2():
    """Système V2 — signaux non capturés par le marché (bullpen repos, biais public, splits)."""
    top_n = int(request.args.get('top', 10))

    try:
        matches = _scrape_cached()
        if not matches:
            return jsonify({"error": "Aucun match trouvé"}), 404

        from analyzer_v2 import AnalyzerV2
        engine = AnalyzerV2()
        picks  = engine.analyze(matches, top_n=top_n)

        # Enregistrer pour tracking historique
        try:
            from predictions import record_opportunity
            for p in picks:
                record_opportunity({
                    "date":            p.date,
                    "time":            p.time,
                    "home_team":       p.home_team,
                    "away_team":       p.away_team,
                    "bet_type":        "Gagnant à 2 issues",
                    "selection":       p.bet_team,
                    "odds":            p.odds,
                    "fair_prob":       p.implied_prob,
                    "math_prob":       p.implied_prob,
                    "value_score":     round(p.composite_score * 100, 1),
                    "recommendation":  p.confidence,
                    "sport":           "baseball",
                    "kelly_fraction":  0.0,
                    "analysis_mode":   "v2",
                    "system_version":  "v2",
                })
        except Exception:
            pass

        out = []
        for p in picks:
            out.append({
                "home_team":       p.home_team,
                "away_team":       p.away_team,
                "date":            p.date,
                "time":            p.time,
                "bet_team":        p.bet_team,
                "bet_side":        p.bet_side,
                "odds":            round(p.odds, 2),
                "implied_prob":    round(p.implied_prob * 100, 1),
                "composite_score": round(p.composite_score * 100, 1),
                "confidence":      p.confidence,
                "rationale":       p.rationale,
                "home_pitcher":    p.home_pitcher,
                "away_pitcher":    p.away_pitcher,
                "league":          p.league,
                "signals": [
                    {
                        "name":        s.name,
                        "score":       round(s.score, 3),
                        "weight":      s.weight,
                        "weighted":    round(s.score * s.weight, 4),
                        "description": s.description,
                    }
                    for s in p.signals
                ],
            })

        return jsonify({
            "picks":     out,
            "total":     len(out),
            "timestamp": _now_mtl().strftime("%H:%M:%S"),
            "date":      _today_mtl(),
            "system":    "v2",
        })

    except Exception as e:
        import traceback
        return jsonify({"error": str(e), "trace": traceback.format_exc()}), 500


@app.route('/api/stats_v2')
def api_stats_v2():
    """Statistiques historiques du système V2 — lit predictions.json directement."""
    from datetime import date
    from pathlib import Path
    today  = _today_mtl()
    budget = float(request.args.get('budget', 10))

    predictions_file = Path(__file__).parent / "predictions.json"
    if not predictions_file.exists():
        return jsonify({
            "total": 0, "pending": 0, "wins": 0, "losses": 0,
            "win_rate": None, "roi": 0.0, "roi_pct": None,
            "by_date": [], "system": "v2",
        })

    try:
        with open(predictions_file, encoding="utf-8") as f:
            records = json.load(f)
    except Exception:
        return jsonify({"error": "Impossible de lire predictions.json"}), 500

    # Filtrer uniquement les picks V2 passés (pas aujourd'hui)
    v2_picks = [
        r for r in records
        if (r.get("system_version") == "v2" or r.get("analysis_mode") == "v2")
        and r.get("date", "9999") < today
    ]

    all_picks = []
    # Grouper par date pour résoudre les outcomes
    dates_needed = {p.get("date") for p in v2_picks if p.get("date")}
    mlb_cache = {d: _fetch_mlb_results(d) for d in dates_needed}

    for pick in v2_picks:
        d = pick.get("date", "")
        mlb_results = mlb_cache.get(d, {})
        # Mapper les champs de predictions.json vers le format attendu par _resolve_pick
        pick_mapped = {
            "home_team":  pick.get("home_team", ""),
            "away_team":  pick.get("away_team", ""),
            "bet_type":   pick.get("bet_type", ""),
            "selection":  pick.get("selection", ""),
            "odds":       pick.get("odds", 2.0),
        }
        outcome = _resolve_pick(pick_mapped, mlb_results)
        if outcome == "pending" and pick.get("outcome"):
            outcome = pick["outcome"]
        all_picks.append({
            **pick,
            "date":    d,
            "outcome": outcome,
        })

    # ── Calcul des mises proportionnelles par soir ───────────────────────────
    # Regrouper par date → calculer les mises proportionnelles au score composite
    MIN_BET = 0.50
    by_date_picks: dict = {}
    for p in all_picks:
        by_date_picks.setdefault(p["date"], []).append(p)

    # Attacher la mise à chaque pick
    for d, picks_d in by_date_picks.items():
        total_score = sum(float(p.get("value_score") or 0) for p in picks_d)
        for p in picks_d:
            score = float(p.get("value_score") or 0)
            raw   = (score / total_score * budget) if total_score > 0 else budget / len(picks_d)
            p["mise"] = max(MIN_BET, round(raw * 2) / 2)
        # Ajustement pour coller au budget
        total_mise = sum(p["mise"] for p in picks_d)
        diff = round((budget - total_mise) * 2) / 2
        if diff != 0 and picks_d:
            max_pick = max(picks_d, key=lambda p: p["mise"])
            max_pick["mise"] = max(MIN_BET, round((max_pick["mise"] + diff) * 2) / 2)

    resolved = [p for p in all_picks if p["outcome"] != "pending"]
    wins    = sum(1 for p in resolved if p["outcome"] == "win")
    losses  = sum(1 for p in resolved if p["outcome"] == "loss")

    by_date: dict = {}
    for p in resolved:
        d    = p["date"]
        mise = p.get("mise", 1.0)
        odds = p.get("odds") or 2.0
        profit = round(mise * (odds - 1.0), 2) if p["outcome"] == "win" \
            else round(-mise, 2) if p["outcome"] == "loss" else 0.0
        by_date.setdefault(d, {"wins": 0, "losses": 0, "roi": 0.0, "mise_tot": 0.0})
        by_date[d]["wins"]     += 1 if p["outcome"] == "win" else 0
        by_date[d]["losses"]   += 1 if p["outcome"] == "loss" else 0
        by_date[d]["roi"]       = round(by_date[d]["roi"] + profit, 2)
        by_date[d]["mise_tot"]  = round(by_date[d]["mise_tot"] + mise, 2)

    roi_total  = sum(dd["roi"] for dd in by_date.values())
    mise_total = sum(dd["mise_tot"] for dd in by_date.values())

    # Cumul journalier
    cumul = 0.0
    daily = []
    for d in sorted(by_date.keys()):
        dd = by_date[d]
        cumul = round(cumul + dd["roi"], 2)
        total_d = dd["wins"] + dd["losses"]
        daily.append({
            "date":      d,
            "wins":      dd["wins"],
            "losses":    dd["losses"],
            "roi":       dd["roi"],
            "mise_tot":  dd["mise_tot"],
            "cumul_roi": cumul,
            "win_rate":  round(dd["wins"] / total_d * 100, 1) if total_d else None,
        })

    total = wins + losses
    return jsonify({
        "total":      total,
        "pending":    len(all_picks) - len(resolved),
        "wins":       wins,
        "losses":     losses,
        "win_rate":   round(wins / total * 100, 1) if total else None,
        "roi":        round(roi_total, 2),
        "roi_pct":    round(roi_total / mise_total * 100, 1) if mise_total else None,
        "mise_total": round(mise_total, 2),
        "budget":     budget,
        "by_date":    daily,
        "system":     "v2",
    })


@app.route('/api/mlb-combo-today')
def api_mlb_combo_today():
    """Retourne les combos du snapshot sauvegardé aujourd'hui (figés)."""
    today = _today_mtl()
    daily_path = os.path.join(_SNAPSHOTS_DIR, f"{today}.json")
    if not os.path.exists(daily_path):
        return jsonify({"combos": [], "saved_at": None})
    try:
        with open(daily_path, encoding="utf-8") as f:
            snap = json.load(f)
        return jsonify({
            "combos":   snap.get("combos", []),
            "saved_at": snap.get("time", ""),
        })
    except Exception:
        return jsonify({"combos": [], "saved_at": None})


@app.route('/api/mlb-combo-history')
def api_mlb_combo_history():
    """Historique des Combos MLB résolus depuis les snapshots passés."""
    today = _today_mtl()

    if not os.path.isdir(_SNAPSHOTS_DIR):
        return jsonify({"combos": [], "summary": {}})

    snap_files = sorted([
        f for f in os.listdir(_SNAPSHOTS_DIR)
        if f.endswith(".json") and f[:10] < today
    ])

    combos_history = []
    for fname in snap_files:
        d = fname[:10]
        try:
            with open(os.path.join(_SNAPSHOTS_DIR, fname), encoding="utf-8") as fh:
                snap = json.load(fh)
        except Exception:
            continue
        combos_raw = snap.get("combos", [])
        if not combos_raw:
            continue
        mlb_results = _fetch_mlb_results(d)
        for resolved in _resolve_combos(combos_raw, mlb_results):
            combos_history.append({**resolved, "date": d})

    combos_history.reverse()  # plus récent en premier
    resolved_combos = [c for c in combos_history if c["combo_outcome"] in ("win", "loss")]
    wins   = sum(1 for c in resolved_combos if c["combo_outcome"] == "win")
    losses = len(resolved_combos) - wins
    summary = {
        "total":    len(combos_history),
        "resolved": len(resolved_combos),
        "wins":     wins,
        "losses":   losses,
        "win_rate": round(wins / len(resolved_combos) * 100, 1) if resolved_combos else None,
    }
    return jsonify({"combos": combos_history, "summary": summary})


def _resolve_live(pick: dict, home_score: int, away_score: int,
                  home_name: str, away_name: str) -> str:
    """
    Résout win/loss/push pour un match terminé à partir des scores.
    Évite toute correspondance de noms — utilise uniquement les scores et le type de pari.
    """
    import re as _re
    sel   = (pick.get("selection") or "").lower()
    btype = (pick.get("bet_type") or "").lower()
    total = home_score + away_score

    # --- Total / Over-Under ---
    if any(k in btype for k in ("total", "plus/moins", "over", "under")):
        # Détecter si c'est un total d'équipe (nom dans le bet_type)
        def _tm(name, bt):
            words = _re.findall(r'[a-z\u00e0-\u00ff]+', name.lower())
            return any(w in bt for w in words if len(w) > 2)

        if home_name and _tm(home_name, btype):
            score = home_score
        elif away_name and _tm(away_name, btype):
            score = away_score
        else:
            score = total

        m = _re.search(r'(\d+\.?\d*)', sel) or _re.search(r'(\d+\.?\d*)', btype)
        if m:
            line = float(m.group(1))
            if "plus" in sel or "over" in sel:
                return "win" if score > line else ("push" if score == line else "loss")
            if "moins" in sel or "under" in sel:
                return "win" if total < line else ("push" if total == line else "loss")
        return "pending"

    # --- Moneyline / Gagnant ---
    if any(k in btype for k in ("gagnant", "moneyline", "victoire", "winner", "2 issues")):
        winner = "home" if home_score > away_score else ("away" if away_score > home_score else "push")
        if winner == "push":
            return "push"
        # Cherche des mots-clés du nom de l'équipe dans la sélection
        home_tokens = [t for t in _re.sub(r'[()]', '', home_name).lower().split() if len(t) > 2]
        away_tokens = [t for t in _re.sub(r'[()]', '', away_name).lower().split() if len(t) > 2]
        # Aussi les tokens MO-J depuis pick
        home_moj = _re.sub(r'\s*\(.*?\)', '', (pick.get("home_team") or "")).lower()
        away_moj = _re.sub(r'\s*\(.*?\)', '', (pick.get("away_team") or "")).lower()
        for t in home_moj.split():
            if len(t) > 2:
                home_tokens.append(t)
        for t in away_moj.split():
            if len(t) > 2:
                away_tokens.append(t)

        sel_is_home = any(t in sel for t in home_tokens)
        sel_is_away = any(t in sel for t in away_tokens)
        if sel_is_home:
            return "win" if winner == "home" else "loss"
        if sel_is_away:
            return "win" if winner == "away" else "loss"
        return "pending"

    return "pending"


@app.route('/api/mlb-live')
def api_mlb_live():
    """Snapshot du jour + résultats/scores MLB en temps réel via statsapi."""
    from datetime import date as _date, datetime as _datetime, timedelta as _timedelta
    today = _today_mtl()

    # Charger le snapshot du jour
    snap = None
    daily_path = os.path.join(_SNAPSHOTS_DIR, f"{today}.json")
    if os.path.exists(daily_path):
        with open(daily_path, encoding="utf-8") as f:
            snap = json.load(f)
    elif os.path.exists(_SNAPSHOT_PATH):
        with open(_SNAPSHOT_PATH, encoding="utf-8") as f:
            candidate = json.load(f)
        if candidate.get("date") == today:
            snap = candidate

    if not snap:
        return jsonify({"error": "no_snapshot"}), 404

    # Scores MLB en direct via statsapi — indexé par team_id pour matching robuste
    game_scores = {}   # str(team_id) → entry
    total_games = 0
    try:
        import statsapi
        from mlb_stats import _find_team_id
        games = statsapi.schedule(sportId=1, date=today)
        total_games = len(games)
        for g in games:
            home      = g.get("home_name", "")
            away      = g.get("away_name", "")
            home_id   = g.get("home_id")
            away_id   = g.get("away_id")
            hs        = g.get("home_score") or 0
            as_       = g.get("away_score") or 0
            status    = g.get("status", "")
            inning      = g.get("current_inning", "")
            inning_half = g.get("inning_state", "")
            start_utc   = g.get("game_datetime", "")

            state = "pre"
            if status in ("Final", "Game Over", "Completed Early"):
                state = "final"
            elif status in ("In Progress", "Manager challenge", "Critical"):
                state = "live"
            elif status in ("Postponed", "Cancelled", "Suspended"):
                state = "postponed"

            score_str = ""
            if state == "final":
                score_str = f"Final {as_}–{hs}"
            elif state == "live":
                half = "Haut" if inning_half in ("Top", "Mid") else "Bas"
                score_str = f"{half} {inning}e — {as_}–{hs}"
            elif state == "postponed":
                score_str = "Reporté"

            entry = {
                "home_name":  home, "away_name":  away,
                "home_id":    home_id, "away_id": away_id,
                "home_score": hs,   "away_score": as_,
                "state":      state, "score_str": score_str,
                "inning":     inning, "status":   status,
                "start_utc":  start_utc,
            }
            if home_id:
                game_scores[str(home_id)] = entry
            if away_id:
                game_scores[str(away_id)] = entry
            # Fallback : clés par nom statsapi en minuscules
            game_scores[home.lower()] = entry
            game_scores[away.lower()] = entry
    except Exception as e:
        print(f"  [mlb-live] Erreur statsapi: {e}")

    def _find_game(team_name: str):
        """Trouve un match dans game_scores via team_id (robuste) ou nom partiel."""
        from mlb_stats import _find_team_id
        # 1. Via team_id — méthode la plus fiable
        tid = _find_team_id(team_name)
        if tid and str(tid) in game_scores:
            return game_scores[str(tid)]
        # 2. Nom brut direct
        raw = team_name.lower().strip()
        if raw in game_scores:
            return game_scores[raw]
        # 3. Sans parenthèses ex: "Tampa Bay (Rays)" → "tampa bay"
        import re as _re
        base = _re.sub(r'\s*\(.*?\)', '', raw).strip()
        if base in game_scores:
            return game_scores[base]
        # 4. Mot-clé partiel
        words = [w for w in base.split() if len(w) > 3]
        for key, entry in game_scores.items():
            if any(w in key for w in words):
                return entry
        return None

    # Construire la liste complète des matchs du jour : picks + autres matchs
    all_matches = []
    wins = losses = pending = 0
    net = 0.0
    matched_games = set()

    # 1. Ajouter les picks du snapshot (enrichis avec scores)
    for pick in snap.get("picks", []):
        home = (pick.get("home_team") or "")
        away = (pick.get("away_team") or "")
        game = _find_game(home) or _find_game(away)

        outcome = "pending"
        score_str = ""
        start_local = ""

        if game:
            score_str = game["score_str"]
            state = game["state"]
            if state == "pre":
                try:
                    dt = _datetime.fromisoformat(game["start_utc"].replace("Z", "+00:00"))
                    local = dt - _timedelta(hours=4)
                    start_local = local.strftime("Débute à %H h %M")
                except Exception:
                    start_local = "À venir"
                outcome = "pending"
            elif state in ("live", "final"):
                outcome = "live" if state == "live" else _resolve_live(
                    pick, game["home_score"], game["away_score"],
                    game["home_name"], game["away_name"],
                )
            matched_games.add((game["home_name"], game["away_name"]))

        is_bet = pick.get("is_bet", bool(pick.get("mise", 0)))
        mise   = (pick.get("mise") or 0.0) if is_bet else 0.0
        odds   = pick.get("odds") or 2.0
        profit = 0.0
        if is_bet:
            if outcome == "win":
                profit = mise * (odds - 1)
                wins += 1
            elif outcome == "loss":
                profit = -mise
                losses += 1
            else:
                pending += 1
        net += profit

        all_matches.append({
            **pick,
            "outcome":     outcome,
            "score_str":   score_str,
            "start_local": start_local,
            "profit":      round(profit, 2),
            "is_bet":      is_bet,
        })

    # 2. Ajouter les autres matchs du jour (sans paris)
    try:
        import statsapi
        games = statsapi.schedule(sportId=1, date=today)
        for g in games:
            home = g.get("home_name", "")
            away = g.get("away_name", "")
            # Passer si ce match a déjà des picks
            if (home, away) in matched_games:
                continue

            home_score = g.get("home_score") or 0
            away_score = g.get("away_score") or 0
            status = g.get("status", "")
            inning = g.get("current_inning", "")
            inning_half = g.get("inning_state", "")
            start_utc = g.get("game_datetime", "")

            state = "pre"
            if status in ("Final", "Game Over", "Completed Early"):
                state = "final"
            elif status in ("In Progress", "Manager challenge", "Critical"):
                state = "live"
            elif status in ("Postponed", "Cancelled", "Suspended"):
                state = "postponed"

            score_str = ""
            if state == "final":
                score_str = f"Final {away_score}–{home_score}"
            elif state == "live":
                half = "Haut" if inning_half in ("Top", "Mid") else "Bas"
                score_str = f"{half} {inning}e — {away_score}–{home_score}"
            elif state == "postponed":
                score_str = "Reporté"
            else:  # pre
                try:
                    dt = _datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
                    local = dt - _timedelta(hours=4)
                    score_str = local.strftime("Débute à %H h %M")
                except Exception:
                    score_str = "À venir"

            all_matches.append({
                "match":       f"{away} @ {home}",
                "away_team":   away,
                "home_team":   home,
                "score_str":   score_str,
                "state":       state,
                "is_bet":      False,
            })
    except Exception as e:
        print(f"  [mlb-live] Erreur pour autres matchs: {e}")

    return jsonify({
        "date":           snap.get("date"),
        "time":           snap.get("time"),
        "all_matches":    all_matches,
        "wins":           wins,
        "losses":         losses,
        "pending":        pending,
        "net":            round(net, 2),
        "total_matches":  total_games,
        "fetched_at":     _now_mtl().strftime("%H:%M:%S"),
    })


@app.route('/api/weights')
def api_weights():
    """Retourne les poids actuels des facteurs et les valeurs par défaut."""
    from mlb_stats import load_weights, load_stat_vs_math, DEFAULT_WEIGHTS
    w = load_weights()
    svmath = load_stat_vs_math()
    return jsonify({
        "weights":      w,
        "defaults":     DEFAULT_WEIGHTS,
        "stat_vs_math": svmath,
    })


@app.route('/api/calibrate', methods=['POST'])
def api_calibrate():
    """
    Recalibre les poids des facteurs basé sur l'historique des snapshots.
    Requiert au minimum 10 picks résolus avec factor_scores.
    """
    from datetime import date
    today = _today_mtl()

    if not os.path.isdir(_SNAPSHOTS_DIR):
        return jsonify({"error": "Aucun snapshot disponible"}), 404

    past_files = sorted(
        [f for f in os.listdir(_SNAPSHOTS_DIR) if f.endswith(".json") and f[:10] <= today]
    )
    if not past_files:
        return jsonify({"error": "Aucun snapshot disponible"}), 404

    # Charger tous les picks résolus avec factor_scores
    all_picks = []
    for fname in past_files:
        with open(os.path.join(_SNAPSHOTS_DIR, fname), encoding="utf-8") as f:
            snap = json.load(f)
        snap_date   = snap.get("date", fname[:10])
        mlb_results = _fetch_mlb_results(snap_date)
        for pick in snap.get("picks", []):
            if not pick.get("factor_scores"):
                continue
            outcome = _resolve_pick(pick, mlb_results)
            if outcome not in ("win", "loss"):
                continue
            all_picks.append({**pick, "outcome": outcome})

    if len(all_picks) < 10:
        return jsonify({
            "error": f"Données insuffisantes — {len(all_picks)} picks résolus avec scores de facteurs (minimum 10). "
                     "Continuez à sauvegarder des snapshots pour accumuler des données."
        }), 400

    from mlb_stats import load_weights, save_weights, DEFAULT_WEIGHTS

    current_weights = load_weights()
    factors = list(DEFAULT_WEIGHTS.keys())

    new_weights   = {}
    factor_stats  = {}

    for factor in factors:
        high_picks = [p for p in all_picks if p["factor_scores"].get(factor, 0.5) > 0.5]
        low_picks  = [p for p in all_picks if p["factor_scores"].get(factor, 0.5) <= 0.5]

        if len(high_picks) >= 3 and len(low_picks) >= 3:
            high_wr = sum(1 for p in high_picks if p["outcome"] == "win") / len(high_picks)
            low_wr  = sum(1 for p in low_picks  if p["outcome"] == "win") / len(low_picks)
            # delta positif = facteur élevé → plus de victoires → augmenter le poids
            delta   = high_wr - low_wr
            new_w   = max(0.01, current_weights.get(factor, DEFAULT_WEIGHTS[factor]) * (1.0 + delta))
            new_weights[factor] = new_w
            factor_stats[factor] = {
                "high_wr":          round(high_wr * 100, 1),
                "low_wr":           round(low_wr  * 100, 1),
                "delta":            round(delta * 100, 1),
                "predictive_power": round(abs(delta) * 100, 1),
                "high_n":           len(high_picks),
                "low_n":            len(low_picks),
            }
        else:
            new_weights[factor] = current_weights.get(factor, DEFAULT_WEIGHTS[factor])
            factor_stats[factor] = {"insufficient_data": True, "n": len(all_picks)}

    # Normaliser pour que la somme = 1
    total = sum(new_weights.values())
    normalized = {k: round(v / total, 4) for k, v in new_weights.items()}

    save_weights(normalized)

    # ── Auto-ajustement stat_vs_math selon la calibration ────────────────
    # Construire les bins depuis les picks résolus
    stat_weight_result = {}
    buckets = [(i / 10, (i + 1) / 10) for i in range(4, 9)]
    calibration_bins = []
    for lo, hi in buckets:
        bin_picks = [p for p in all_picks if lo <= (p.get("fair_prob") or 0) < hi]
        if bin_picks:
            wins = sum(1 for p in bin_picks if p["outcome"] == "win")
            calibration_bins.append({
                "predicted": round((lo + hi) / 2 * 100, 1),
                "actual":    round(wins / len(bin_picks) * 100, 1),
                "count":     len(bin_picks),
            })
    if calibration_bins:
        stat_weight_result = _auto_adjust_stat_weight(calibration_bins)

    return jsonify({
        "ok":               True,
        "picks_analyzed":   len(all_picks),
        "old_weights":      current_weights,
        "new_weights":      normalized,
        "factor_stats":     factor_stats,
        "stat_weight_adj":  stat_weight_result,
    })


def _compute_bin_weighted_bias(calibration_bins: list) -> float:
    """
    Calcule le biais pondéré par le nombre de picks dans chaque bin.
    bias > 0  → sous-estimation (prédir trop bas)
    bias < 0  → sur-estimation (prédire trop haut) → réduire stat_weight
    """
    total_n = sum(b.get("count", 0) for b in calibration_bins)
    if total_n == 0:
        return 0.0
    weighted = sum(
        (b.get("actual", 50) - b.get("predicted", 50)) * b.get("count", 0)
        for b in calibration_bins
    )
    return round(weighted / total_n / 100.0, 4)  # convertir % → fraction


def _auto_adjust_stat_weight(calibration_bins: list) -> dict:
    """
    Ajuste stat_vs_math dans weights.json selon le biais de calibration.
    Sensibilité : 0.50 (25% de sur-estimation → réduction de 0.125)
    Limites : [0.20, 0.65]
    """
    from mlb_stats import load_stat_vs_math, save_stat_vs_math

    if not calibration_bins:
        return {"ok": False, "reason": "Aucun bin de calibration"}

    total_n = sum(b.get("count", 0) for b in calibration_bins)
    if total_n < 10:
        return {"ok": False, "reason": f"Données insuffisantes ({total_n} picks)"}

    bias = _compute_bin_weighted_bias(calibration_bins)
    current = load_stat_vs_math()

    # Sensibilité : chaque 10% de sur-estimation → réduction de 0.05 de stat_weight
    # bias négatif = sur-estimation → ajustement négatif (réduit stat_weight)
    SENSITIVITY = 0.50
    adjustment = bias * SENSITIVITY
    new_weight = max(0.20, min(0.65, round(current + adjustment, 3)))

    # Pas d'ajustement si changement < 0.5%
    if abs(new_weight - current) < 0.005:
        return {
            "ok":      True,
            "changed": False,
            "current": current,
            "bias_pct": round(bias * 100, 1),
            "reason":  "Biais trop faible pour ajustement",
        }

    saved = save_stat_vs_math(new_weight)
    return {
        "ok":       True,
        "changed":  True,
        "old":      current,
        "new":      saved,
        "bias_pct": round(bias * 100, 1),
        "adjustment": round(adjustment, 3),
        "n":        total_n,
    }


@app.route('/api/auto_calibrate_stat_weight', methods=['GET', 'POST'])
def api_auto_calibrate_stat_weight():
    """
    Recalibrage automatique de stat_vs_math basé sur la courbe de calibration.
    Lit les predictions.json, calcule le biais bin par bin, ajuste weights.json.
    """
    from datetime import date
    today = _today_mtl()

    if not os.path.isdir(_SNAPSHOTS_DIR):
        return jsonify({"ok": False, "reason": "Aucun snapshot disponible"}), 404

    past_files = sorted(
        [f for f in os.listdir(_SNAPSHOTS_DIR) if f.endswith(".json") and f[:10] < today]
    )
    if not past_files:
        return jsonify({"ok": False, "reason": "Aucun snapshot passé"}), 404

    # Reconstruire les bins de calibration depuis les snapshots
    all_resolved = []
    for fname in past_files:
        try:
            with open(os.path.join(_SNAPSHOTS_DIR, fname), encoding="utf-8") as f:
                snap = json.load(f)
            snap_date   = snap.get("date", fname[:10])
            mlb_results = _fetch_mlb_results(snap_date)
            for pick in snap.get("picks", []):
                outcome = _resolve_pick(pick, mlb_results)
                if outcome not in ("win", "loss"):
                    continue
                all_resolved.append({**pick, "outcome": outcome})
        except Exception:
            continue

    if len(all_resolved) < 10:
        return jsonify({
            "ok":     False,
            "reason": f"Données insuffisantes ({len(all_resolved)} picks résolus, minimum 10)"
        }), 400

    # Construire les bins (40-50, 50-60, 60-70, 70-80, 80-90%)
    buckets = [(i / 10, (i + 1) / 10) for i in range(4, 9)]
    calibration_bins = []
    for lo, hi in buckets:
        picks = [p for p in all_resolved if lo <= (p.get("fair_prob") or 0) < hi]
        if picks:
            wins = sum(1 for p in picks if p["outcome"] == "win")
            calibration_bins.append({
                "label":     f"{int(lo*100)}-{int(hi*100)}%",
                "predicted": round((lo + hi) / 2 * 100, 1),
                "actual":    round(wins / len(picks) * 100, 1),
                "count":     len(picks),
            })

    result = _auto_adjust_stat_weight(calibration_bins)
    result["bins"] = calibration_bins
    return jsonify(result)


@app.route('/api/refresh', methods=['POST'])
def api_refresh():
    """Force un re-scrape (invalide le cache)."""
    global _scrape_cache
    with _scrape_lock:
        _scrape_cache = None
    with _lock:
        _cache["status"] = "idle"
        _cache["data"]   = None
        _cache["date"]   = None
    return jsonify({"status": "cache_cleared"})


# ─── Combos Même Soir MLB ─────────────────────────────────────────────────────

def _generate_mlb_combos(opp_list: list, n: int = 3) -> list:
    """
    Génère n propositions de parlay pour la soirée MLB.
    Stratégie : picks Excellent de matchs DIFFÉRENTS (cross-game parlay).
    Bonus : si ≥2 picks Excellent sur le même match, ajoute un SGP en priorité.
    """
    import re as _re
    from collections import defaultdict

    def _short(name: str) -> str:
        m = _re.search(r'\(([^)]+)\)', name or "")
        return m.group(1) if m else (name or "").split()[-1] if name else "?"

    def _is_total(p) -> bool:
        return any(k in (p.get("bet_type") or "").lower() for k in ("total", "plus/moins"))

    def _is_winner(p) -> bool:
        return any(k in (p.get("bet_type") or "").lower()
                   for k in ("gagnant", "moneyline", "victoire", "2 issues"))

    def _pick_entry(p, home=None, away=None):
        return {
            "bet_type":    p.get("bet_type"),
            "selection":   p.get("selection") or p.get("selection_label"),
            "odds":        p.get("odds"),
            "fair_prob":   p.get("fair_prob"),
            "value_score": p.get("value_score"),
            "home_team":   home or p.get("home_team", ""),
            "away_team":   away or p.get("away_team", ""),
        }

    def _combo(picks, combo_type):
        """Construit un dict combo à partir d'une liste de picks (2 ou 3)."""
        combined = round(
            __import__('functools').reduce(
                lambda a, b: a * b,
                [float(p.get("odds") or 1.0) for p in picks]
            ), 2
        )
        avg_score = round(sum((p.get("value_score") or 50) for p in picks) / len(picks), 1)
        # Label court : surnom des équipes impliquées
        teams = list(dict.fromkeys(
            _short(p.get("home_team") or p.get("away_team", "?"))
            for p in picks
        ))
        short_match = " · ".join(teams[:3])
        return {
            "match":         " + ".join(
                f"{_short(p.get('away_team','?'))}@{_short(p.get('home_team','?'))}"
                for p in picks
            ),
            "short_match":   short_match,
            "home_team":     picks[0].get("home_team", ""),
            "away_team":     picks[0].get("away_team", ""),
            "combo_type":    combo_type,
            "label":         " + ".join(
                (p.get("selection") or p.get("selection_label") or p.get("bet_type") or "?")
                for p in picks
            ),
            "combined_odds": combined,
            "avg_score":     avg_score,
            "mise":          None,
            "picks":         [_pick_entry(p) for p in picks],
        }

    # ── Picks Excellent triés par score ──────────────────────────────
    excellent = sorted(
        [p for p in opp_list if "Excellent" in (p.get("recommendation") or "")],
        key=lambda p: -(p.get("value_score") or 0),
    )
    if len(excellent) < 2:
        return []

    proposals = []

    def _match_key(p):
        return f"{p.get('home_team','')}|{p.get('away_team','')}"

    def _pick_key(p):
        """Clé unique d'un pick (pour la déduplication)."""
        return (_match_key(p), (p.get("bet_type") or "").lower()[:40])

    # ── 1. SGP (Même Match) si possible ──────────────────────────────
    by_match = defaultdict(list)
    for p in excellent:
        by_match[_match_key(p)].append(p)

    sgp_pick_keys = set()
    for key, picks in by_match.items():
        if len(picks) < 2 or len(proposals) >= n:
            continue
        winners = [p for p in picks if _is_winner(p)]
        totals  = [p for p in picks if _is_total(p)]
        if winners and totals:
            pair, ctype = [winners[0], totals[0]], "SGP : Gagnant + Total"
        elif len(totals) >= 2:
            pair, ctype = totals[:2], "SGP : Double Total"
        else:
            pair, ctype = picks[:2], "SGP : Double sélection"
        combined = pair[0].get("odds", 1.0) * pair[1].get("odds", 1.0)
        if combined > 3.0:
            home = picks[0].get("home_team", "")
            away = picks[0].get("away_team", "")
            combo = _combo(pair, ctype)
            combo["short_match"] = f"{_short(away)} @ {_short(home)}"
            combo["match"]       = f"{away} @ {home}"
            proposals.append(combo)
            sgp_pick_keys.update(_pick_key(p) for p in pair)

    # ── 2. Cross-game parlays ─────────────────────────────────────────
    def _best_picks_distinct(pool, k):
        """Retourne k picks avec au plus 1 par match, triés par value_score."""
        result, seen = [], set()
        for p in pool:
            mk = _match_key(p)
            if mk not in seen:
                seen.add(mk)
                result.append(p)
            if len(result) == k:
                break
        return result

    # Piste des picks déjà utilisés dans les combos cross-game
    used_cross = set()   # set de _pick_key

    def _not_duplicate_combo(picks):
        """Retourne True si ce combo apporte au moins 1 pick nouveau."""
        return any(_pick_key(p) not in used_cross for p in picks)

    remaining = [p for p in excellent if _pick_key(p) not in sgp_pick_keys]

    # Double du soir (top 2, matchs différents)
    if len(proposals) < n:
        pair = _best_picks_distinct(remaining, 2)
        if len(pair) == 2 and pair[0].get("odds", 1.0) * pair[1].get("odds", 1.0) > 2.5:
            proposals.append(_combo(pair, "Double du soir"))
            used_cross.update(_pick_key(p) for p in pair)

    # Triple du soir (top 3, matchs différents)
    if len(proposals) < n:
        trip = _best_picks_distinct(remaining, 3)
        if len(trip) == 3 and _not_duplicate_combo(trip):
            proposals.append(_combo(trip, "Triple du soir"))
            used_cross.update(_pick_key(p) for p in trip)

    # Double alternatif (picks 3+4, matchs différents) — diversité
    if len(proposals) < n:
        alt_pool = [p for p in remaining if _pick_key(p) not in used_cross]
        pair_alt = _best_picks_distinct(alt_pool, 2)
        if len(pair_alt) == 2:
            alt_label = "Double Total O/U" if all(_is_total(p) for p in pair_alt) else \
                        "Double Gagnant" if all(_is_winner(p) for p in pair_alt) else \
                        "Double alternatif"
            proposals.append(_combo(pair_alt, alt_label))
            used_cross.update(_pick_key(p) for p in pair_alt)

    proposals.sort(key=lambda x: -x["avg_score"])
    return proposals[:n]


# ─── Construction du payload ──────────────────────────────────────────────────

def _apply_mises_nhl(opps, budget: float, max_bets: int = 7, min_bets: int = 3,
                     min_bet: float = 0.5) -> list[float]:
    """
    Même logique que l'app NHL :
    - Top max_bets picks avec Kelly > 0 (complété à min_bets si besoin)
    - Distribution proportionnelle aux fractions Kelly
    - Arrondi à $0.50, min $0.50/pari
    - Total ajusté exactement au budget
    """
    # Sélection : top picks avec Kelly > 0
    kelly_pos = [o for o in opps if o.kelly_fraction > 0]
    selected  = kelly_pos[:max_bets]

    # Compléter jusqu'à min_bets avec les meilleurs value_score si besoin
    if len(selected) < min_bets:
        rest = [o for o in opps if o not in selected]
        rest.sort(key=lambda o: -(o.value_score or 0))
        selected += rest[:min_bets - len(selected)]

    if not selected:
        return [0.0] * len(opps)

    weights   = [o.kelly_fraction if o.kelly_fraction > 0 else 0.001 for o in selected]
    total_w   = sum(weights)

    # Proportionnel au budget
    amounts = [w / total_w * budget for w in weights]
    # Arrondi à $0.50, min $0.50
    amounts = [max(round(a * 2) / 2, min_bet) for a in amounts]
    tot     = sum(amounts)
    # Re-normaliser pour coller au budget
    amounts = [round(a / tot * budget * 2) / 2 for a in amounts]
    amounts = [max(a, min_bet) for a in amounts]
    # Ajuster le plus gros pari pour que total = budget exactement
    diff = round((budget - sum(amounts)) * 2) / 2
    if diff != 0:
        mx = amounts.index(max(amounts))
        amounts[mx] = round((amounts[mx] + diff) * 2) / 2

    # Mapper sur la liste complète d'opps
    result     = [0.0] * len(opps)
    sel_index  = {id(o): amt for o, amt in zip(selected, amounts)}
    for i, o in enumerate(opps):
        result[i] = sel_index.get(id(o), 0.0)
    return result


@app.route('/api/compare-systems')
def api_compare_systems():
    """Compare 6 stratégies de mise MLB sur les snapshots historiques.

    A : ½ Kelly Standard   — tous les picks, mise Kelly existante
    B : Moneyline Only     — seulement gagnant/2 issues, mise plate
    C : Excellents Only    — picks Excellent *** uniquement, mise Kelly
    D : Kelly Plafonné     — tous les picks, mise = min(Kelly, plafond)
    E : Under Seulement    — picks "Moins de" uniquement, mise Kelly
    F : Kelly Dynamique    — % du bankroll courant réparti par Kelly chaque soir
    """
    flat_amt       = float(request.args.get("flat",          10))
    cap_amt        = float(request.args.get("cap",            5))
    bankroll_start = float(request.args.get("bankroll_start", 100))
    nightly_pct    = float(request.args.get("nightly_pct",    10))
    _MIN_BET       = 0.50

    def _is_moneyline(bt):
        return any(k in bt.lower() for k in ("gagnant", "2 issues", "winner", "moneyline", "victoire"))

    def _is_under(sel):
        return any(k in sel.lower() for k in ("moins", "under"))

    def _half_kelly(fp, odds):
        """½ Kelly fraction (0–1)."""
        b = odds - 1.0
        if b <= 0 or fp <= 0:
            return 0.0
        return max(0.0, (fp * b - (1.0 - fp)) / b * 0.5)

    snap_dir = _SNAPSHOTS_DIR
    if not os.path.isdir(snap_dir):
        return jsonify({"days": [], "summary": {}, "picks": []})

    today      = _today_mtl()
    all_picks  = []
    days_out   = []
    cum        = {k: 0.0 for k in "ABCDEFG"}
    bankroll_f = bankroll_start   # bankroll dynamique Système F

    for fname in sorted(os.listdir(snap_dir)):
        if not fname.endswith(".json"):
            continue
        date_str = fname[:-5]
        if date_str >= today:
            continue

        try:
            with open(os.path.join(snap_dir, fname), encoding="utf-8") as f:
                snap = json.load(f)
        except Exception:
            continue

        mlb_results = _fetch_mlb_results(date_str)
        snap_picks  = [p for p in snap.get("picks", [])
                       if p.get("is_bet") or p.get("mise", 0)]

        # Collecter les picks résolus du jour
        valid_day = []
        for p in snap_picks:
            mise_a = float(p.get("mise") or 0)
            if mise_a <= 0:
                continue
            odds = float(p.get("odds") or 0)
            if odds <= 1:
                continue
            outcome = _resolve_pick(p, mlb_results)
            if outcome not in ("win", "loss"):
                continue
            fp = float(p.get("fair_prob") or 0)
            valid_day.append({
                "p":       p,
                "mise_a":  mise_a,
                "odds":    odds,
                "fp":      fp,
                "outcome": outcome,
                "edge":    round((fp - 1.0 / odds) * 100, 2),
                "rec":     p.get("recommendation", ""),
                "bt":      p.get("bet_type", ""),
                "sel":     p.get("selection", ""),
                "hk":      _half_kelly(fp, odds),
            })

        if not valid_day:
            continue

        # ── Calcul des mises Système F (Kelly dynamique) ─────────────────
        budget_f = round(bankroll_f * nightly_pct / 100, 2) if bankroll_f >= _MIN_BET else 0.0
        f_mises  = []
        if valid_day and budget_f >= _MIN_BET:
            total_hk = sum(pk["hk"] for pk in valid_day)
            if total_hk > 0:
                raw = [pk["hk"] / total_hk * budget_f for pk in valid_day]
            else:
                eq  = budget_f / len(valid_day)
                raw = [eq] * len(valid_day)
            f_mises = [max(round(r * 2) / 2, _MIN_BET) for r in raw]
            # Ajuster le plus gros pick pour coller exactement au budget
            diff = round((budget_f - sum(f_mises)) * 2) / 2
            if diff != 0 and f_mises:
                mx = f_mises.index(max(f_mises))
                f_mises[mx] = max(round((f_mises[mx] + diff) * 2) / 2, _MIN_BET)
        else:
            f_mises = [0.0] * len(valid_day)

        day = {k: {"mise": 0.0, "net": 0.0, "picks": 0, "wins": 0, "losses": 0}
               for k in "ABCDEFG"}
        day["date"] = date_str
        day["F"]["bankroll_before"] = round(bankroll_f, 2)
        day_f_net = 0.0

        for i, pk in enumerate(valid_day):
            mise_a  = pk["mise_a"]
            odds    = pk["odds"]
            outcome = pk["outcome"]
            win     = outcome == "win"
            rec     = pk["rec"]
            bt      = pk["bt"]
            sel     = pk["sel"]
            edge    = pk["edge"]
            mise_f  = f_mises[i]

            def _net(mise, w): return round(mise * (odds - 1), 2) if w else round(-mise, 2)

            a_net = _net(mise_a, win)
            b_act  = _is_moneyline(bt);  b_mise = flat_amt if b_act else 0;  b_net = _net(b_mise, win) if b_act else 0
            c_act  = "Excellent" in rec; c_mise = mise_a if c_act else 0;    c_net = _net(c_mise, win) if c_act else 0
            d_mise = min(mise_a, cap_amt);                                    d_net = _net(d_mise, win)
            e_act  = _is_under(sel);     e_mise = mise_a if e_act else 0;    e_net = _net(e_mise, win) if e_act else 0
            f_net  = _net(mise_f, win) if mise_f > 0 else 0
            day_f_net += f_net

            # Système G : Conservateur (Phase 1 — cotes ≥1.80, no totals, downgrade Excellent)
            g_act = (odds >= 1.80 and _is_moneyline(bt) and
                    "Bon" in rec)  # downgrade de Excellent
            g_mise = mise_a if g_act else 0
            g_net = _net(g_mise, win) if g_act else 0

            for key, mise_v, net_v, active in [
                ("A", mise_a, a_net, True),
                ("B", b_mise, b_net, b_act),
                ("C", c_mise, c_net, c_act),
                ("D", d_mise, d_net, True),
                ("E", e_mise, e_net, e_act),
                ("F", mise_f, f_net, mise_f > 0),
                ("G", g_mise, g_net, g_act),
            ]:
                if not active:
                    continue
                day[key]["picks"]  += 1
                day[key]["mise"]   += mise_v
                day[key]["net"]    += net_v
                day[key]["wins"]   += 1 if win else 0
                day[key]["losses"] += 0 if win else 1

            all_picks.append({
                "date": date_str,
                "match": pk["p"].get("match", ""),
                "selection": sel, "bet_type": bt,
                "odds": odds, "edge": edge, "outcome": outcome,
                "A_mise": mise_a, "A_net": a_net,
                "B_active": b_act, "B_mise": b_mise, "B_net": b_net,
                "C_active": c_act, "C_mise": c_mise, "C_net": c_net,
                "D_mise": d_mise, "D_net": d_net, "D_capped": mise_a > cap_amt,
                "E_active": e_act, "E_mise": e_mise, "E_net": e_net,
                "F_mise": mise_f, "F_net": f_net, "F_bankroll": round(bankroll_f, 2),
            })

        # Mettre à jour le bankroll F
        bankroll_f = max(0.0, round(bankroll_f + day_f_net, 2))
        day["F"]["bankroll_after"] = bankroll_f

        for k in "ABCDEFG":
            day[k]["net"]  = round(day[k]["net"], 2)
            day[k]["mise"] = round(day[k]["mise"], 2)
            cum[k] = round(cum[k] + day[k]["net"], 2)
            day[k]["cumulative"] = cum[k]

        days_out.append(day)

    def _summary(k):
        picks  = sum(d[k]["picks"]  for d in days_out)
        wins   = sum(d[k]["wins"]   for d in days_out)
        losses = sum(d[k]["losses"] for d in days_out)
        total  = sum(d[k]["mise"]   for d in days_out)
        net    = cum[k]
        res = {
            "picks":      picks,
            "wins":       wins,
            "losses":     losses,
            "cumulative": net,
            "roi":        round(net / total * 100, 1) if total else 0,
            "win_rate":   round(wins / (wins + losses) * 100, 1) if (wins + losses) else 0,
            "total_mise": round(total, 2),
        }
        if k == "F":
            res["initial_bankroll"] = bankroll_start
            res["final_bankroll"]   = round(bankroll_f, 2)
            res["roi"] = round((bankroll_f - bankroll_start) / bankroll_start * 100, 1) if bankroll_start else 0
        return res

    return jsonify({
        "days":    days_out,
        "summary": {k: _summary(k) for k in "ABCDEFG"},
        "picks":   all_picks,
        "params":  {"flat": flat_amt, "cap": cap_amt,
                    "bankroll_start": bankroll_start, "nightly_pct": nightly_pct},
    })


def _get_mlb_team_logo(team_name: str) -> str:
    """Retourne l'URL du logo ESPN pour une équipe MLB."""
    if not team_name:
        return ""

    # Mapping vers abréviations ESPN/MLB (matching le template)
    abbrev_map = {
        "orioles": "BAL", "baltimore": "BAL",
        "red sox": "BOS", "boston": "BOS",
        "cubs": "CHC", "chicago cubs": "CHC", "chicago": "CHC",
        "white sox": "CWS",
        "reds": "CIN", "cincinnati": "CIN",
        "guardians": "CLE", "cleveland": "CLE",
        "rockies": "COL", "colorado": "COL",
        "tigers": "DET", "detroit": "DET",
        "astros": "HOU", "houston": "HOU",
        "royals": "KC", "kansas city": "KC",
        "angels": "LAA", "los angeles angels": "LAA", "los angeles": "LAD",
        "dodgers": "LAD", "los angeles dodgers": "LAD",
        "marlins": "MIA", "miami": "MIA",
        "brewers": "MIL", "milwaukee": "MIL",
        "twins": "MIN", "minnesota": "MIN",
        "mets": "NYM", "new york mets": "NYM",
        "yankees": "NYY", "new york yankees": "NYY", "new york": "NYY",
        "athletics": "OAK", "oakland": "OAK",
        "phillies": "PHI", "philadelphia": "PHI", "philadelphie": "PHI",
        "pirates": "PIT", "pittsburgh": "PIT",
        "cardinals": "STL", "st. louis": "STL", "saint-louis": "STL", "st louis": "STL",
        "padres": "SD", "san diego": "SD",
        "giants": "SF", "san francisco": "SF",
        "mariners": "SEA", "seattle": "SEA",
        "rays": "TB", "tampa bay": "TB",
        "rangers": "TEX", "texas": "TEX",
        "blue jays": "TOR", "toronto": "TOR",
        "nationals": "WSH", "washington": "WSH",
        "diamondbacks": "ARI", "arizona": "ARI",
        "braves": "ATL", "atlanta": "ATL",
    }

    # Normaliser et chercher
    s = team_name.lower().strip()
    abbrev = abbrev_map.get(s)

    if not abbrev:
        # Chercher par substring
        for key, val in abbrev_map.items():
            if key in s or s in key:
                abbrev = val
                break

    if abbrev:
        return f"https://a.espncdn.com/i/teamlogos/mlb/500/{abbrev}.png"

    return ""


def _build_payload(opps, matches, bankroll: float, kelly_frac: float,
                   max_nightly: float = None, all_picks=None, mode: str = "standard") -> dict:
    """Construit le dict JSON retourné au frontend."""
    from kelly import edge_percent

    budget    = max_nightly if (max_nightly and max_nightly > 0) else 10.0
    allocated = _apply_mises_nhl(opps, budget)

    # Pré-charger la météo pour tous les matchs uniques (1 appel par stade)
    _match_weather: dict = {}
    try:
        from weather import get_weather
        from mlb_stats import _find_team_id
        for opp in opps:
            ht = opp.match.home_team
            if ht not in _match_weather:
                tid = _find_team_id(ht)
                if tid:
                    w = get_weather(tid, game_hour=19)
                    _match_weather[ht] = w or {}
    except Exception:
        pass

    opp_list = []
    for opp, bet_amount in zip(opps, allocated):
        # Récupérer les scores par facteur (pour la calibration adaptative)
        try:
            from mlb_stats import get_factor_scores
            home_fs = get_factor_scores(opp.match.home_team, is_home=True)
            away_fs = get_factor_scores(opp.match.away_team, is_home=False)
            factor_scores = {k: round((home_fs.get(k, 0.5) - away_fs.get(k, 0.5)) / 2 + 0.5, 4)
                             for k in home_fs}
        except Exception:
            factor_scores = {}

        # Météo du stade (déjà fetchée au-dessus)
        w = _match_weather.get(opp.match.home_team, {})
        weather_info = {
            "temp_c":        w.get("temp_c"),
            "wind_kmh":      w.get("wind_kmh"),
            "wind_cardinal": w.get("wind_cardinal"),
            "wind_component": w.get("wind_component"),
            "run_adjustment": w.get("run_adjustment", 0.0),
            "precip_prob":   w.get("precip_prob"),
            "is_dome":       w.get("is_dome", False),
            "description":   w.get("description", ""),
        } if w else {}

        # Ajouter les logos MLB
        away_logo = _get_mlb_team_logo(opp.match.away_team)
        home_logo = _get_mlb_team_logo(opp.match.home_team)

        opp_list.append({
            "match":          f"{opp.match.away_team} @ {opp.match.home_team}",
            "away_team":      opp.match.away_team,
            "home_team":      opp.match.home_team,
            "away_logo":      away_logo,
            "home_logo":      home_logo,
            "date":           opp.match.date,
            "time":           opp.match.time,
            "league":         opp.league,
            "bet_type":       opp.bet_type,
            "selection_label": opp.selection_label,
            "odds":           opp.odds,
            "house_margin":   round(opp.house_margin, 2),
            "value_score":    round(opp.value_score, 1),
            "recommendation": opp.recommendation,
            "fair_prob":      round(opp.fair_prob, 4),
            "implied_prob":   round(opp.implied_prob, 4),
            "math_prob":      round(opp.math_prob, 4),
            "edge_pct":       edge_percent(opp.fair_prob, opp.odds),
            "kelly_fraction": round(opp.kelly_fraction, 4),
            "kelly_bet":      bet_amount,
            "kelly_potential": round(bet_amount * opp.odds, 2) if bet_amount > 0 else 0,
            "pitcher_info":   opp.pitcher_info,
            "prediction_id":  opp.prediction_id,
            "event_url":      opp.match.event_url,
            "factor_scores":  factor_scores,
            "weather":        weather_info,
        })

    n_excellent = sum(1 for o in opp_list if "Excellent" in o["recommendation"])
    n_bon       = sum(1 for o in opp_list if o["recommendation"].startswith("Bon"))
    avg_margin  = (sum(o["house_margin"] for o in opp_list) / len(opp_list)
                   if opp_list else 0.0)
    total_kelly = sum(o["kelly_bet"] for o in opp_list)

    # Soirée peu conseillée si aucun paris Excellent OU edge moyen < 2%
    avg_edge    = (sum(o["edge_pct"] for o in opp_list) / len(opp_list)
                   if opp_list else 0.0)
    confidence  = round(avg_edge, 2)   # % d'avantage moyen sur les paris sélectionnés
    low_value   = n_excellent == 0 or avg_edge < 2.0 or len(opp_list) == 0

    combos = _generate_mlb_combos(opp_list)

    # ── Prédictions informatives (tous les picks analysés, sans mises) ──────────
    # Utilisées pour afficher les analyses même quand aucun pari n'est recommandé,
    # et pour alimenter les statistiques (is_bet=False dans les snapshots).
    info_list = []
    if all_picks:
        # Déduper : ne pas doubler les opps que l'on vient d'afficher
        # Inclure selection_label pour bien différencier Over vs Under du même marché
        opp_keys = {(o.match.date, o.match.home_team, o.match.away_team, o.bet_type, o.selection_label)
                    for o in opps}
        for opp in all_picks:
            # Sauter si déjà dans opp_list (déjà affiché avec mise)
            opp_key = (opp.match.date, opp.match.home_team, opp.match.away_team, opp.bet_type, opp.selection_label)
            if opp_key in opp_keys:
                continue

            w = _match_weather.get(opp.match.home_team, {})
            weather_info_i = {
                "run_adjustment": w.get("run_adjustment", 0.0),
                "temp_c":        w.get("temp_c"),
                "wind_kmh":      w.get("wind_kmh"),
                "wind_cardinal": w.get("wind_cardinal"),
                "is_dome":       w.get("is_dome", False),
                "precip_prob":   w.get("precip_prob"),
            } if w else {}
            # Ajouter les logos MLB
            away_logo_i = _get_mlb_team_logo(opp.match.away_team)
            home_logo_i = _get_mlb_team_logo(opp.match.home_team)

            info_list.append({
                "match":          f"{opp.match.away_team} @ {opp.match.home_team}",
                "away_team":      opp.match.away_team,
                "home_team":      opp.match.home_team,
                "away_logo":      away_logo_i,
                "home_logo":      home_logo_i,
                "date":           opp.match.date,
                "time":           opp.match.time,
                "league":         opp.league,
                "bet_type":       opp.bet_type,
                "selection_label": opp.selection_label,
                "odds":           opp.odds,
                "house_margin":   round(opp.house_margin, 2),
                "value_score":    round(opp.value_score, 1),
                "recommendation": opp.recommendation,
                "fair_prob":      round(opp.fair_prob, 4),
                "implied_prob":   round(opp.implied_prob, 4),
                "math_prob":      round(opp.math_prob, 4),
                "edge_pct":       edge_percent(opp.fair_prob, opp.odds),
                "kelly_fraction": round(opp.kelly_fraction, 4),
                "pitcher_info":   opp.pitcher_info,
                "prediction_id":  opp.prediction_id,
                "event_url":      opp.match.event_url,
                "factor_scores":  {},
                "weather":        weather_info_i,
                "is_info_only":   True,   # flag : ne pas miser
            })

    # Carousel = matchs d'AUJOURD'HUI uniquement (heure Montréal), avec scores live
    from datetime import timedelta as _td
    today_mtl = (datetime.utcnow() - _td(hours=4)).strftime('%Y-%m-%d')

    # Index des données live par (home, away)
    live_by_key = {}
    for m in matches:
        live_by_key[(m.home_team, m.away_team)] = {
            "live_status":     getattr(m, 'live_status', ''),
            "detailed_status": getattr(m, 'detailed_status', ''),
            "away_score":      getattr(m, 'away_score', 0),
            "home_score":      getattr(m, 'home_score', 0),
            "current_inning":  getattr(m, 'current_inning', ''),
        }

    # ── Carousel : TOUS les matchs MLB du jour depuis MLB.com (indépendant de Loto-Québec) ──
    carousel_list = []
    seen_carousel = set()

    try:
        import statsapi
        from scraper import _normalize_team_name_mlb
        mlb_games = statsapi.schedule(sportId=1, date=today_mtl)
        for g in mlb_games:
            away_raw = g.get("away_name", "")
            home_raw = g.get("home_name", "")
            away = _normalize_team_name_mlb(away_raw)
            home = _normalize_team_name_mlb(home_raw)
            ck = (home, away)
            if ck in seen_carousel:
                continue
            seen_carousel.add(ck)

            # Scores & état
            hs = g.get("home_score") or 0
            as_ = g.get("away_score") or 0
            status = g.get("status", "")
            inning = g.get("current_inning", "")
            inning_state = g.get("inning_state", "")
            start_utc = g.get("game_datetime", "")

            if status in ("Final", "Game Over", "Completed Early"):
                live_status = "Final"
                current_inning = "Final"
            elif status in ("In Progress", "Manager challenge", "Critical"):
                live_status = "Live"
                half = "Haut" if inning_state in ("Top", "Mid") else "Bas"
                current_inning = f"{half} {inning}" if inning else "En cours"
            elif status in ("Postponed", "Cancelled", "Suspended"):
                live_status = "Postponed"
                current_inning = "Reporté"
            else:
                live_status = "Preview"
                current_inning = ""

            # Heure locale Montréal
            time_str = ""
            try:
                dt = datetime.fromisoformat(start_utc.replace("Z", "+00:00"))
                local = dt.replace(tzinfo=None) - _td(hours=4)
                time_str = local.strftime("%H:%M")
            except Exception:
                pass

            # Vérifier si ce match a des cotes analysées (pour garder les infos)
            live_data = live_by_key.get(ck, {})
            carousel_list.append({
                "match":           f"{away} @ {home}",
                "away_team":       away,
                "home_team":       home,
                "date":            today_mtl,
                "time":            time_str,
                "odds":            0,
                "recommendation":  "—",
                "event_url":       "",
                "live_status":     live_status,
                "detailed_status": status,
                "away_score":      as_,
                "home_score":      hs,
                "current_inning":  current_inning,
            })
    except Exception as e:
        print(f"  [carousel] Erreur statsapi: {e}")
        # Fallback : utiliser les matchs du scraper
        for m in matches:
            ck = (m.home_team, m.away_team)
            if ck in seen_carousel:
                continue
            seen_carousel.add(ck)
            carousel_list.append({
                "match":           f"{m.away_team} @ {m.home_team}",
                "away_team":       m.away_team,
                "home_team":       m.home_team,
                "date":            m.date,
                "time":            m.time,
                "odds":            0,
                "recommendation":  "—",
                "event_url":       m.event_url,
                "live_status":     getattr(m, 'live_status', ''),
                "detailed_status": getattr(m, 'detailed_status', ''),
                "away_score":      getattr(m, 'away_score', 0),
                "home_score":      getattr(m, 'home_score', 0),
                "current_inning":  getattr(m, 'current_inning', ''),
            })

    # Trier : matchs en cours d'abord, puis par heure
    # Status priority: Live=0, Preview=1, Final=2, Postponed=3
    def sort_key(m):
        status = m.get("live_status", "").lower()
        status_order = {"live": 0, "preview": 1, "final": 2, "postponed": 3}
        status_priority = status_order.get(status, 99)
        time_str = m.get("time") or "99:99"
        return (status_priority, time_str)

    carousel_list.sort(key=sort_key)

    return {
        "opportunities":      opp_list,
        "all_predictions":    opp_list + info_list,  # tous les picks : paris + prédictions info-only
        "carousel_matches":   carousel_list,
        "combos":             combos,
        "total_matches":      len(matches),
        "total_picks":        len(opp_list),
        "n_excellent":        n_excellent,
        "n_bon":              n_bon,
        "avg_margin":         round(avg_margin, 2),
        "bankroll":           max_nightly,  # budget soir = bankroll effectif
        "kelly_fraction":     kelly_frac,
        "max_nightly":        max_nightly,
        "total_kelly_wagered": round(total_kelly, 2),
        "raw_kelly_total":    round(total_kelly, 2),
        "kelly_confidence":   round(confidence, 2),
        "low_value_night":    low_value,
        "timestamp":          _now_mtl().strftime("%H:%M:%S"),
        "date":               _today_mtl(),
        "mode":               mode,
    }


# ─── Authentication Routes ────────────────────────────────────────────────────

@app.route("/login", methods=["GET", "POST"])
def login():
    """Page de login"""
    if current_user.is_authenticated:
        return redirect(url_for('index'))

    if request.method == "POST":
        username = request.form.get("username", "").strip()
        password = request.form.get("password", "").strip()

        # Vérifier les credentials
        if username == _LOGIN_USERNAME and password == _LOGIN_PASSWORD:
            user = User(username)
            login_user(user)
            return redirect(url_for('index'))
        else:
            return render_template("login.html", error="Identifiants invalides")

    return render_template("login.html")


@app.route("/logout")
@login_required
def logout():
    """Logout et rediriger vers login"""
    logout_user()
    return redirect(url_for('login'))


@app.route("/api/change-password", methods=["POST"])
@login_required
def api_change_password():
    """Changer le mot de passe de l'utilisateur"""
    global _LOGIN_PASSWORD

    data = request.get_json() or {}
    old_password = data.get("old_password", "").strip()
    new_password = data.get("new_password", "").strip()

    if not old_password or not new_password:
        return jsonify({"error": "Les deux mots de passe sont obligatoires"}), 400

    # Vérifier l'ancien mot de passe
    if old_password != _LOGIN_PASSWORD:
        return jsonify({"error": "Ancien mot de passe incorrect"}), 401

    # Mots de passe identiques
    if old_password == new_password:
        return jsonify({"error": "Le nouveau mot de passe doit être différent"}), 400

    # Mettre à jour le mot de passe en mémoire (note: ne persiste que pour la session)
    # Pour persister, il faudrait l'écrire dans un fichier ou une DB
    _LOGIN_PASSWORD = new_password

    return jsonify({"success": True, "message": "Mot de passe changé avec succès"})


# ─── Upload Snapshots (pour initialiser les stats en prod) ────────────────────

@app.route('/api/upload-snapshots', methods=['POST'])
@login_required
def api_upload_snapshots():
    """Upload un fichier tar.gz contenant les snapshots et l'extrait dans /data/snapshots/"""
    import tarfile

    if 'file' not in request.files:
        return jsonify({"error": "Aucun fichier fourni"}), 400

    file = request.files['file']
    if not file or not file.filename.endswith('.tar.gz'):
        return jsonify({"error": "Le fichier doit être un tar.gz"}), 400

    try:
        # Sauvegarder temporairement le fichier
        temp_path = os.path.join(_DATA_DIR, "snapshots-upload.tar.gz")
        file.save(temp_path)

        # Extraire dans /data/snapshots/
        os.makedirs(_SNAPSHOTS_DIR, exist_ok=True)
        with tarfile.open(temp_path, "r:gz") as tar:
            tar.extractall(path=_DATA_DIR)

        # Nettoyer
        os.remove(temp_path)

        # Compter les fichiers importés
        snap_count = len([f for f in os.listdir(_SNAPSHOTS_DIR) if f.endswith(".json")])

        print(f"  [upload-snapshots] {snap_count} snapshots importés depuis {file.filename}")
        return jsonify({
            "ok": True,
            "message": f"{snap_count} snapshots importés avec succès",
            "snapshots_count": snap_count
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


# ─── Lancement ────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5003))
    print(f"\n  MLB Analyzer — http://localhost:{port}\n")

    # Auto-résoudre les paris passés au démarrage (pour calibration)
    try:
        from predictions import update_outcomes
        update_outcomes(days_back=7)
    except Exception as e:
        print(f"  [startup] Erreur update_outcomes: {e}")

    # Pré-chauffer le cache stats MLB (silencieux, non-bloquant)
    def _prewarm_stats():
        try:
            from mlb_stats import _ensure_standings, _fetch_todays_pitchers
            _ensure_standings()
            _fetch_todays_pitchers()
            print("  [startup] Cache stats MLB pre-charge OK")
        except Exception as e:
            print(f"  [startup] Prewarm stats: {e}")

    # Pré-lancer l'analyse complète en arrière-plan
    def _prewarm_analysis():
        import time
        time.sleep(1)   # laisser Flask démarrer
        try:
            print("  [startup] Pré-analyse MLB en cours...")
            _start_analysis_thread(
                bankroll=DEFAULT_BANKROLL,
                kelly_frac=DEFAULT_KELLY_FRAC,
                max_nightly=DEFAULT_MAX_NIGHTLY,
                top_n=50,
            )
        except Exception as e:
            print(f"  [startup] Erreur pré-analyse: {e}")

    threading.Thread(target=_prewarm_stats,    daemon=True).start()
    threading.Thread(target=_prewarm_analysis, daemon=True).start()

    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
