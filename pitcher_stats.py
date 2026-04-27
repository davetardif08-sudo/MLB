"""
Stats des lanceurs partants MLB.

Sources :
  - mlb-statsapi : ERA, WHIP, K/9, BB/9 de la saison en cours
  - pybaseball (optionnel) : FIP, xFIP des saisons précédentes

Les lanceurs partants sont le facteur prédictif le plus important en MLB
(représente ~40% de la valeur prédictive selon les études).

Cache : 6 heures
"""

import json
import time
from pathlib import Path
from typing import Optional

try:
    import statsapi
    _STATSAPI_AVAILABLE = True
except ImportError:
    _STATSAPI_AVAILABLE = False


_CACHE_FILE = Path(__file__).parent / ".pitcher_stats_cache.json"
_CACHE_TTL  = 6 * 3600  # 6 heures

_pitcher_cache: dict = {}  # {player_id: {stats...}}
_cache_ts: float = 0.0


def _load_cache():
    global _pitcher_cache, _cache_ts
    if not _CACHE_FILE.exists():
        return
    try:
        with open(_CACHE_FILE, encoding="utf-8") as f:
            data = json.load(f)
        if time.time() - data.get("_ts", 0) < _CACHE_TTL:
            _pitcher_cache = data.get("pitchers", {})
            _cache_ts      = data.get("_ts", 0.0)
    except Exception:
        pass


