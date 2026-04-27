"""
Module météo MLB — Open-Meteo (gratuit, sans clé API)

Impact sur les totaux :
  - Densité de l'air (température + pression + humidité) → portée de la balle
  - Composante vent (vitesse × cos(angle vs orientation stade)) → runs in/out
  - Précipitations → risque de report

Formule physique :
  ρ = Pair_sec / (Rd × T) + Pvapeur / (Rv × T)
  Air moins dense → balle porte plus loin → plus de HR → plus de runs

Cache : 1 heure par stade.
"""

import math
import time
import requests
from typing import Optional

_weather_cache: dict = {}
_CACHE_TTL = 3600  # 1 heure

# ─── Stades MLB : coordonnées + bearing home → CF ─────────────────────────────
# cf_bearing : direction boussole du marbre vers le champ central (0=N, 90=E…)
# is_dome    : True = toit fixe (météo sans impact)
# retractable: True = toit rétractable (impact météo réduit à 50%)

STADIUMS = {
    108: {"name": "Angel Stadium",         "lat": 33.8003,  "lon": -117.8827, "cf_bearing": 310, "is_dome": False},
    109: {"name": "Chase Field",           "lat": 33.4453,  "lon": -112.0667, "cf_bearing": 315, "is_dome": False, "retractable": True},
    110: {"name": "Camden Yards",          "lat": 39.2838,  "lon": -76.6218,  "cf_bearing": 350, "is_dome": False},
    111: {"name": "Fenway Park",           "lat": 42.3467,  "lon": -71.0972,  "cf_bearing": 95,  "is_dome": False},
    112: {"name": "Wrigley Field",         "lat": 41.9484,  "lon": -87.6553,  "cf_bearing": 177, "is_dome": False},
    113: {"name": "Great American BP",     "lat": 39.0979,  "lon": -84.5082,  "cf_bearing": 309, "is_dome": False},
    114: {"name": "Progressive Field",     "lat": 41.4962,  "lon": -81.6852,  "cf_bearing": 320, "is_dome": False},
    115: {"name": "Coors Field",           "lat": 39.7559,  "lon": -104.9942, "cf_bearing": 292, "is_dome": False},
    116: {"name": "Comerica Park",         "lat": 42.3390,  "lon": -83.0485,  "cf_bearing": 310, "is_dome": False},
    117: {"name": "Minute Maid Park",      "lat": 29.7573,  "lon": -95.3555,  "cf_bearing": 315, "is_dome": False, "retractable": True},
    118: {"name": "Kauffman Stadium",      "lat": 39.0517,  "lon": -94.4803,  "cf_bearing": 340, "is_dome": False},
    119: {"name": "Dodger Stadium",        "lat": 34.0739,  "lon": -118.2400, "cf_bearing": 315, "is_dome": False},
    120: {"name": "Nationals Park",        "lat": 38.8731,  "lon": -77.0074,  "cf_bearing": 313, "is_dome": False},
    121: {"name": "Citi Field",            "lat": 40.7571,  "lon": -73.8458,  "cf_bearing": 330, "is_dome": False},
    133: {"name": "Sutter Health Park",    "lat": 38.5807,  "lon": -121.5005, "cf_bearing": 330, "is_dome": False},
    134: {"name": "PNC Park",              "lat": 40.4469,  "lon": -80.0057,  "cf_bearing": 310, "is_dome": False},
    135: {"name": "Petco Park",            "lat": 32.7073,  "lon": -117.1566, "cf_bearing": 310, "is_dome": False},
    136: {"name": "T-Mobile Park",         "lat": 47.5914,  "lon": -122.3325, "cf_bearing": 330, "is_dome": False, "retractable": True},
    137: {"name": "Oracle Park",           "lat": 37.7786,  "lon": -122.3893, "cf_bearing": 288, "is_dome": False},
    138: {"name": "Busch Stadium",         "lat": 38.6226,  "lon": -90.1928,  "cf_bearing": 333, "is_dome": False},
    139: {"name": "Tropicana Field",       "lat": 27.7683,  "lon": -82.6534,  "cf_bearing": 305, "is_dome": True},
    140: {"name": "Globe Life Field",      "lat": 32.7512,  "lon": -97.0832,  "cf_bearing": 325, "is_dome": False, "retractable": True},
    141: {"name": "Rogers Centre",         "lat": 43.6414,  "lon": -79.3894,  "cf_bearing": 2,   "is_dome": True},
    142: {"name": "Target Field",          "lat": 44.9817,  "lon": -93.2781,  "cf_bearing": 303, "is_dome": False},
    143: {"name": "Citizens Bank Park",    "lat": 39.9061,  "lon": -75.1665,  "cf_bearing": 330, "is_dome": False},
    144: {"name": "Truist Park",           "lat": 33.8908,  "lon": -84.4678,  "cf_bearing": 300, "is_dome": False},
    145: {"name": "Guaranteed Rate Field", "lat": 41.8300,  "lon": -87.6339,  "cf_bearing": 355, "is_dome": False},
    146: {"name": "loanDepot park",        "lat": 25.7781,  "lon": -80.2197,  "cf_bearing": 355, "is_dome": False, "retractable": True},
    147: {"name": "Yankee Stadium",        "lat": 40.8296,  "lon": -73.9262,  "cf_bearing": 274, "is_dome": False},
    158: {"name": "Am. Family Field",      "lat": 43.0280,  "lon": -87.9712,  "cf_bearing": 330, "is_dome": False, "retractable": True},
}

