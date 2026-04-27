"""
Stats avancées Statcast via pybaseball.

Données récupérées :
  - Exit velocity moyen, hard hit %, barrel %
  - xBA, xSLG, xwOBA par équipe
  - Stats offensives FanGraphs (wOBA, wRC+, ISO)

Cache : 12 heures (données Statcast ne sont pas temps réel)

Note : pybaseball peut être lent au premier chargement (télécharge des CSV).
"""

import json
import time
from pathlib import Path
from typing import Optional

try:
    import pybaseball
    from pybaseball import (
        team_batting,
        team_pitching,
        batting_stats,
        pitching_stats,
    )
    pybaseball.cache.enable()
    _PYBASEBALL_AVAILABLE = True
except ImportError:
    _PYBASEBALL_AVAILABLE = False
    print("[statcast] pybaseball non installé — stats Statcast désactivées")


_CACHE_FILE = Path(__file__).parent / ".statcast_cache.json"
_CACHE_TTL  = 12 * 3600  # 12 heures

_statcast_cache: dict = {}
_cache_loaded         = False


def _load_cache():
    global _statcast_cache, _cache_loaded
    if _cache_loaded:
        return
    if not _CACHE_FILE.exists():
        _cache_loaded = True
        return
    try:
        with open(_CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if time.time() - data.get("_ts", 0) < _CACHE_TTL:
            _statcast_cache = data.get("teams", {})
    except Exception:
        pass
    _cache_loaded = True


def _save_cache():
    try:
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "_ts":   time.time(),
                "teams": _statcast_cache,
            }, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# --- Mapping noms d'équipes FanGraphs ----------------------------------------
# FanGraphs utilise des abréviations différentes de mlb-statsapi

FANGRAPHS_TEAM_MAP = {
    "yankees": "NYY", "new york yankees": "NYY",
    "red sox": "BOS", "boston":           "BOS",
    "blue jays": "TOR", "toronto":        "TOR",
    "orioles": "BAL", "baltimore":        "BAL",
    "rays":    "TBR", "tampa bay":        "TBR",
    "white sox": "CHW", "chicago white sox": "CHW",
    "guardians": "CLE", "cleveland":      "CLE",
    "tigers":  "DET", "detroit":          "DET",
    "royals":  "KCR", "kansas city":      "KCR",
    "twins":   "MIN", "minnesota":        "MIN",
    "astros":  "HOU", "houston":          "HOU",
    "athletics": "OAK", "oakland":        "OAK",
    "mariners": "SEA", "seattle":         "SEA",
    "angels":  "LAA", "los angeles angels": "LAA",
    "rangers": "TEX", "texas":            "TEX",
    "braves":  "ATL", "atlanta":          "ATL",
    "marlins": "MIA", "miami":            "MIA",
    "mets":    "NYM", "new york mets":    "NYM",
    "phillies": "PHI", "philadelphia":    "PHI",
    "nationals": "WSN", "washington":     "WSN",
    "cubs":    "CHC", "chicago cubs":     "CHC",
    "reds":    "CIN", "cincinnati":       "CIN",
    "brewers": "MIL", "milwaukee":        "MIL",
    "pirates": "PIT", "pittsburgh":       "PIT",
    "cardinals": "STL", "st. louis":      "STL",
    "diamondbacks": "ARI", "arizona":     "ARI",
    "rockies": "COL", "colorado":         "COL",
    "dodgers": "LAD", "los angeles dodgers": "LAD",
    "padres":  "SDP", "san diego":        "SDP",
    "giants":  "SFG", "san francisco":    "SFG",
}


def _find_fangraphs_abbr(name: str) -> Optional[str]:
    name_lower = name.lower().strip()
    if name_lower in FANGRAPHS_TEAM_MAP:
        return FANGRAPHS_TEAM_MAP[name_lower]
    for key, abbr in FANGRAPHS_TEAM_MAP.items():
        if key in name_lower or name_lower in key:
            return abbr
    return None


# --- Chargement des stats Statcast -------------------------------------------

