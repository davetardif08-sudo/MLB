"""
Moteur d'analyse des cotes pour la MLB.

Adapté de miseojeu-analyzer/analyzer.py avec :
  - Pondérations spécifiques MLB (lanceur partant = 40%)
  - Intégration mlb_stats + pitcher_stats + statcast_stats
  - Calcul Kelly Criterion (via kelly.py)
"""

from dataclasses import dataclass, field
from typing import Optional
from scraper import Match, BetGroup, Selection


# ─── Résultats d'analyse ──────────────────────────────────────────────────────

@dataclass
class AnalyzedSelection:
    selection: Selection
    implied_prob: float
    fair_prob: float
    edge: float
    value_score: float
    recommendation: str


@dataclass
class AnalyzedBetGroup:
    bet_group: BetGroup
    house_margin: float
    selections: list[AnalyzedSelection] = field(default_factory=list)
    best_value: Optional[AnalyzedSelection] = None


@dataclass
class AnalyzedMatch:
    match: Match
    analyzed_groups: list[AnalyzedBetGroup] = field(default_factory=list)
    top_picks: list[AnalyzedSelection] = field(default_factory=list)
    overall_score: float = 0.0


@dataclass
class BettingOpportunity:
    """Un paris recommandé avec son contexte complet."""
    match: Match
    bet_type: str
    selection_label: str
    odds: float
    prediction_id: str
    value_score: float
    recommendation: str
    house_margin: float
    fair_prob: float
    implied_prob: float
    sport: str
    league: str
    math_prob: float = 0.0
    kelly_fraction: float = 0.0    # Fraction Kelly (0.25x conservateur)
    pitcher_info: str = ""         # "SP: Gausman vs Cole" si disponible

    @property
    def display_match(self) -> str:
        if self.pitcher_info:
            return f"{self.match.away_team} @ {self.match.home_team}  [{self.pitcher_info}]"
        return f"{self.match.away_team} @ {self.match.home_team}"

    @property
    def display_date(self) -> str:
        return f"{self.match.date} {self.match.time}"

    def kelly_bet(self, bankroll: float, fraction: float = 0.25) -> float:
        """Calcule la mise recommandée en $ selon Kelly Criterion."""
        from kelly import kelly_bet
        return kelly_bet(self.fair_prob, self.odds, bankroll, fraction)


# ─── Seuils de recommandation ─────────────────────────────────────────────────

THRESHOLDS = {
    "house_margin_low":    5.0,
    "house_margin_medium": 8.0,
    "value_neutral":       50,   # EV neutre (EV=0) — filtre minimum
    "min_odds":            1.45,  # MLB : favoris réguliers à 1.50-1.70 → élargir plancher
    "max_odds":            3.20,  # MLB : outsiders réguliers à 2.60-3.20 → élargir plafond
    # ── Filtres O/U (calibration révèle surconfiance systématique -22 à -45%) ──
    "ou_shrink_factor":   0.65,  # Compresse prob O/U vers 50% (corrige surestimation)
    "min_edge_total":     0.08,  # Edge minimum 8% après shrinkage pour paris Total
    "kelly_frac_total":   0.40,  # Multiplicateur Kelly pour Total (0.10x effectif à 0.25x base)
    # ── Mode standard : accepte tous les marchés ──
    "ban_totals":         False,
    "downgrade_excellent": False,
    "min_edge_moneyline": 0.00,
}

# ─── Mode CONSERVATEUR (Phase 1 — Stop-Loss) ─────────────────────────────────
# Basé sur l'analyse historique MLB :
#   - Cotes 1.50-1.79 : 18% WR (catastrophe) → relever plancher à 1.80
#   - Totals O/U : 47.3% WR vs 52.4% breakeven → bannir (sauf edge très fort)
#   - Moneyline : 55% WR → focus
#   - "Excellent ***" : 44% WR, ROI -19$ → traiter comme suspect (downgrade)
#   - "Neutre" : 57.7% WR, ROI +3.63$ → préférer ces picks
THRESHOLDS_CONSERVATIVE = {
    "house_margin_low":    5.0,
    "house_margin_medium": 8.0,
    "value_neutral":       55,   # Barre plus haute pour pari qualifié
    "min_odds":            1.80,  # Bannir la zone 1.50-1.79 (18% WR)
    "max_odds":            2.80,  # Outsiders extrêmes moins fiables
    "ou_shrink_factor":    0.55,  # Plus agressif : compresse vers 50%
    "min_edge_total":      0.15,  # 15% edge minimum pour Total (vs 8%)
    "kelly_frac_total":    0.25,  # Kelly Total réduit davantage
    "ban_totals":          True,  # Bannir tous les totaux sauf edge ≥ 15%
    "downgrade_excellent": True,  # "Excellent" (surconfiance) → "Neutre"
    "min_edge_moneyline":  0.03,  # Au moins 3% edge pour moneyline
}