# Densité de l'air de référence (20°C, 1013.25 hPa, 50% humidité)
_RHO_REF = 1.1989  # kg/m³


# ─── Physique ─────────────────────────────────────────────────────────────────

def calc_air_density(temp_c: float, pressure_hpa: float, humidity_pct: float) -> float:
    """
    Densité de l'air (kg/m³) via loi des gaz parfaits.
    Moins dense → balle porte plus loin → plus de runs.
    Humidité élevée = air MOINS dense (H2O = 18 g/mol vs N2 = 28, O2 = 32).
    """
    T    = temp_c + 273.15                                    # Kelvin
    P    = pressure_hpa * 100                                 # Pascals
    Psat = 611.2 * math.exp(17.67 * temp_c / (temp_c + 243.5))  # vapeur saturante
    Pv   = (humidity_pct / 100.0) * Psat                     # pression vapeur réelle
    Pd   = P - Pv                                             # pression air sec
    return Pd / (287.05 * T) + Pv / (461.5 * T)


def _wind_component(wind_speed_kmh: float, wind_dir_from: float,
                    cf_bearing: float) -> float:
    """
    Composante du vent dans l'axe home-plate → CF.
    wind_dir_from : direction D'OÙ vient le vent (convention météo : 270° = vent d'Ouest)
    Retourne > 0 si tailwind (sortant, favorise frappeurs)
             < 0 si headwind (entrant, favorise lanceurs)
    """
    wind_travel  = (wind_dir_from + 180) % 360   # direction vers où le vent souffle
    angle_diff   = ((wind_travel - cf_bearing + 180) % 360) - 180
    return wind_speed_kmh * math.cos(math.radians(angle_diff))


def _wind_compass(deg: float) -> str:
    """Convertit un angle en point cardinal (ex: 270 → 'O')."""
    directions = ["N", "NE", "E", "SE", "S", "SO", "O", "NO"]
    idx = round(deg / 45) % 8
    return directions[idx]


# ─── Fetch & cache ────────────────────────────────────────────────────────────

