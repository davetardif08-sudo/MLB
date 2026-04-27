"""
Suivi des prédictions MLB pour calibration historique.

Flux :
  1. record_opportunity()  — sauvegarde chaque paris recommandé
  2. update_outcomes()     — récupère les résultats via mlb-statsapi
  3. compute_calibration() — compare prédit vs réel
  4. get_feature_weights() — retourne les poids optimaux appris

Adapté de miseojeu-analyzer/predictions.py pour la MLB.
"""

import json
import time
from datetime import date, datetime, timedelta
from pathlib import Path

PREDICTIONS_FILE = Path(__file__).parent / "predictions.json"
MIN_OUTCOMES     = 20   # min résultats avant d'activer la correction
MIN_SIGNAL_SAMPLES = 8

# Poids par défaut MLB
_DEFAULT_WEIGHTS = {
    "stat_vs_math": 0.50,  # 50/50 MLB (moins de stats historiques au début)
    "intra_stat": {
        "win_pct":       0.35,
        "home_away":     0.15,
        "recent_form":   0.15,
        "runs_per_game": 0.10,
        "era":           0.10,
        "ops":           0.08,
        "whip":          0.07,
    },
}

_fw_cache: dict | None = None
_fw_cache_ts: float    = 0.0
_FW_TTL                = 300.0  # 5 min


# ─── Persistance ──────────────────────────────────────────────────────────────