def _save_cache():
    try:
        with open(_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump({
                "_ts":      time.time(),
                "pitchers": _pitcher_cache,
            }, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


_load_cache()


# --- Fetch stats d'un lanceur ------------------------------------------------

def get_pitcher_stats(player_id: int) -> dict:
    """Récupère les stats saisonnières d'un lanceur via statsapi."""
    pid_str = str(player_id)

    # Cache mémoire
    if pid_str in _pitcher_cache:
        return _pitcher_cache[pid_str]

    if not _STATSAPI_AVAILABLE:
        return {}

    try:
        data = statsapi.player_stats(player_id, group='pitching', type='season')
        # Le résultat est une chaîne de texte formatée — parser les valeurs clés
        stats = _parse_pitcher_stats_text(data, player_id)
        _pitcher_cache[pid_str] = stats
        _save_cache()
        return stats
    except Exception as e:
        print(f"  [pitcher_stats] Erreur player {player_id}: {e}")
        return {}


def _parse_pitcher_stats_text(text: str, player_id: int) -> dict:
    """
    Parse le texte de statsapi.player_stats() pour extraire ERA, WHIP, K/9, etc.
    Le format est du texte tabulaire formaté par la librairie.
    """
    stats = {"player_id": player_id}

    if not text:
        return stats

    import re

    # ERA
    m = re.search(r'era\s*[:\|]\s*([0-9]+\.[0-9]+)', text, re.IGNORECASE)
    if m:
        stats["era"] = float(m.group(1))

    # WHIP
    m = re.search(r'whip\s*[:\|]\s*([0-9]+\.[0-9]+)', text, re.IGNORECASE)
    if m:
        stats["whip"] = float(m.group(1))

    # Strikeouts
    m = re.search(r'strikeOuts\s*[:\|]\s*([0-9]+)', text, re.IGNORECASE)
    if not m:
        m = re.search(r'\bso\b\s*[:\|]\s*([0-9]+)', text, re.IGNORECASE)
    if m:
        stats["strikeouts"] = int(m.group(1))

    # Innings pitched
    m = re.search(r'inningsPitched\s*[:\|]\s*([0-9]+\.[0-9]+)', text, re.IGNORECASE)
    if not m:
        m = re.search(r'\bip\b\s*[:\|]\s*([0-9]+\.[0-9]+)', text, re.IGNORECASE)
    if m:
        ip = float(m.group(1))
        stats["innings_pitched"] = ip
        if "strikeouts" in stats and ip > 0:
            stats["k9"] = round(stats["strikeouts"] / ip * 9, 2)

    # Walks
    m = re.search(r'baseOnBalls\s*[:\|]\s*([0-9]+)', text, re.IGNORECASE)
    if not m:
        m = re.search(r'\bbb\b\s*[:\|]\s*([0-9]+)', text, re.IGNORECASE)
    if m:
        bb = int(m.group(1))
        stats["walks"] = bb
        ip = stats.get("innings_pitched", 1)
        if ip > 0:
            stats["bb9"] = round(bb / ip * 9, 2)

    # Home runs allowed
    m = re.search(r'homeRuns\s*[:\|]\s*([0-9]+)', text, re.IGNORECASE)
    if m:
        hr = int(m.group(1))
        stats["hr_allowed"] = hr
        ip = stats.get("innings_pitched", 1)
        if ip > 0:
            stats["hr9"] = round(hr / ip * 9, 2)

    # FIP approximé si ERA + HR + BB + K disponibles
    if all(k in stats for k in ("era", "walks", "strikeouts", "hr_allowed", "innings_pitched")):
        ip = stats["innings_pitched"]
        if ip > 0:
            # FIP = (13*HR + 3*BB - 2*K) / IP + cFIP
            cfip = 3.2  # constante FIP approx
            fip = (13 * stats["hr_allowed"] + 3 * stats["walks"]
                   - 2 * stats["strikeouts"]) / ip + cfip
            stats["fip"] = round(max(0.0, fip), 2)

    return stats


def get_pitcher_from_schedule(game_id: int, side: str = 'home') -> Optional[dict]:
    """
    Récupère le lanceur partant prévu via le schedule MLB.
    side : 'home' ou 'away'
    """
    if not _STATSAPI_AVAILABLE:
        return None
    try:
        boxscore = statsapi.boxscore_data(game_id)
        team_key = 'home' if side == 'home' else 'away'
        pitchers = boxscore.get(team_key, {}).get('pitchers', [])
        if pitchers:
            # Le premier lanceur = lanceur partant
            p = pitchers[0]
            return {
                "player_id":  p.get("personId"),
                "name":       p.get("fullName", ""),
                "jersey":     p.get("jerseyNumber", ""),
            }
    except Exception:
        pass
    return None


# --- Score de qualité d'un lanceur -------------------------------------------

# Normalisation ERA (plus bas = mieux) : élite=2.50, mauvais=6.00
_ERA_MIN, _ERA_MAX     = 2.50, 6.00
_WHIP_MIN, _WHIP_MAX   = 0.90, 1.60
_K9_MIN, _K9_MAX       = 4.0,  12.0
_FIP_MIN, _FIP_MAX     = 2.50, 6.00


def pitcher_quality_score(stats: dict) -> float:
    """
    Calcule un score de qualité du lanceur (0-1).
    1.0 = lanceur élite, 0.0 = lanceur très faible.
    """
    if not stats:
        return 0.5  # Neutre si pas de données

    scores = []

    era = stats.get("era")
    if era is not None:
        s = 1.0 - (min(max(era, _ERA_MIN), _ERA_MAX) - _ERA_MIN) / (_ERA_MAX - _ERA_MIN)
        scores.append(("era", s, 0.35))

    whip = stats.get("whip")
    if whip is not None:
        s = 1.0 - (min(max(whip, _WHIP_MIN), _WHIP_MAX) - _WHIP_MIN) / (_WHIP_MAX - _WHIP_MIN)
        scores.append(("whip", s, 0.25))

    k9 = stats.get("k9")
    if k9 is not None:
        s = (min(max(k9, _K9_MIN), _K9_MAX) - _K9_MIN) / (_K9_MAX - _K9_MIN)
        scores.append(("k9", s, 0.20))

    fip = stats.get("fip")
    if fip is not None:
        s = 1.0 - (min(max(fip, _FIP_MIN), _FIP_MAX) - _FIP_MIN) / (_FIP_MAX - _FIP_MIN)
        scores.append(("fip", s, 0.20))

    if not scores:
        return 0.5

    total_weight = sum(w for _, _, w in scores)
    if total_weight <= 0:
        return 0.5

    weighted_sum = sum(s * w for _, s, w in scores)
    return round(weighted_sum / total_weight, 4)


def pitching_advantage(home_pitcher_stats: dict, away_pitcher_stats: dict) -> float:
    """
    Retourne l'avantage pitching pour l'équipe à domicile.
    Positif = avantage domicile, négatif = avantage visiteur.
    Plage : [-0.15, +0.15] — traduit en ajustement de probabilité.
    """
    home_score = pitcher_quality_score(home_pitcher_stats)
    away_score = pitcher_quality_score(away_pitcher_stats)
    raw_diff   = home_score - away_score
    # Limiter l'impact à ±15% de probabilité
    return round(max(-0.15, min(0.15, raw_diff * 0.3)), 4)