def get_weather(home_team_id: int, game_hour: int = 19) -> Optional[dict]:
    """
    Retourne le dict météo complet pour un stade MLB.
    Utilise Open-Meteo (gratuit, sans clé).
    Retourne un dict avec run_adjustment = 0 pour les dômes.
    """
    stadium = STADIUMS.get(home_team_id)
    if not stadium:
        return None

    # Dôme fixe → météo sans impact
    if stadium.get("is_dome"):
        return {
            "stadium": stadium["name"], "is_dome": True, "retractable": False,
            "temp_c": 22, "temp_f": 72, "wind_kmh": 0, "wind_dir": 0,
            "wind_cardinal": "--", "wind_component": 0,
            "pressure_hpa": 1013, "humidity_pct": 50, "precip_prob": 0,
            "air_density": _RHO_REF, "density_delta_pct": 0,
            "density_runs": 0.0, "wind_runs": 0.0, "run_adjustment": 0.0,
            "description": "Toit fixe — météo sans impact",
        }

    cache_key = (home_team_id, game_hour)
    cached = _weather_cache.get(cache_key)
    if cached and time.time() - cached.get("_ts", 0) < _CACHE_TTL:
        return cached

    try:
        from datetime import date
        r = requests.get("https://api.open-meteo.com/v1/forecast", params={
            "latitude":    stadium["lat"],
            "longitude":   stadium["lon"],
            "hourly":      ",".join([
                "temperature_2m", "windspeed_10m", "winddirection_10m",
                "surface_pressure", "relativehumidity_2m",
                "precipitation_probability",
            ]),
            "forecast_days": 2,
            "timezone":    "auto",
        }, timeout=8)

        if not r.ok:
            return None

        hourly = r.json()["hourly"]
        today  = date.today().isoformat()

        # Trouver l'index de l'heure du match
        idx = next(
            (i for i, t in enumerate(hourly["time"])
             if t.startswith(f"{today}T{game_hour:02d}")),
            game_hour,
        )

        temp_c   = hourly["temperature_2m"][idx]
        wind_kmh = hourly["windspeed_10m"][idx]
        wind_dir = hourly["winddirection_10m"][idx]
        pressure = hourly["surface_pressure"][idx]
        humidity = hourly["relativehumidity_2m"][idx]
        precip   = (hourly["precipitation_probability"][idx]
                    if idx < len(hourly.get("precipitation_probability", []))
                    else 0)

        # ── Densité ──────────────────────────────────────────────────
        rho           = calc_air_density(temp_c, pressure, humidity)
        density_delta = (_RHO_REF - rho) / _RHO_REF * 100  # + = moins dense = plus de runs
        density_runs  = max(-1.0, min(2.0, density_delta * 0.08))

        # ── Vent ─────────────────────────────────────────────────────
        cf_bearing = stadium["cf_bearing"]
        wc         = _wind_component(wind_kmh, wind_dir, cf_bearing)
        wind_runs  = max(-1.5, min(1.5, wc * 0.025))

        # Toit rétractable : impact météo réduit de 40%
        if stadium.get("retractable"):
            density_runs *= 0.6
            wind_runs    *= 0.6

        run_adjustment = round(density_runs + wind_runs, 2)

        # ── Description ──────────────────────────────────────────────
        vent_cardinal = _wind_compass(wind_dir)
        wind_desc = ""
        if wind_kmh > 8:
            direction_rel = "sortant" if wc > 3 else ("entrant" if wc < -3 else "latéral")
            wind_desc = f"vent {vent_cardinal} {wind_kmh:.0f} km/h ({direction_rel})"

        if run_adjustment >= 0.5:
            impact_txt = f"+{run_adjustment:.1f} runs (favorise Over)"
        elif run_adjustment <= -0.4:
            impact_txt = f"{run_adjustment:.1f} runs (favorise Under)"
        else:
            impact_txt = "conditions neutres"

        parts = [f"{temp_c:.0f}°C"]
        if wind_desc:
            parts.append(wind_desc)
        if precip >= 40:
            parts.append(f"pluie {precip}%")
        description = " - ".join(parts) + f" -> {impact_txt}"

        result = {
            "stadium":           stadium["name"],
            "is_dome":           False,
            "retractable":       stadium.get("retractable", False),
            "temp_c":            round(temp_c, 1),
            "temp_f":            round(temp_c * 9 / 5 + 32, 1),
            "wind_kmh":          round(wind_kmh, 1),
            "wind_dir":          round(wind_dir),
            "wind_cardinal":     vent_cardinal,
            "wind_component":    round(wc, 1),
            "pressure_hpa":      round(pressure, 1),
            "humidity_pct":      humidity,
            "precip_prob":       precip,
            "air_density":       round(rho, 4),
            "density_delta_pct": round(density_delta, 2),
            "density_runs":      round(density_runs, 2),
            "wind_runs":         round(wind_runs, 2),
            "run_adjustment":    run_adjustment,
            "description":       description,
            "_ts":               time.time(),
        }
        _weather_cache[cache_key] = result
        try:
            print(f"  [weather] {stadium['name']}: {description}".encode('ascii', errors='replace').decode('ascii'))
        except Exception:
            pass
        return result

    except Exception as e:
        try:
            print(f"  [weather] Erreur stade {home_team_id}: {e}")
        except Exception:
            pass
        return None


def get_weather_run_adjustment(home_team: str, game_hour: int = 19) -> tuple:
    """
    Interface principale pour mlb_stats.py.
    Retourne (run_adjustment: float, description: str, weather_dict: dict).
    """
    try:
        from mlb_stats import _find_team_id
        tid = _find_team_id(home_team)
        if not tid:
            return 0.0, "", {}
        w = get_weather(tid, game_hour)
        if not w:
            return 0.0, "", {}
        return w["run_adjustment"], w.get("description", ""), w
    except Exception as e:
        print(f"  [weather] get_weather_run_adjustment: {e}")
        return 0.0, "", {}
