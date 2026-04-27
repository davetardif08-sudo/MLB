"""
Stats d'équipes MLB via l'API officielle MLB (mlb-statsapi).

Données récupérées :
  - Classements (standings) : W%, run differential, record dom/route
  - Statistiques d'équipe : runs/match, ERA équipe, OPS, WHIP
  - Lanceur partant prévu (ERA, WHIP individuels)
  - Bullpen : ERA équipe moins contribution du partant
  - Facteurs parc (Coors Field, etc.)
  - Forme récente (10 derniers matchs)

Cache : 4 heures
"""

import json
import math
import time
import re
from datetime import datetime, date
from pathlib import Path
from typing import Optional

try:
    import statsapi
    _STATSAPI_AVAILABLE = True
except ImportError:
    _STATSAPI_AVAILABLE = False
    print("[mlb_stats] mlb-statsapi non installé — stats désactivées")


# --- Cache JSON et poids adaptatifs ------------------------------------------

_CACHE_FILE   = Path(__file__).parent / ".mlb_stats_cache.json"
_WEIGHTS_FILE = Path(__file__).parent / "weights.json"
_CACHE_TTL    = 4 * 3600  # 4 heures

# Poids par défaut (somme = 1.0) — 11 facteurs
# ERA → FIP (Fielding Independent Pitching), OPS → wOBA, win_pct → pyth_wpct
DEFAULT_WEIGHTS = {
    "pyth_wpct":     0.18,   # Pythagorean W% (remplace win_pct)
    "home_away":     0.07,
    "recent_form":   0.10,
    "runs_per_game": 0.08,
    "fip":           0.06,   # FIP équipe (remplace ERA)
    "woba":          0.06,   # wOBA (remplace OPS)
    "whip":          0.04,
    "starter_fip":   0.18,   # FIP du lanceur partant (remplace starter_era)
    "starter_whip":  0.07,
    "bullpen_era":   0.07,
    "run_diff":      0.09,
}

# Constantes sabermétriques
_FIP_CONSTANT  = 3.17   # MLB 2024 average
_LEAGUE_FIP    = 4.20
_LEAGUE_WOBA   = 0.320
_LEAGUE_WPCT   = 0.500
_REGRESS_IP    = 60.0   # IP minimums pour faire confiance au FIP individuel
_REGRESS_PA    = 150.0  # PA minimums pour faire confiance au wOBA


def _compute_fip(so: float, bb: float, hr: float, ip: float) -> float:
    """FIP = (13×HR + 3×BB - 2×SO) / IP + constante"""
    if ip <= 0:
        return _LEAGUE_FIP
    raw = (13 * hr + 3 * bb - 2 * so) / ip + _FIP_CONSTANT
    return round(max(1.50, min(9.0, raw)), 2)


def _compute_woba(bb: float, hbp: float, singles: float, doubles: float,
                  triples: float, hr: float, pa: float) -> float:
    """wOBA avec pondérations linéaires 2024"""
    if pa <= 0:
        return _LEAGUE_WOBA
    num = (0.69*bb + 0.72*hbp + 0.89*singles + 1.27*doubles +
           1.62*triples + 2.10*hr)
    return round(max(0.200, min(0.500, num / pa)), 3)


def _pyth_wpct(runs_scored: float, runs_allowed: float) -> float:
    """Pythagorean Win% : RS^1.83 / (RS^1.83 + RA^1.83)"""
    if runs_scored <= 0 and runs_allowed <= 0:
        return _LEAGUE_WPCT
    rs = max(0.001, runs_scored) ** 1.83
    ra = max(0.001, runs_allowed) ** 1.83
    return round(rs / (rs + ra), 4)


def _regress(value: float, sample: float, full_sample: float,
             league_avg: float) -> float:
    """Régression vers la moyenne selon taille d'échantillon."""
    w = min(1.0, sample / full_sample)
    return w * value + (1 - w) * league_avg