def _load() -> list[dict]:
    if not PREDICTIONS_FILE.exists():
        return []
    try:
        with open(PREDICTIONS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def _save(records: list[dict]):
    try:
        with open(PREDICTIONS_FILE, "w", encoding="utf-8") as f:
            json.dump(records, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"[predictions] Erreur sauvegarde: {e}")


def _record_key(pick: dict) -> str:
    return (f"{pick.get('date','?')}|"
            f"{pick.get('home_team','?')}|"
            f"{pick.get('away_team','?')}|"
            f"{pick.get('bet_type','?')}|"
            f"{pick.get('selection','?')}")


# ─── Enregistrement ───────────────────────────────────────────────────────────

def record_opportunity(pick: dict):
    """
    Enregistre un paris recommandé dans le fichier de suivi.

    pick doit contenir :
      date, time, home_team, away_team, bet_type, selection,
      odds, fair_prob, value_score, recommendation, sport,
      math_prob (optionnel), kelly_fraction (optionnel)
    """
    records = _load()
    key = _record_key(pick)

    # Éviter les doublons
    for r in records:
        if _record_key(r) == key:
            return

    record = {
        "key":            key,
        "date":           pick.get("date", ""),
        "time":           pick.get("time", ""),
        "home_team":      pick.get("home_team", ""),
        "away_team":      pick.get("away_team", ""),
        "bet_type":       pick.get("bet_type", ""),
        "selection":      pick.get("selection", ""),
        "odds":           pick.get("odds", 0.0),
        "fair_prob":      pick.get("fair_prob", 0.0),
        "math_prob":      pick.get("math_prob", 0.0),
        "value_score":    pick.get("value_score", 0.0),
        "recommendation": pick.get("recommendation", ""),
        "sport":          pick.get("sport", "baseball"),
        "kelly_fraction":  pick.get("kelly_fraction", 0.0),
        "signals":         pick.get("signals", {}),
        "analysis_mode":   pick.get("analysis_mode", "standard"),
        "system_version":  pick.get("system_version", ""),
        "outcome":         None,    # "win" | "loss" | "push"
        "saved_at":        datetime.now().isoformat(),
    }
    records.append(record)
    _save(records)


# ─── Mise à jour des résultats ────────────────────────────────────────────────

def update_outcomes(days_back: int = 5):
    """
    Met à jour les résultats des paris passés via mlb-statsapi.
    Cherche les matchs des N derniers jours.
    """
    try:
        import statsapi
    except ImportError:
        print("[predictions] mlb-statsapi non disponible — résultats non mis à jour")
        return

    records = _load()
    changed = 0

    pending = [r for r in records
               if r.get("outcome") is None
               and r.get("date")
               and r.get("sport", "baseball") == "baseball"]

    if not pending:
        return

    # Grouper par date pour minimiser les appels API
    from collections import defaultdict
    by_date: dict[str, list] = defaultdict(list)
    for r in pending:
        by_date[r["date"]].append(r)

    today = date.today()
    cutoff = (today - timedelta(days=days_back)).isoformat()

    for game_date, date_records in by_date.items():
        if game_date > today.isoformat() or game_date < cutoff:
            continue

        try:
            schedule = statsapi.schedule(sportId=1, date=_reformat_date(game_date))
        except Exception as e:
            print(f"[predictions] Erreur schedule {game_date}: {e}")
            continue

        for record in date_records:
            outcome = _find_outcome(record, schedule)
            if outcome:
                record["outcome"] = outcome
                changed += 1

    if changed:
        _save(records)
        print(f"[predictions] {changed} résultats mis à jour")


def _reformat_date(iso_date: str) -> str:
    """Convertit YYYY-MM-DD en MM/DD/YYYY pour statsapi."""
    try:
        dt = datetime.strptime(iso_date, "%Y-%m-%d")
        return dt.strftime("%m/%d/%Y")
    except Exception:
        return iso_date


def _find_outcome(record: dict, schedule: list) -> str | None:
    """Tente de trouver le résultat d'un pari dans le schedule."""
    from mlb_stats import _find_team_id
    home = record.get("home_team", "")
    away = record.get("away_team", "")
    selection = record.get("selection", "").lower()
    bet_type   = record.get("bet_type", "").lower()

    home_tid = _find_team_id(home)
    away_tid = _find_team_id(away)

    for game in schedule:
        if game.get("status") not in ("Final", "Game Over", "Completed Early"):
            continue

        g_home = (game.get("home_name", "") or "").lower()
        g_away = (game.get("away_name", "") or "").lower()

        # Matching par team_id (robuste)
        g_home_id = game.get("home_id")
        g_away_id = game.get("away_id")
        if home_tid and away_tid:
            if not (g_home_id == home_tid and g_away_id == away_tid):
                continue
        else:
            # Fallback nom
            home_lower = home.lower()
            away_lower = away.lower()
            home_match = any(w in g_home for w in home_lower.split() if len(w) > 3)
            away_match = any(w in g_away for w in away_lower.split() if len(w) > 3)
            if not (home_match and away_match):
                continue

        home_score = int(game.get("home_score", 0) or 0)
        away_score = int(game.get("away_score", 0) or 0)

        # Paris sur le gagnant (moneyline)
        if any(k in bet_type for k in ("gagnant", "victoire", "moneyline", "2 issues")):
            if home_score == away_score:
                return "push"
            winner_side = "home" if home_score > away_score else "away"
            # Détecter si la sélection vise l'équipe locale ou visiteuse
            sel_is_home = any(w in selection for w in home.lower().split() if len(w) > 3)
            if not sel_is_home:
                sel_is_home = any(w in selection for w in g_home.split() if len(w) > 3)
            return "win" if (sel_is_home and winner_side == "home") or (not sel_is_home and winner_side == "away") else "loss"

        # Paris Total (over/under)
        if any(k in bet_type for k in ("total", "plus/moins")):
            import re
            m = re.search(r'(\d+\.?\d*)', record.get("selection", ""))
            if not m:
                m = re.search(r'(\d+\.?\d*)', record.get("bet_type", ""))
            if not m:
                continue
            line  = float(m.group(1))
            total = home_score + away_score
            is_over = "plus" in selection or "over" in selection or "au moins" in selection
            if total == line:
                return "push"
            actual_over = total > line
            return "win" if (is_over == actual_over) else "loss"

    return None


# ─── Calibration ──────────────────────────────────────────────────────────────

def compute_calibration(sport: str = "baseball") -> dict:
    """Analyse les prédictions passées et retourne les statistiques de calibration."""
    records = [r for r in _load()
               if r.get("outcome") and r.get("sport", "baseball") == sport]

    if len(records) < MIN_OUTCOMES:
        return {"status": "insufficient_data", "n": len(records), "min": MIN_OUTCOMES}

    wins   = sum(1 for r in records if r.get("outcome") == "win")
    losses = sum(1 for r in records if r.get("outcome") == "loss")
    pushes = sum(1 for r in records if r.get("outcome") == "push")

    win_rate = wins / (wins + losses) if (wins + losses) > 0 else 0.5

    avg_pred_prob  = sum(r.get("fair_prob", 0.5) for r in records) / len(records)
    avg_odds       = sum(r.get("odds", 2.0) for r in records) / len(records)

    roi = ((wins * (sum(r.get("odds", 2.0) - 1 for r in records if r.get("outcome") == "win"))
            - losses) / len(records)) * 100

    return {
        "status":         "ok",
        "n":              len(records),
        "wins":           wins,
        "losses":         losses,
        "pushes":         pushes,
        "win_rate":       round(win_rate, 4),
        "avg_pred_prob":  round(avg_pred_prob, 4),
        "avg_odds":       round(avg_odds, 4),
        "roi_pct":        round(roi, 2),
        "calibration_bias": round(win_rate - avg_pred_prob, 4),
    }


def get_feature_weights(sport: str = "baseball") -> dict:
    """Retourne les poids appris ou les poids par défaut."""
    global _fw_cache, _fw_cache_ts

    now = time.time()
    if _fw_cache and now - _fw_cache_ts < _FW_TTL:
        return _fw_cache

    cal = compute_calibration(sport)

    if cal.get("status") != "ok":
        return _DEFAULT_WEIGHTS

    # Ajustement simple : si win_rate > pred_prob → augmenter stat_vs_math
    bias = cal.get("calibration_bias", 0.0)
    new_weights = {
        "stat_vs_math": max(0.30, min(0.70,
            _DEFAULT_WEIGHTS["stat_vs_math"] + bias * 0.5)),
        "intra_stat": _DEFAULT_WEIGHTS["intra_stat"].copy(),
    }

    _fw_cache    = new_weights
    _fw_cache_ts = now
    return new_weights


def classify_bet_type(bet_type: str, home: str = "", away: str = "") -> str:
    """Classifie un type de pari en catégorie standard."""
    bt_l = bet_type.lower()
    if any(k in bt_l for k in ("gagnant", "victoire", "moneyline", "2 issues")):
        return "moneyline"
    if any(k in bt_l for k in ("total", "plus/moins", "over", "under")):
        return "total"
    if any(k in bt_l for k in ("handicap", "écart", "run line")):
        return "runline"
    return "autre"


def get_bet_type_multipliers(sport: str = "baseball") -> dict:
    """Retourne les multiplicateurs de valeur par type de pari."""
    records = [r for r in _load()
               if r.get("outcome") and r.get("sport", "baseball") == sport]

    multipliers: dict = {}
    by_type: dict = {}

    for r in records:
        cat = classify_bet_type(r.get("bet_type", ""))
        if cat not in by_type:
            by_type[cat] = {"wins": 0, "total": 0}
        by_type[cat]["total"] += 1
        if r.get("outcome") == "win":
            by_type[cat]["wins"] += 1

    for cat, data in by_type.items():
        if data["total"] >= MIN_SIGNAL_SAMPLES:
            win_rate  = data["wins"] / data["total"]
            baseline  = 0.50
            multipliers[cat] = round(1.0 + (win_rate - baseline) * 2, 3)

    return multipliers


def get_history(n: int = 50, sport: str = "baseball") -> list[dict]:
    """Retourne les N derniers paris enregistrés."""
    records = [r for r in _load()
               if r.get("sport", "baseball") == sport]
    return sorted(records, key=lambda x: x.get("saved_at", ""), reverse=True)[:n]


def get_stats(sport: str = "baseball") -> dict:
    """Retourne un résumé des statistiques de paris."""
    return compute_calibration(sport)
