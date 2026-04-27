"""
Analyzer V2 — Système basé sur des signaux non-capturés par le marché.

Philosophie fondamentale :
  - Les stats publiques (ERA, W%, WHIP) sont DÉJÀ dans les cotes → inutiles
  - Le marché est efficient sur les favoris populaires → les fader
  - Les vrais edges : bullpen repos, splits L/R, biais public, cotes underdogs

Signaux utilisés :
  1. Underdog value   (35%) — Dogs 1.85-2.60 contre favoris surpayés
  2. Bullpen repos    (30%) — Repos des releveurs 3 derniers jours
  3. Biais public     (20%) — Équipes populaires systématiquement surcôtées
  4. Splits L/R       (15%) — Avantage main du lanceur vs alignement adverse
"""

from dataclasses import dataclass, field
from typing import Optional
import statsapi
from datetime import date as _date, timedelta


# ─── Équipes que le public sur-mise systématiquement ──────────────────────────
# Source: études de public betting bias en MLB
POPULAR_TEAMS = {
    "new york (yankees)", "new york yankees", "yankees",
    "los angeles (dodgers)", "los angeles dodgers", "dodgers",
    "boston (red sox)", "boston red sox", "red sox",
    "chicago (cubs)", "chicago cubs", "cubs",
    "st. louis (cardinals)", "saint-louis (cardinals)", "cardinals",
    "san francisco (giants)", "san francisco giants", "giants",
    "new york (mets)", "new york mets", "mets",
    "philadelphia (phillies)", "philadelphia phillies", "phillies",
}

# ─── Données de sortie ────────────────────────────────────────────────────────

@dataclass
class V2Signal:
    name: str
    score: float        # -1.0 à +1.0 (0 = neutre)
    weight: float
    description: str


@dataclass
class V2Pick:
    """Un pick recommandé par le système V2."""
    home_team: str
    away_team: str
    date: str
    time: str
    bet_team: str           # Équipe sur laquelle on mise
    bet_side: str           # "home" ou "away"
    odds: float
    implied_prob: float
    signals: list[V2Signal] = field(default_factory=list)
    composite_score: float = 0.0    # Score composite pondéré
    confidence: str = ""            # "Fort", "Moyen", "Faible"
    rationale: str = ""             # Explication lisible
    home_pitcher: str = ""
    away_pitcher: str = ""
    event_id: str = ""
    league: str = "MLB"
    sport: str = "baseball"
    system_version: str = "v2"

    @property
    def display_match(self) -> str:
        p = f"  [{self.away_pitcher} vs {self.home_pitcher}]" if (self.home_pitcher or self.away_pitcher) else ""
        return f"{self.away_team} @ {self.home_team}{p}"


# ─── Cache bullpen ────────────────────────────────────────────────────────────

_bullpen_cache: dict = {}  # team_name → (score, timestamp)