def _load_season_stats():
    """Charge les stats offensives et de lancer de la saison en cours."""
    _load_cache()
    if _statcast_cache:
        return

    if not _PYBASEBALL_AVAILABLE:
        return

    from datetime import date
    current_year = date.today().year
    prev_year    = current_year - 1

    print(f"  [statcast] Chargement stats FanGraphs {prev_year} (premier chargement)...")

    try:
        # Stats offensives par équipe (saison précédente pour fiabilité)
        bat = team_batting(prev_year)
        pit = team_pitching(prev_year)

        # Convertir en dict par équipe
        if bat is not None and not bat.empty:
            for _, row in bat.iterrows():
                team_abbr = str(row.get('Team', '')).upper()
                if not team_abbr or team_abbr == 'NAN':
                    continue
                _statcast_cache[team_abbr] = _statcast_cache.get(team_abbr, {})
                _statcast_cache[team_abbr].update({
                    "woba":  round(float(row.get('wOBA', 0.320) or 0.320), 3),
                    "wrc_plus": int(row.get('wRC+', 100) or 100),
                    "iso":   round(float(row.get('ISO', 0.160) or 0.160), 3),
                    "ops":   round(float(row.get('OPS', 0.720) or 0.720), 3),
                    "bb_pct": round(float(row.get('BB%', 8.5) or 8.5), 1),
                    "k_pct":  round(float(row.get('K%', 22.0) or 22.0), 1),
                    "hard_hit_pct": round(float(row.get('Hard%', 36.0) or 36.0), 1),
                })

        if pit is not None and not pit.empty:
            for _, row in pit.iterrows():
                team_abbr = str(row.get('Team', '')).upper()
                if not team_abbr or team_abbr == 'NAN':
                    continue
                _statcast_cache[team_abbr] = _statcast_cache.get(team_abbr, {})
                _statcast_cache[team_abbr].update({
                    "fip":     round(float(row.get('FIP', 4.20) or 4.20), 2),
                    "xfip":    round(float(row.get('xFIP', 4.20) or 4.20), 2),
                    "era_pit": round(float(row.get('ERA', 4.20) or 4.20), 2),
                    "k9_team": round(float(row.get('K/9', 8.5) or 8.5), 2),
                    "bb9_team": round(float(row.get('BB/9', 3.2) or 3.2), 2),
                    "hr9_team": round(float(row.get('HR/9', 1.15) or 1.15), 2),
                })

        _save_cache()
        print(f"  [statcast] {len(_statcast_cache)} équipes chargées")

    except Exception as e:
        print(f"  [statcast] Erreur chargement FanGraphs: {e}")


def get_team_statcast(team_name: str) -> dict:
    """Retourne les stats Statcast/FanGraphs d'une équipe."""
    _load_season_stats()
    abbr = _find_fangraphs_abbr(team_name)
    if abbr and abbr in _statcast_cache:
        return _statcast_cache[abbr]
    # Chercher aussi par nom partiel dans le cache
    team_lower = team_name.lower()
    for key, data in _statcast_cache.items():
        if team_lower in key.lower() or key.lower() in team_lower:
            return data
    return {}


def statcast_edge(home_team: str, away_team: str) -> dict:
    """
    Calcule les avantages Statcast entre les deux équipes.
    Retourne un dict avec des scores d'avantage pour chaque dimension.
    """
    home = get_team_statcast(home_team)
    away = get_team_statcast(away_team)

    if not home or not away:
        return {"offense_edge": 0.0, "pitching_edge": 0.0, "combined_edge": 0.0}

    # Avantage offensif (wOBA : plus haut = mieux)
    home_off = home.get("woba", 0.320)
    away_off = away.get("woba", 0.320)
    off_range = 0.040  # écart typique entre bonne/mauvaise équipe
    offense_edge = (home_off - away_off) / off_range if off_range else 0.0
    offense_edge = max(-1.0, min(1.0, offense_edge))

    # Avantage pitching (xFIP : plus bas = mieux pour l'équipe)
    home_fip = home.get("xfip", 4.20)
    away_fip = away.get("xfip", 4.20)
    pit_range = 0.80
    pitching_edge = (away_fip - home_fip) / pit_range if pit_range else 0.0
    pitching_edge = max(-1.0, min(1.0, pitching_edge))

    combined = (offense_edge * 0.5 + pitching_edge * 0.5)

    return {
        "offense_edge":  round(offense_edge, 3),
        "pitching_edge": round(pitching_edge, 3),
        "combined_edge": round(combined, 3),
        "home_woba":     round(home_off, 3),
        "away_woba":     round(away_off, 3),
        "home_xfip":     round(home_fip, 2),
        "away_xfip":     round(away_fip, 2),
    }