def _get_thresholds(mode: str = "standard") -> dict:
    """Retourne le dict de seuils selon le mode (standard ou conservateur)."""
    if mode == "conservative":
        return THRESHOLDS_CONSERVATIVE
    return THRESHOLDS


# ─── Moteur d'analyse ─────────────────────────────────────────────────────────

class OddsAnalyzer:
    """Analyse les cotes MLB et identifie les meilleures opportunités."""

    def analyze_matches(self, matches: list[Match]) -> list[AnalyzedMatch]:
        analyzed = []
        for match in matches:
            am = self._analyze_match(match)
            if am.analyzed_groups:   # inclure si au moins un groupe analysé
                analyzed.append(am)
        analyzed.sort(key=lambda x: x.overall_score, reverse=True)
        return analyzed

    def _analyze_match(self, match: Match) -> AnalyzedMatch:
        am = AnalyzedMatch(match=match)

        for group in match.bet_groups:
            ag = self._analyze_group(group)
            am.analyzed_groups.append(ag)
            for sel in ag.selections:
                if sel.value_score >= THRESHOLDS["value_neutral"]:
                    am.top_picks.append(sel)

        if am.top_picks:
            am.overall_score = sum(p.value_score for p in am.top_picks) / len(am.top_picks)
            am.top_picks.sort(key=lambda x: x.value_score, reverse=True)
            am.top_picks = am.top_picks[:3]

        return am

    def _analyze_group(self, group: BetGroup) -> AnalyzedBetGroup:
        selections = [s for s in group.selections
                      if s.odds >= THRESHOLDS["min_odds"]]

        if not selections:
            return AnalyzedBetGroup(bet_group=group, house_margin=0.0)

        implied_probs = [1.0 / s.odds for s in selections]
        total_implied = sum(implied_probs)
        house_margin  = (total_implied - 1.0) * 100

        if house_margin < 0:
            return AnalyzedBetGroup(bet_group=group, house_margin=0.0)

        fair_probs = [p / total_implied for p in implied_probs] if total_implied > 0 else [1.0 / len(selections)] * len(selections)

        ag = AnalyzedBetGroup(bet_group=group, house_margin=house_margin)

        for i, sel in enumerate(selections):
            implied_prob = implied_probs[i]
            fair_prob    = fair_probs[i]

            value_score = self._compute_value_score(
                odds=sel.odds,
                implied_prob=implied_prob,
                fair_prob=fair_prob,
                house_margin=house_margin,
                n_selections=len(selections),
            )
            edge           = fair_prob - implied_prob
            recommendation = self._classify(value_score, house_margin)

            ag.selections.append(AnalyzedSelection(
                selection=sel,
                implied_prob=implied_prob,
                fair_prob=fair_prob,
                edge=edge,
                value_score=value_score,
                recommendation=recommendation,
            ))

        if ag.selections:
            ag.best_value = max(ag.selections, key=lambda x: x.value_score)

        return ag

    def _compute_value_score(self, odds: float, implied_prob: float,
                               fair_prob: float, house_margin: float,
                               n_selections: int) -> float:
        """
        Score de valeur 0-100 basé sur l'espérance de valeur (EV).
        EV = fair_prob * (odds - 1) - (1 - fair_prob)
        Converti en score 0-100 centré sur EV=0 → 50.
        """
        ev = fair_prob * (odds - 1.0) - (1.0 - fair_prob)

        # EV normalisé : ±20% EV → score 0-100
        # EV = 0   → 50, EV = +0.20 → 100, EV = -0.20 → 0
        score = 50.0 + ev * 250.0

        # Pénalité légère si la marge bookmaker est très élevée (>15%)
        if house_margin > 15:
            score -= 10
        elif house_margin > 10:
            score -= 5

        return max(0.0, min(100.0, score))

    def _classify(self, value_score: float, house_margin: float) -> str:
        # value_score basé sur EV : 50 = EV neutre, >50 = EV positif
        # Seuils rehaussés : "Excellent" doit représenter <25% des picks, pas 78%
        # Calibration historique : picks "Excellent" gagnaient 44% (sous la moyenne)
        # Fallait EV ≥ 10% (score 75) pour filtrer les faux positifs
        if value_score >= 75 and house_margin < 10:
            return "Excellent ***"
        elif value_score >= 65:
            return "Bon **"
        elif value_score >= 50:
            return "Neutre *"
        else:
            return "Éviter"

    def get_top_opportunities(
        self,
        analyzed_matches: list[AnalyzedMatch],
        n: int = 10,
        bankroll: float = 1000.0,
        kelly_fraction: float = 0.25,
        info_mode: bool = False,
        mode: str = "standard",
    ) -> list[BettingOpportunity]:
        """
        info_mode=True : retourne toutes les prédictions sans les filtres O/U stricts.
        mode : "standard" (défaut) ou "conservative" (Phase 1 stop-loss).
        """
        TH = _get_thresholds(mode)
        """
        Extrait les N meilleures opportunités de paris MLB.
        Intègre les stats MLB, lanceurs, Statcast et Kelly Criterion.
        """
        _PERIOD_KW = ("manche", "5 premières", "5 premiers")
        # Marchés de props joueurs — on n'a pas de modèle pour ces paris
        _PLAYER_PROP_KW = (
            "coups sûrs", "retraits au bâton", "buts volés", "points produits",
            "coups de circuit", "bases sur balles", "strikeouts", "hits",
            "home runs", "stolen bases", "rbi", "walks", "total bases",
        )

        def _is_period_market(bt: str) -> bool:
            bt_l = bt.lower()
            return any(k in bt_l for k in _PERIOD_KW)

        def _is_player_prop(bt: str) -> bool:
            """Détecte les paris de props joueurs individuels."""
            bt_l = bt.lower()
            return any(k in bt_l for k in _PLAYER_PROP_KW)

        def _market_cat(bt: str) -> str:
            bt_l = bt.lower()
            if any(k in bt_l for k in ("gagnant", "victoire", "winner", "2 issues", "moneyline")):
                return "moneyline"
            if any(k in bt_l for k in ("total", "points", "plus/moins")):
                return "total"
            if any(k in bt_l for k in ("handicap", "écart", "run line")):
                return "runline"
            return bt_l

        opportunities = []

        for am in analyzed_matches:
            match = am.match

            # Info lanceurs partants
            pitcher_info = ""
            if match.home_pitcher or match.away_pitcher:
                hp = match.home_pitcher or "?"
                ap = match.away_pitcher or "?"
                pitcher_info = f"{ap} vs {hp}"

            for ag in am.analyzed_groups:
                if _is_period_market(ag.bet_group.bet_type):
                    continue
                if _is_player_prop(ag.bet_group.bet_type):
                    continue

                for sel in ag.selections:
                    if sel.selection.odds < TH["min_odds"]:
                        continue
                    if sel.selection.odds > TH["max_odds"]:
                        continue

                    # Catégorie du marché (total vs moneyline vs runline)
                    market = _market_cat(ag.bet_group.bet_type)
                    is_total = market == "total"
                    is_moneyline = market == "moneyline"

                    # ── Mode conservateur : bannir totaux sauf edge très fort ──
                    # (appliqué après calcul adjusted_fp plus bas via min_edge_total)
                    # Filtre moneyline : edge minimum
                    # (appliqué après calcul adjusted_fp plus bas)

                    # Ajustement avec stats MLB
                    try:
                        from mlb_stats import get_adjusted_prob
                        adjusted_fp = get_adjusted_prob(
                            home_team=match.home_team,
                            away_team=match.away_team,
                            bet_type=ag.bet_group.bet_type,
                            selection=sel.selection.label,
                            math_prob=sel.fair_prob,
                            match_date=match.date,
                        )
                    except Exception:
                        adjusted_fp = sel.fair_prob

                    # ── Correction calibration O/U ──────────────────────────────
                    # Calibration historique : le modèle sur-estime de 22-45% sur les
                    # paris Total. On compresse la probabilité vers 50% avant Kelly.
                    if is_total:
                        shrink = TH["ou_shrink_factor"]
                        adjusted_fp = 0.5 + (adjusted_fp - 0.5) * shrink

                    # ── Kelly Criterion ─────────────────────────────────────────
                    # info_mode : kelly normal (pas de réduction) pour affichage seulement
                    eff_kelly = kelly_fraction if info_mode else (
                        kelly_fraction * TH["kelly_frac_total"]
                        if is_total else kelly_fraction
                    )
                    try:
                        from kelly import kelly_fraction as _kf
                        kf = _kf(adjusted_fp, sel.selection.odds, eff_kelly)
                    except Exception:
                        kf = 0.0

                    # Filtrer les paris sans EV positif
                    if kf <= 0:
                        continue

                    # ── Filtre edge minimum pour Total O/U ──────────────────────
                    # Skippé en info_mode (on veut voir toutes les prédictions)
                    if is_total and not info_mode:
                        implied = sel.implied_prob
                        edge_vs_book = adjusted_fp - implied
                        if edge_vs_book < TH["min_edge_total"]:
                            continue

                    # ── Mode conservateur : filtre moneyline edge minimum ──────
                    if is_moneyline and not info_mode and TH.get("min_edge_moneyline", 0) > 0:
                        edge_ml = adjusted_fp - sel.implied_prob
                        if edge_ml < TH["min_edge_moneyline"]:
                            continue

                    # Recalculer value_score et recommendation sur adjusted_fp
                    adj_vs = self._compute_value_score(
                        odds=sel.selection.odds,
                        implied_prob=sel.implied_prob,
                        fair_prob=adjusted_fp,
                        house_margin=ag.house_margin,
                        n_selections=len(ag.selections),
                    )
                    adj_rec = self._classify(adj_vs, ag.house_margin)

                    # Mode conservateur : "Excellent" est suspect (44% WR historique)
                    # → downgrade vers "Bon" pour ne pas sur-parier ces picks
                    if TH.get("downgrade_excellent") and "Excellent" in adj_rec:
                        adj_rec = "Bon **"

                    opp = BettingOpportunity(
                        match=match,
                        bet_type=ag.bet_group.bet_type,
                        selection_label=sel.selection.label,
                        odds=sel.selection.odds,
                        prediction_id=sel.selection.prediction_id,
                        value_score=adj_vs,
                        recommendation=adj_rec,
                        house_margin=ag.house_margin,
                        fair_prob=adjusted_fp,
                        implied_prob=sel.implied_prob,
                        sport=match.sport,
                        league=match.league,
                        math_prob=sel.fair_prob,
                        kelly_fraction=kf,
                        pitcher_info=pitcher_info,
                    )
                    opportunities.append(opp)

        # Garder seulement les matchs d'aujourd'hui et du passé
        from datetime import date as _date
        today = _date.today().isoformat()
        opportunities = [o for o in opportunities
                         if (o.match.date or "9999-99-99") <= today]

        # Charger les multiplicateurs historiques
        try:
            from predictions import get_bet_type_multipliers, classify_bet_type
            _bt_mult = get_bet_type_multipliers(sport="baseball")
        except Exception:
            _bt_mult = {}
            def classify_bet_type(bt, h="", a=""):
                return ""

        def sort_key(o):
            d = o.match.date or "9999-99-99"
            priority = 0 if d == today else 2
            # Trier par Kelly fraction : capture l'EV ET les cotes (= rentabilité réelle)
            return (priority, d, -o.kelly_fraction)

        opportunities.sort(key=sort_key)

        # Déduplication : une seule sélection par match + catégorie de marché
        seen_market: dict = {}
        deduped = []
        for opp in opportunities:
            match_key = (
                opp.match.date,
                (opp.match.home_team or "").lower(),
                (opp.match.away_team or "").lower(),
                _market_cat(opp.bet_type),
            )
            if match_key not in seen_market:
                seen_market[match_key] = opp
                deduped.append(opp)

        # Diversité : séparer par catégorie de marché puis interleave
        by_cat: dict[str, list] = {}
        for opp in deduped:
            cat = _market_cat(opp.bet_type)
            by_cat.setdefault(cat, []).append(opp)

        # Interleave : alterner entre catégories pour garantir la diversité
        # Les moneylines et totals doivent être représentés
        diversified = []
        cat_iters = {cat: iter(opps) for cat, opps in by_cat.items()}
        while cat_iters:
            exhausted = []
            for cat in list(cat_iters.keys()):
                try:
                    diversified.append(next(cat_iters[cat]))
                except StopIteration:
                    exhausted.append(cat)
            for cat in exhausted:
                del cat_iters[cat]

        # Max 2 paris Excellent par match
        match_excellent_count: dict = {}
        neutre_type_count: dict     = {}
        neutre_cap = max(2, n // 6)
        final = []
        for opp in diversified:
            mk = (f"{opp.match.date}|"
                  f"{(opp.match.home_team or '').lower()}|"
                  f"{(opp.match.away_team or '').lower()}")
            if "Excellent" in opp.recommendation:
                if match_excellent_count.get(mk, 0) >= 2:
                    continue
                match_excellent_count[mk] = match_excellent_count.get(mk, 0) + 1
            elif "Neutre" in opp.recommendation:
                cat = classify_bet_type(opp.bet_type, opp.match.home_team or "", opp.match.away_team or "")
                if neutre_type_count.get(cat, 0) >= neutre_cap:
                    continue
                neutre_type_count[cat] = neutre_type_count.get(cat, 0) + 1
            final.append(opp)

        return final[:n]