def _get_bullpen_rest_score(team_name: str, match_date: str) -> tuple[float, str]:
    """
    Score repos bullpen : +1.0 = bien reposé, -1.0 = épuisé.
    Basé sur les innings des releveurs lors des 3 derniers jours.
    """
    cache_key = f"{team_name}|{match_date}"
    if cache_key in _bullpen_cache:
        return _bullpen_cache[cache_key]

    try:
        from mlb_stats import _find_team_id
        team_id = _find_team_id(team_name)
        if not team_id:
            return 0.0, "équipe introuvable"

        today = _date.fromisoformat(match_date) if match_date else _date.today()
        start = (today - timedelta(days=3)).isoformat()
        end   = (today - timedelta(days=1)).isoformat()

        schedule = statsapi.schedule(start_date=start, end_date=end, team=team_id)
        if not schedule:
            return 0.0, "aucune partie récente"

        total_bullpen_ip = 0.0
        games_checked = 0

        for game in schedule[-3:]:
            gid = game.get("game_id")
            if not gid:
                continue
            try:
                box = statsapi.boxscore_data(gid)
                # Déterminer si c'est home ou away
                home_id = box.get("home", {}).get("team", {}).get("id")
                side = "home" if str(home_id) == str(team_id) else "away"
                pitchers = box.get(side, {}).get("pitchers", [])
                # Exclure le lanceur partant (index 0) — on veut seulement le bullpen
                relievers = pitchers[1:] if len(pitchers) > 1 else []
                for rel in relievers:
                    stats = rel.get("stats", {}).get("pitching", {})
                    ip_str = str(stats.get("inningsPitched", "0.0"))
                    try:
                        ip = float(ip_str)
                    except ValueError:
                        ip = 0.0
                    total_bullpen_ip += ip
                games_checked += 1
            except Exception:
                continue

        if games_checked == 0:
            result = (0.0, "données bullpen indisponibles")
        else:
            avg_ip = total_bullpen_ip / games_checked
            # Seuil : > 4.0 IP/match = bullpen sollicité, < 2.0 = reposé
            if avg_ip <= 2.0:
                score = 0.6   # bien reposé
                desc = f"reposé ({avg_ip:.1f} IP/match L{games_checked})"
            elif avg_ip <= 3.5:
                score = 0.2   # moyen
                desc = f"normal ({avg_ip:.1f} IP/match L{games_checked})"
            elif avg_ip <= 5.0:
                score = -0.3  # un peu fatigué
                desc = f"sollicité ({avg_ip:.1f} IP/match L{games_checked})"
            else:
                score = -0.7  # épuisé
                desc = f"épuisé ({avg_ip:.1f} IP/match L{games_checked})"
            result = (score, desc)

        _bullpen_cache[cache_key] = result
        return result

    except Exception as e:
        return 0.0, f"erreur: {e}"


def _get_public_bias_score(team_name: str, is_favorite: bool) -> tuple[float, str]:
    """
    Si une équipe populaire est favorite → marché la surcôte → fade.
    Si une équipe populaire est dog → pas de biais.
    """
    name_l = team_name.lower()
    is_popular = any(pop in name_l for pop in POPULAR_TEAMS)

    if is_popular and is_favorite:
        return -0.5, f"{team_name} = équipe populaire favorite (surcôtée par le public)"
    elif is_popular and not is_favorite:
        return 0.1, f"{team_name} = populaire mais dog (sous-côtée possible)"
    return 0.0, "pas de biais public détecté"


def _get_pitcher_split_score(
    pitcher_name: str,
    pitcher_hand: str,   # "L" ou "R"
    opp_lineup_hand: str # "L-heavy", "R-heavy", "balanced"
) -> tuple[float, str]:
    """
    Avantage split : LHP vs alignement droitier = avantage lancer.
    """
    if not pitcher_hand or pitcher_hand == "?":
        return 0.0, "main du lanceur inconnue"

    if pitcher_hand == "L" and opp_lineup_hand == "R-heavy":
        return 0.4, f"LHP {pitcher_name} vs alignement droitier (avantage)"
    elif pitcher_hand == "R" and opp_lineup_hand == "L-heavy":
        return 0.4, f"RHP {pitcher_name} vs alignement gaucher (avantage)"
    elif pitcher_hand == pitcher_hand:  # même main que l'alignement dominant
        return -0.2, f"{pitcher_hand}HP vs alignement favori (désavantage split)"
    return 0.0, "split neutre"


def _get_pitcher_info(pitcher_name: str) -> dict:
    """Retourne la main (L/R) du lanceur depuis statsapi."""
    try:
        if not pitcher_name or pitcher_name in ("?", "TBD", ""):
            return {"hand": "?"}
        search = statsapi.lookup_player(pitcher_name)
        if not search:
            return {"hand": "?"}
        player = search[0]
        pid = player.get("id")
        if not pid:
            return {"hand": "?"}
        detail = statsapi.player_stat_data(pid, group="pitching", type="career")
        hand = player.get("pitchHand", {}).get("code", "?")
        return {"hand": hand, "name": player.get("fullName", pitcher_name)}
    except Exception:
        return {"hand": "?"}