def load_weights() -> dict:
    """Charge les poids depuis weights.json; retourne DEFAULT_WEIGHTS si absent."""
    try:
        if _WEIGHTS_FILE.exists():
            with open(_WEIGHTS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            w = data.get("weights", {})
            if w and abs(sum(w.values()) - 1.0) < 0.05:
                merged = DEFAULT_WEIGHTS.copy()
                merged.update({k: v for k, v in w.items() if k in DEFAULT_WEIGHTS})
                total = sum(merged.values())
                return {k: round(v / total, 4) for k, v in merged.items()}
    except Exception:
        pass
    return DEFAULT_WEIGHTS.copy()


def save_weights(weights: dict):
    """Sauvegarde les poids (normalisés) dans weights.json (préserve stat_vs_math)."""
    try:
        total = sum(weights.values())
        if total <= 0:
            return
        norm = {k: round(v / total, 4) for k, v in weights.items()}
        # Préserver stat_vs_math existant
        existing = {}
        if _WEIGHTS_FILE.exists():
            with open(_WEIGHTS_FILE, encoding="utf-8") as f:
                existing = json.load(f)
        data = {
            "weights":    norm,
            "updated_at": datetime.now().isoformat(),
        }
        if "stat_vs_math" in existing:
            data["stat_vs_math"] = existing["stat_vs_math"]
        if "intra_stat" in existing:
            data["intra_stat"] = existing["intra_stat"]
        with open(_WEIGHTS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"  [mlb_stats] Erreur save_weights: {e}")


def load_stat_vs_math() -> float:
    """Charge stat_vs_math depuis weights.json (valeur calibrée automatiquement)."""
    try:
        if _WEIGHTS_FILE.exists():
            with open(_WEIGHTS_FILE, encoding="utf-8") as f:
                data = json.load(f)
            return float(data.get("stat_vs_math", 0.50))
    except Exception:
        pass
    return 0.50


def save_stat_vs_math(value: float) -> float:
    """
    Sauvegarde stat_vs_math dans weights.json (préserve les autres clés).
    Retourne la valeur réellement sauvegardée (après clamp).
    """
    value = round(max(0.20, min(0.65, value)), 3)
    try:
        data = {}
        if _WEIGHTS_FILE.exists():
            with open(_WEIGHTS_FILE, encoding="utf-8") as f:
                data = json.load(f)
        data["stat_vs_math"]            = value
        data["stat_vs_math_updated_at"] = datetime.now().isoformat()
        with open(_WEIGHTS_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"  [mlb_stats] stat_vs_math sauvegardé: {value}")
    except Exception as e:
        print(f"  [mlb_stats] Erreur save_stat_vs_math: {e}")
    return value


def _load_cache() -> dict:
    if not _CACHE_FILE.exists():
        return {}
    try:
        with open(_CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if time.time() - data.get("_ts", 0) < _CACHE_TTL:
            return data
    except Exception:
        pass
    return {}


def _save_cache(data: dict):
    try:
        data["_ts"] = time.time()
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


# --- Mapping équipes MLB ------------------------------------------------------

MLB_TEAM_IDS = {
    # AL East
    "yankees":       147, "new york yankees": 147,
    "red sox":       111, "boston":           111,
    "blue jays":     141, "toronto":          141,
    "orioles":       110, "baltimore":        110,
    "rays":          139, "tampa bay":        139, "tampa":         139,
    # AL Central
    "white sox":     145, "chicago white sox": 145,
    "guardians":     114, "cleveland":         114,
    "tigers":        116, "detroit":           116,
    "royals":        118, "kansas city":       118,
    "twins":         142, "minnesota":         142,
    # AL West
    "astros":        117, "houston":           117,
    "athletics":     133, "oakland":           133,
    "mariners":      136, "seattle":           136,
    "angels":        108, "los angeles angels": 108,
    "rangers":       140, "texas":             140,
    # NL East
    "braves":        144, "atlanta":           144,
    "marlins":       146, "miami":             146,
    "mets":          121, "new york mets":     121,
    "phillies":      143, "philadelphia":      143, "philadelphie": 143,
    "nationals":     120, "washington":        120,
    # NL Central
    "cubs":          112, "chicago cubs":      112,
    "reds":          113, "cincinnati":        113,
    "brewers":       158, "milwaukee":         158,
    "pirates":       134, "pittsburgh":        134,
    "cardinals":     138, "st. louis":         138, "saint-louis": 138,
    # NL West
    "diamondbacks":  109, "arizona":           109, "d-backs": 109,
    "rockies":       115, "colorado":          115,
    "dodgers":       119, "los angeles dodgers": 119,
    "padres":        135, "san diego":         135,
    "giants":        137, "san francisco":     137,
}


def _find_team_id(name: str) -> Optional[int]:
    """Trouve l'ID d'équipe MLB depuis un nom partiel."""
    if not name:
        return None
    name_lower = name.lower().strip()
    # Enlever parenthèses MO-J : "Tampa Bay (Rays)" → "tampa bay rays"
    name_clean = re.sub(r'\s*\(([^)]+)\)', r' \1', name_lower).strip()
    for candidate in (name_lower, name_clean):
        if candidate in MLB_TEAM_IDS:
            return MLB_TEAM_IDS[candidate]
    for key, tid in MLB_TEAM_IDS.items():
        if key in name_clean or name_clean in key:
            return tid
    words = name_clean.split()
    for word in words:
        if len(word) > 3 and word in MLB_TEAM_IDS:
            return MLB_TEAM_IDS[word]
    return None


# --- Facteurs de parc (Park Factors) ------------------------------------------
# Run factor : 1.0 = neutre, >1.0 = hitter-friendly, <1.0 = pitcher-friendly
# Basé sur données historiques, mis à jour annuellement

PARK_FACTORS = {
    115: 1.38,  # Colorado Rockies — Coors Field (altitude 1600m)
    113: 1.10,  # Cincinnati Reds — Great American Ball Park
    143: 1.08,  # Philadelphia Phillies — Citizens Bank Park
    111: 1.07,  # Boston Red Sox — Fenway Park (Green Monster)
    140: 1.05,  # Texas Rangers — Globe Life Field
    112: 1.04,  # Chicago Cubs — Wrigley Field
    144: 1.03,  # Atlanta Braves — Truist Park
    158: 1.02,  # Milwaukee Brewers — American Family Field
    147: 1.01,  # New York Yankees — Yankee Stadium
    # Neutres (~1.0) : la plupart des parcs
    # Pitcher-friendly
    119: 0.96,  # Los Angeles Dodgers — Dodger Stadium
    136: 0.95,  # Seattle Mariners — T-Mobile Park
    139: 0.94,  # Tampa Bay Rays — Tropicana Field
    121: 0.95,  # New York Mets — Citi Field
    137: 0.93,  # San Francisco Giants — Oracle Park (vent, marine layer)
    135: 0.94,  # San Diego Padres — Petco Park
    133: 1.00,  # Athletics — Sutter Health Park, Sacramento (depuis 2025, neutre)
    146: 0.95,  # Miami Marlins — loanDepot park (humidité)
}


def _park_factor(home_team_id: int) -> float:
    """Retourne le park factor pour le stade de l'équipe locale."""
    return PARK_FACTORS.get(home_team_id, 1.0)


# --- Données d'équipe --------------------------------------------------------

_team_stats_cache: dict = {}
_standings_loaded = False


def _ensure_standings():
    """Charge les classements + stats d'équipe dans le cache."""
    global _standings_loaded, _team_stats_cache
    if _standings_loaded:
        return

    disk = _load_cache()
    if disk and "teams" in disk:
        _team_stats_cache = disk.get("teams", {})
        _standings_loaded = True
        return

    if not _STATSAPI_AVAILABLE:
        return

    try:
        print("  [mlb_stats] Chargement des classements MLB...")
        current_year = date.today().year
        standings_al = statsapi.standings_data(leagueId='103')
        standings_nl = statsapi.standings_data(leagueId='104')

        for division_data in list(standings_al.values()) + list(standings_nl.values()):
            for team in division_data.get("teams", []):
                tid = team.get("team_id")
                if not tid:
                    continue

                wins   = int(team.get("w", 0))
                losses = int(team.get("l", 0))
                total  = wins + losses
                wpct   = wins / total if total > 0 else 0.5

                # Stats d'équipe
                runs_per_game = 4.5
                runs_allowed_pg = 4.5
                fip  = _LEAGUE_FIP
                woba = _LEAGUE_WOBA
                whip = 1.30
                pa_total = 0

                try:
                    h = statsapi.get('team_stats', {
                        'teamId': tid, 'stats': 'season',
                        'group': 'hitting', 'sportId': 1, 'season': current_year,
                    })
                    for s in h.get('stats', []):
                        splits = s.get('splits', [])
                        if splits:
                            st = splits[0].get('stat', {})
                            # Runs per game
                            rpg = st.get('runsPerGame')
                            if rpg is not None:
                                runs_per_game = float(rpg)
                            else:
                                r  = float(st.get('runs', 0))
                                gp = float(st.get('gamesPlayed', 1))
                                if gp > 0:
                                    runs_per_game = round(r / gp, 2)
                            # wOBA
                            pa   = float(st.get('plateAppearances', 0))
                            bb   = float(st.get('baseOnBalls', 0))
                            hbp  = float(st.get('hitByPitch', 0))
                            h_   = float(st.get('hits', 0))
                            d_   = float(st.get('doubles', 0))
                            t_   = float(st.get('triples', 0))
                            hr_  = float(st.get('homeRuns', 0))
                            singles = h_ - d_ - t_ - hr_
                            if pa > 0:
                                raw_woba = _compute_woba(bb, hbp, singles, d_, t_, hr_, pa)
                                woba = _regress(raw_woba, pa, _REGRESS_PA, _LEAGUE_WOBA)
                            pa_total = pa
                except Exception as e:
                    print(f"    [hitting err tid={tid}]: {e}")

                try:
                    p = statsapi.get('team_stats', {
                        'teamId': tid, 'stats': 'season',
                        'group': 'pitching', 'sportId': 1, 'season': current_year,
                    })
                    for s in p.get('stats', []):
                        splits = s.get('splits', [])
                        if splits:
                            st   = splits[0].get('stat', {})
                            whip = float(st.get('whip', 1.30))
                            # FIP
                            so_p = float(st.get('strikeOuts', 0))
                            bb_p = float(st.get('baseOnBalls', 0))
                            hr_p = float(st.get('homeRuns', 0))
                            ip_p = float(st.get('inningsPitched', 0) or 0)
                            # Runs allowed per game (pour Pythagorean)
                            ra   = float(st.get('runs', 0))
                            gp   = float(st.get('gamesPlayed', total or 1))
                            runs_allowed_pg = ra / gp if gp > 0 else 4.5
                            if ip_p > 0:
                                raw_fip = _compute_fip(so_p, bb_p, hr_p, ip_p)
                                fip = _regress(raw_fip, ip_p, _REGRESS_IP, _LEAGUE_FIP)
                except Exception as e:
                    print(f"    [pitching err tid={tid}]: {e}")

                # Pythagorean W% (plus prédictif que le vrai W%)
                pyth = _pyth_wpct(runs_per_game, runs_allowed_pg)
                pyth = _regress(pyth, total, 30, _LEAGUE_WPCT)  # 30 matchs pour pleine confiance

                _team_stats_cache[str(tid)] = {
                    "team_id":        tid,
                    "name":           team.get("name", ""),
                    "wins":           wins,
                    "losses":         losses,
                    "win_pct":        round(wpct, 4),         # gardé pour référence
                    "pyth_wpct":      round(pyth, 4),         # NEW: Pythagorean W%
                    "home_wins":      int(team.get("home_wins", 0)),
                    "home_losses":    int(team.get("home_losses", 0)),
                    "away_wins":      int(team.get("away_wins", 0)),
                    "away_losses":    int(team.get("away_losses", 0)),
                    "run_diff":       int(team.get("run_differential", 0)),
                    "last10_wins":    int(team.get("last_ten_wins", 0)),
                    "last10_losses":  int(team.get("last_ten_losses", 0)),
                    "streak":         team.get("streak", ""),
                    "runs_per_game":  round(runs_per_game, 2),
                    "runs_allowed_pg": round(runs_allowed_pg, 2),
                    "fip":            round(fip, 2),           # NEW: FIP équipe
                    "woba":           round(woba, 3),          # NEW: wOBA
                    "whip":           round(whip, 2),
                }

        _standings_loaded = True
        _save_cache({"teams": _team_stats_cache})
        print(f"  [mlb_stats] {len(_team_stats_cache)} équipes chargées (avec stats {current_year})")

    except Exception as e:
        print(f"  [mlb_stats] Erreur standings: {e}")


# --- Lanceurs partants -------------------------------------------------------

_pitcher_cache: dict = {}    # str(team_id) → {era, whip, ip, name}
_pitcher_cache_ts: float = 0


def _fetch_todays_pitchers():
    """Récupère les stats des lanceurs partants prévus aujourd'hui (en parallèle)."""
    global _pitcher_cache, _pitcher_cache_ts
    if time.time() - _pitcher_cache_ts < 3600:
        return
    _pitcher_cache_ts = time.time()

    if not _STATSAPI_AVAILABLE:
        return
    try:
        from concurrent.futures import ThreadPoolExecutor, as_completed

        today    = date.today().strftime('%m/%d/%Y')
        schedule = statsapi.schedule(sportId=1, date=today)

        # Construire la liste (pitcher_name, team_name) à fetcher
        tasks = []
        for game in schedule:
            for side in ('home', 'away'):
                pname = game.get(f'{side}_probable_pitcher', '')
                tname = game.get(f'{side}_name', '')
                if pname and tname:
                    tasks.append((pname, tname))

        def _fetch_one(pitcher_name, team_name_):
            try:
                players = statsapi.lookup_player(pitcher_name)
                if not players:
                    return None
                pid   = players[0]['id']
                pdata = statsapi.player_stat_data(pid, group='pitching',
                                                  type='season', sportId=1)
                p_fip = p_whip = None
                p_ip  = 0.0
                for s in pdata.get('stats', []):
                    st = s.get('stats', {})
                    if st.get('era') or st.get('strikeOuts'):
                        p_whip = float(st.get('whip', 1.30))
                        ip_raw = st.get('inningsPitched', 0)
                        p_ip   = float(str(ip_raw).replace('.1', '.33').replace('.2', '.67')) if ip_raw else 0.0
                        so_    = float(st.get('strikeOuts', 0))
                        bb_    = float(st.get('baseOnBalls', 0))
                        hr_    = float(st.get('homeRuns', 0))
                        p_fip  = _regress(_compute_fip(so_, bb_, hr_, p_ip), p_ip, _REGRESS_IP, _LEAGUE_FIP) \
                                 if p_ip > 0 else _LEAGUE_FIP
                        break
                if p_fip is not None:
                    tid = _find_team_id(team_name_)
                    if tid:
                        return (str(tid), pitcher_name, team_name_, p_fip, p_whip or 1.30, p_ip)
            except Exception:
                pass
            return None

        with ThreadPoolExecutor(max_workers=8) as ex:
            futures = {ex.submit(_fetch_one, pn, tn): (pn, tn) for pn, tn in tasks}
            for fut in as_completed(futures):
                result = fut.result()
                if result:
                    tid, pname, tname, p_fip, p_whip, p_ip = result
                    _pitcher_cache[tid] = {
                        "name": pname, "era": p_fip, "fip": p_fip,
                        "whip": p_whip, "ip": p_ip,
                    }
                    print(f"  [mlb_stats] SP: {pname} ({tname}) FIP={p_fip:.2f} WHIP={p_whip}")
    except Exception as e:
        print(f"  [mlb_stats] Erreur fetch pitchers: {e}")


def get_team_stats(team_name: str) -> dict:
    """Retourne les stats d'une équipe par son nom."""
    _ensure_standings()
    tid = _find_team_id(team_name)
    if tid is None:
        return {}
    return _team_stats_cache.get(str(tid), {})


def _home_win_pct(stats: dict) -> float:
    hw = stats.get("home_wins", 0)
    hl = stats.get("home_losses", 0)
    total = hw + hl
    return hw / total if total > 0 else 0.5


def _away_win_pct(stats: dict) -> float:
    aw = stats.get("away_wins", 0)
    al = stats.get("away_losses", 0)
    total = aw + al
    return aw / total if total > 0 else 0.5


def _recent_form(stats: dict) -> float:
    w = stats.get("last10_wins", 0)
    l = stats.get("last10_losses", 0)
    total = w + l
    return w / total if total > 0 else 0.5


def _bullpen_era(team_stats: dict, starter_era: float) -> float:
    """
    Estime l'ERA du bullpen.
    team ERA = mix de starter ERA + bullpen ERA pondéré par IP.
    En moyenne : starters lancent 5.5 IP / 9 IP → 61%.
    bullpen_era ≈ (team_era - starter_era * 0.61) / 0.39
    """
    team_era = team_stats.get("fip", team_stats.get("era", 4.50))  # "era" n'existe pas dans le cache → fallback sur "fip"
    bp_era = (team_era - starter_era * 0.61) / 0.39
    return max(1.50, min(8.0, bp_era))


# --- Calcul de la force d'équipe MLB -----------------------------------------

_NORM = {
    "pyth_wpct":     (0.30, 0.70),   # Pythagorean W%
    "home_win_pct":  (0.30, 0.70),
    "away_win_pct":  (0.25, 0.65),
    "recent_form":   (0.20, 0.80),
    "runs_per_game": (2.5,  6.5),
    "fip":           (2.50, 5.50),   # FIP (meilleur prédicteur que ERA)
    "starter_fip":   (2.00, 6.00),   # FIP lanceur partant
    "starter_whip":  (0.80, 1.70),
    "bullpen_era":   (2.50, 6.00),
    "woba":          (0.270, 0.370), # wOBA (meilleur prédicteur que OPS)
    "whip":          (0.90, 1.60),
    "run_diff":      (-80, 120),
}


def _normalize(val: float, mn: float, mx: float, invert: bool = False) -> float:
    if mx == mn:
        return 0.5
    norm = (val - mn) / (mx - mn)
    norm = max(0.0, min(1.0, norm))
    return 1.0 - norm if invert else norm


def get_factor_scores(team_name: str, is_home: bool = True,
                       stats: dict = None) -> dict:
    """Retourne les scores normalisés (0-1) de chaque facteur pour une équipe."""
    if stats is None:
        stats = get_team_stats(team_name)
    if not stats:
        return {k: 0.5 for k in DEFAULT_WEIGHTS}

    tid = _find_team_id(team_name)

    # Lanceur partant
    sp = _pitcher_cache.get(str(tid), {}) if tid else {}
    sp_era  = sp.get("era", stats.get("era", 4.50))
    sp_whip = sp.get("whip", stats.get("whip", 1.30))

    # Bullpen
    bp_era = _bullpen_era(stats, sp_era)

    if is_home:
        split = _normalize(_home_win_pct(stats), *_NORM["home_win_pct"])
    else:
        split = _normalize(_away_win_pct(stats), *_NORM["away_win_pct"])

    woba_val = float(stats.get("woba", _LEAGUE_WOBA))
    fip_val  = float(stats.get("fip",  _LEAGUE_FIP))
    sp_fip   = sp.get("fip", sp.get("era", fip_val))  # FIP partant, fallback sur FIP équipe
    bp_era   = _bullpen_era(stats, sp_fip)

    return {
        "pyth_wpct":     _normalize(stats.get("pyth_wpct", _LEAGUE_WPCT), *_NORM["pyth_wpct"]),
        "home_away":     split,
        "recent_form":   _normalize(_recent_form(stats),                   *_NORM["recent_form"]),
        "runs_per_game": _normalize(stats.get("runs_per_game", 4.5),       *_NORM["runs_per_game"]),
        "fip":           _normalize(fip_val,                                *_NORM["fip"],          invert=True),
        "woba":          _normalize(woba_val,                               *_NORM["woba"]),
        "whip":          _normalize(stats.get("whip", 1.30),               *_NORM["whip"],         invert=True),
        "starter_fip":   _normalize(sp_fip,                                 *_NORM["starter_fip"],  invert=True),
        "starter_whip":  _normalize(sp_whip,                                *_NORM["starter_whip"], invert=True),
        "bullpen_era":   _normalize(bp_era,                                 *_NORM["bullpen_era"],  invert=True),
        "run_diff":      _normalize(stats.get("run_diff", 0),              *_NORM["run_diff"]),
    }


def team_strength_score(team_name: str, is_home: bool = True,
                         weights: dict = None) -> float:
    """Score de force d'équipe (0-1) basé sur tous les facteurs."""
    w = weights or load_weights()

    try:
        _fetch_todays_pitchers()
    except Exception:
        pass

    stats = get_team_stats(team_name)
    if not stats:
        return 0.5

    fs = get_factor_scores(team_name, is_home, stats=stats)
    score = sum(w.get(k, 0) * v for k, v in fs.items())
    return round(max(0.0, min(1.0, score)), 4)


# --- Probabilité ajustée avec stats MLB + parc --------------------------------

def _poisson_over_prob(expected_runs: float, line: float) -> float:
    """
    Probabilité que le total de runs > line, via CDF Poisson.
    Plus précis que l'approximation linéaire.
    """
    if expected_runs <= 0:
        return 0.01
    # P(X > line) = 1 - P(X <= floor(line))
    k_max = int(line)
    # Si line est entière, over = total > line (pas >=)
    # Si line est .5, over = total >= ceil(line)
    is_half = (line % 1.0) > 0.01
    if is_half:
        k_max = int(line)  # e.g. line=8.5 → P(X >= 9) = 1 - P(X <= 8)

    cdf = 0.0
    for k in range(k_max + 1):
        cdf += math.exp(-expected_runs) * (expected_runs ** k) / math.factorial(k)
    return max(0.05, min(0.95, 1.0 - cdf))


def get_adjusted_prob(home_team: str, away_team: str,
                       bet_type: str, selection: str,
                       math_prob: float,
                       match_date: str = "",
                       weights: dict = None) -> float:
    """
    Calcule la probabilité ajustée en intégrant les stats MLB,
    le lanceur partant, le parc et le bullpen.
    """
    # stat_vs_math : priorité au fichier weights.json (calibré automatiquement)
    stat_weight = load_stat_vs_math()
    try:
        from predictions import get_feature_weights
        fw = get_feature_weights(sport="baseball")
        intra = fw.get("intra_stat", {})
    except Exception:
        intra = {}

    bet_lower = bet_type.lower()
    sel_lower = selection.lower()

    # Charger les lanceurs (best effort)
    try:
        _fetch_todays_pitchers()
    except Exception:
        pass

    home_tid = _find_team_id(home_team)
    away_tid = _find_team_id(away_team)

    # Park factor
    pf = _park_factor(home_tid) if home_tid else 1.0

    # --- Paris sur le gagnant (moneyline) ---
    if any(k in bet_lower for k in ("gagnant", "victoire", "winner", "2 issues", "moneyline")):
        w_override = intra if intra else None
        home_str = team_strength_score(home_team, is_home=True,  weights=w_override)
        away_str = team_strength_score(away_team, is_home=False, weights=w_override)

        # Avantage terrain MLB : ~4% multiplicatif (proportionnel à la force réelle)
        home_str = min(1.0, home_str * 1.04)

        total = home_str + away_str
        if total <= 0:
            return math_prob

        sel_is_home = any(frag in sel_lower for frag in _team_fragments(home_team))
        stat_prob = home_str / total if sel_is_home else away_str / total

        # Cohérence modèle↔direction : si le modèle contredit la sélection,
        # augmenter son poids pour freiner le pari (même logique que les totaux)
        model_favors_selection = stat_prob >= 0.50
        if not model_favors_selection:
            effective_stat_weight = min(0.75, stat_weight * 1.5)
        else:
            effective_stat_weight = stat_weight

        adjusted = effective_stat_weight * stat_prob + (1 - effective_stat_weight) * math_prob
        return round(max(0.01, min(0.99, adjusted)), 4)

    # --- Paris Total de points (over/under) ---
    if any(k in bet_lower for k in ("total", "plus/moins")):
        home_stats = get_team_stats(home_team)
        away_stats = get_team_stats(away_team)

        home_rpg = home_stats.get("runs_per_game", 4.5)
        away_rpg = away_stats.get("runs_per_game", 4.5)
        home_era = home_stats.get("fip", home_stats.get("era", 4.50))  # FIP = proxy ERA (clé "era" absente du cache)
        away_era = away_stats.get("fip", away_stats.get("era", 4.50))

        # Lanceur partant : impact direct sur les runs permis
        home_sp = _pitcher_cache.get(str(home_tid), {}) if home_tid else {}
        away_sp = _pitcher_cache.get(str(away_tid), {}) if away_tid else {}
        home_sp_era = home_sp.get("era", home_era)  # pitcher_cache a bien la clé "era" (= FIP individuel)
        away_sp_era = away_sp.get("era", away_era)

        # Runs attendus de chaque équipe :
        #   L'attaque de l'away frappe contre le lanceur home et le bullpen home
        #   Starters lancent ~61% des IP, bullpen ~39%
        home_bp_era = _bullpen_era(home_stats, home_sp_era)
        away_bp_era = _bullpen_era(away_stats, away_sp_era)

        # Runs de l'équipe away = f(away_rpg, home_sp_era, home_bp_era)
        # Formule : RPG ajusté = RPG_base × (pitcher_ERA / ligue_ERA_moy)
        league_avg_era = 4.20
        away_runs = away_rpg * (home_sp_era * 0.61 + home_bp_era * 0.39) / league_avg_era
        home_runs = home_rpg * (away_sp_era * 0.61 + away_bp_era * 0.39) / league_avg_era

        # Appliquer le park factor
        expected_total = (home_runs + away_runs) * pf

        # Ajustement météo : densité de l'air + composante vent
        try:
            from weather import get_weather_run_adjustment
            _hour = 19  # heure de match par défaut
            if match_date:
                pass  # TODO : extraire l'heure réelle si disponible
            weather_adj, _wdesc, _wdict = get_weather_run_adjustment(home_team, _hour)
            expected_total += weather_adj
        except Exception:
            weather_adj = 0.0

        # Extraire la ligne
        m = re.search(r'(\d+\.?\d*)', selection)
        if not m:
            m = re.search(r'(\d+\.?\d*)', bet_type)
        line = float(m.group(1)) if m else 8.5

        # Poisson pour la probabilité over
        over_prob = _poisson_over_prob(expected_total, line)

        # Correction biais : pour les lignes basses (≤5.0 runs par équipe),
        # le modèle Poisson surestime systématiquement la probabilité over.
        if line <= 5.0:
            regression = max(0.0, (5.0 - line) / 5.0) * 0.35
            over_prob = over_prob * (1 - regression) + 0.5 * regression

        is_over = "plus" in sel_lower or "over" in sel_lower or "au moins" in sel_lower
        stat_prob = over_prob if is_over else 1.0 - over_prob

        # Cohérence modèle vs direction du pari :
        # Si on parie "Plus de" mais que le modèle prédit < ligne → stat_weight élevé
        # (le modèle dit clairement non, respecter ça)
        # Si on parie "Plus de" et modèle prédit > ligne → confiance normale
        model_agrees = (is_over and expected_total >= line) or (not is_over and expected_total <= line)

        if not model_agrees:
            # Modèle contredit la direction du pari — augmenter son poids pour bloquer
            effective_stat_weight = min(0.75, stat_weight * 1.5)
        else:
            # Pour les totaux d'équipe à ligne basse, le marché est efficient
            is_team_total = line <= 6.0
            effective_stat_weight = stat_weight * 0.70 if is_team_total else stat_weight

        adjusted = effective_stat_weight * stat_prob + (1 - effective_stat_weight) * math_prob
        return round(max(0.01, min(0.99, adjusted)), 4)

    return math_prob


def _team_fragments(team_name: str) -> list[str]:
    """Génère des fragments pour matcher le nom dans une sélection."""
    lower = team_name.lower()
    clean = re.sub(r'\s*\(([^)]+)\)', r' \1', lower).strip()
    frags = [lower, clean]
    for word in clean.split():
        if len(word) > 3:
            frags.append(word)
    return frags


def get_todays_schedule() -> list[dict]:
    """Retourne les matchs MLB d'aujourd'hui avec les lanceurs prévus."""
    if not _STATSAPI_AVAILABLE:
        return []
    try:
        today = date.today().strftime('%m/%d/%Y')
        schedule = statsapi.schedule(sportId=1, date=today)
        return schedule if isinstance(schedule, list) else []
    except Exception as e:
        print(f"  [mlb_stats] Erreur schedule: {e}")
        return []