def _estimate_lineup_handedness(team_name: str, match_date: str) -> str:
    """
    Estime si l'alignement est dominé par des droitiers ou gauchers.
    Approximation via statsapi roster.
    """
    try:
        from mlb_stats import _find_team_id
        team_id = _find_team_id(team_name)
        if not team_id:
            return "balanced"

        roster = statsapi.roster(team_id, rosterType="active")
        lines = roster.strip().split("\n")
        lefties = sum(1 for l in lines if "#" in l and "  L  " in l)
        total   = len([l for l in lines if "#" in l])
        if total == 0:
            return "balanced"
        ratio = lefties / total
        if ratio > 0.55:
            return "L-heavy"
        elif ratio < 0.35:
            return "R-heavy"
        return "balanced"
    except Exception:
        return "balanced"


# ─── Moteur V2 ────────────────────────────────────────────────────────────────

class AnalyzerV2:
    """
    Système V2 : signaux non-capturés par le marché.
    Focus : Moneyline underdogs avec avantages structurels.
    """

    # Pondérations des signaux
    WEIGHTS = {
        "underdog_value": 0.35,
        "bullpen_rest":   0.30,
        "public_bias":    0.20,
        "pitcher_split":  0.15,
    }

    # Filtres stricts (non-négociables)
    MIN_ODDS  = 1.85   # Pas de favoris lourds
    MAX_ODDS  = 2.80   # Pas de gros outsiders spéculatifs
    MIN_SCORE = 0.10   # Score composite minimum pour recommander

    def analyze(self, matches: list, top_n: int = 10) -> list[V2Pick]:
        picks = []
        for match in matches:
            new_picks = self._analyze_match(match)
            picks.extend(new_picks)

        # Trier par score composite
        picks.sort(key=lambda p: p.composite_score, reverse=True)

        # Filtrer seulement les picks avec score positif
        picks = [p for p in picks if p.composite_score >= self.MIN_SCORE]

        return picks[:top_n]

    def _analyze_match(self, match) -> list[V2Pick]:
        """Analyse un match et retourne 0-1 picks V2."""
        results = []

        # Collecter les groupes de moneyline
        for group in match.bet_groups:
            if not self._is_moneyline(group.bet_type):
                continue
            if len(group.selections) != 2:
                continue

            sel_a, sel_b = group.selections[0], group.selections[1]
            # implied probs
            ip_a = 1.0 / sel_a.odds if sel_a.odds > 1 else 0
            ip_b = 1.0 / sel_b.odds if sel_b.odds > 1 else 0
            total = ip_a + ip_b
            if total <= 0:
                continue
            fp_a = ip_a / total
            fp_b = ip_b / total

            # Pour chaque sélection, évaluer si c'est un dog viable
            for sel, fp, opp_sel, opp_fp in [
                (sel_a, fp_a, sel_b, fp_b),
                (sel_b, fp_b, sel_a, fp_a),
            ]:
                if not (self.MIN_ODDS <= sel.odds <= self.MAX_ODDS):
                    continue

                # Identifier home/away pour ce pick
                label_l = sel.label.lower()
                is_home = (match.home_team.lower() in label_l or
                           any(part in label_l for part in match.home_team.lower().split()))
                bet_side = "home" if is_home else "away"
                bet_team  = match.home_team if is_home else match.away_team
                opp_team  = match.away_team if is_home else match.home_team

                # Pitcher de ce côté
                bet_pitcher = match.home_pitcher if is_home else match.away_pitcher
                opp_pitcher = match.away_pitcher if is_home else match.home_pitcher

                pick = self._score_pick(
                    match=match,
                    bet_team=bet_team,
                    opp_team=opp_team,
                    bet_side=bet_side,
                    odds=sel.odds,
                    implied_prob=fp,
                    opp_implied=opp_fp,
                    bet_pitcher=bet_pitcher,
                    opp_pitcher=opp_pitcher,
                    bet_group_type=group.bet_type,
                )
                if pick:
                    results.append(pick)

        return results

    def _score_pick(self, match, bet_team, opp_team, bet_side, odds,
                    implied_prob, opp_implied, bet_pitcher, opp_pitcher,
                    bet_group_type) -> Optional[V2Pick]:
        signals = []

        # ── Signal 1 : Underdog Value ──────────────────────────────
        is_dog = implied_prob < 0.50
        is_fav = not is_dog
        opp_is_popular = any(pop in opp_team.lower() for pop in POPULAR_TEAMS)

        if is_dog:
            # Dog contre équipe populaire → meilleur signal
            if opp_is_popular:
                uv_score = 0.8
                uv_desc  = f"Dog ({odds:.2f}) contre favoris populaires ({opp_team})"
            else:
                uv_score = 0.4
                uv_desc  = f"Dog ({odds:.2f}) — valeur potentielle si marché sur-réagit"
        else:
            # Favori : moins intéressant
            uv_score = -0.3
            uv_desc  = f"Favori ({odds:.2f}) — peu de valeur structurelle"

        signals.append(V2Signal("underdog_value", uv_score, self.WEIGHTS["underdog_value"], uv_desc))

        # ── Signal 2 : Bullpen Repos ───────────────────────────────
        bull_score, bull_desc = _get_bullpen_rest_score(bet_team, match.date or "")
        opp_bull_score, opp_bull_desc = _get_bullpen_rest_score(opp_team, match.date or "")
        # Score relatif : notre repos vs leur repos
        rel_bull = round((bull_score - opp_bull_score) / 2.0, 3)
        if bull_desc and opp_bull_desc:
            combined_bull_desc = f"Nous: {bull_desc} | Eux: {opp_bull_desc}"
        else:
            combined_bull_desc = bull_desc or "données indisponibles"
        signals.append(V2Signal("bullpen_rest", rel_bull, self.WEIGHTS["bullpen_rest"], combined_bull_desc))

        # ── Signal 3 : Biais Public ────────────────────────────────
        bias_score, bias_desc = _get_public_bias_score(opp_team, is_favorite=(opp_implied > 0.50))
        # Si adversaire est surcôté → notre équipe est sous-côtée → positif pour nous
        signals.append(V2Signal("public_bias", -bias_score, self.WEIGHTS["public_bias"], bias_desc))

        # ── Signal 4 : Splits L/R ─────────────────────────────────
        opp_lineup = _estimate_lineup_handedness(opp_team, match.date or "")
        pit_info   = _get_pitcher_info(bet_pitcher or "")
        split_score, split_desc = _get_pitcher_split_score(
            pitcher_name=bet_pitcher or "",
            pitcher_hand=pit_info.get("hand", "?"),
            opp_lineup_hand=opp_lineup,
        )
        signals.append(V2Signal("pitcher_split", split_score, self.WEIGHTS["pitcher_split"], split_desc))

        # ── Score composite ────────────────────────────────────────
        composite = sum(s.score * s.weight for s in signals)
        composite = round(composite, 4)

        # Seuil minimum
        if composite < self.MIN_SCORE:
            return None

        # Confidence
        if composite >= 0.35:
            confidence = "Fort 🟢"
        elif composite >= 0.20:
            confidence = "Moyen 🟡"
        else:
            confidence = "Faible 🔸"

        # Rationale lisible
        top_signals = sorted(signals, key=lambda s: abs(s.score * s.weight), reverse=True)[:2]
        rationale = " | ".join(s.description for s in top_signals if s.description)

        return V2Pick(
            home_team=match.home_team,
            away_team=match.away_team,
            date=match.date or "",
            time=match.time or "",
            bet_team=bet_team,
            bet_side=bet_side,
            odds=odds,
            implied_prob=round(implied_prob, 4),
            signals=signals,
            composite_score=composite,
            confidence=confidence,
            rationale=rationale,
            home_pitcher=match.home_pitcher or "",
            away_pitcher=match.away_pitcher or "",
            event_id=match.event_id or "",
            league=match.league or "MLB",
            sport=match.sport or "baseball",
        )

    @staticmethod
    def _is_moneyline(bet_type: str) -> bool:
        bt = bet_type.lower()
        return any(k in bt for k in ("gagnant", "2 issues", "winner", "moneyline", "victoire"))
