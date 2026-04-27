"""
Kelly Criterion — Calcul des mises optimales.

Formule de Kelly : f* = (bp - q) / b
  - b = cotes nettes (odds - 1)
  - p = probabilité estimée de gagner
  - q = 1 - p (probabilité de perdre)
  - f* = fraction du bankroll à miser

Note : La mise Kelly complète est agressive. On utilise une fraction réduite
(fractional Kelly) pour limiter le risque. Recommandé : 0.25x (quart Kelly).

Exemple :
  - Cote : 2.10 (b = 1.10)
  - Probabilité estimée : 52% (p = 0.52)
  - Kelly complet : f* = (1.10 × 0.52 - 0.48) / 1.10 = 10.9%
  - Quart Kelly (0.25x) : 2.7% du bankroll
"""


def kelly_fraction(prob: float, odds: float, fraction: float = 0.25) -> float:
    """
    Calcule la fraction Kelly ajustée du bankroll à miser.

    Args:
        prob     : Probabilité estimée de gagner (0-1)
        odds     : Cotes décimales (ex: 2.10)
        fraction : Facteur réducteur Kelly (défaut 0.25 = quart Kelly)

    Returns:
        Fraction du bankroll (0-1). Retourne 0 si pas de valeur positive.
    """
    if prob <= 0 or prob >= 1 or odds <= 1:
        return 0.0

    b = odds - 1.0  # cotes nettes
    q = 1.0 - prob

    full_kelly = (b * prob - q) / b

    # Si Kelly est négatif, il ne faut pas parier
    if full_kelly <= 0:
        return 0.0

    adjusted = full_kelly * fraction
    # Plafonner à 10% du bankroll même avec Kelly
    return round(min(adjusted, 0.10), 4)


def kelly_bet(prob: float, odds: float,
              bankroll: float = 1000.0,
              fraction: float = 0.25,
              max_nightly: float = None) -> float:
    """
    Calcule la mise recommandée en dollars (sans plafond nightly ici —
    le plafond global est appliqué dans kelly_allocate() sur la liste complète).

    Args:
        prob        : Probabilité estimée (0-1)
        odds        : Cotes décimales
        bankroll    : Montant total du bankroll en $
        fraction    : Facteur Kelly (0.25 = quart Kelly conservateur)
        max_nightly : Ignoré ici — utiliser kelly_allocate() pour le plafond total

    Returns:
        Mise brute en $ (non plafonnée individuellement).
    """
    kf = kelly_fraction(prob, odds, fraction)
    if kf <= 0:
        return 0.0
    return bankroll * kf


def kelly_allocate(bets_raw: list[float], max_nightly: float) -> list[float]:
    """
    Distribue proportionnellement un budget total entre plusieurs mises Kelly.

    Si la somme des mises brutes dépasse max_nightly, chaque mise est
    réduite au prorata pour que le total = max_nightly.

    Args:
        bets_raw    : Mises brutes calculées par kelly_bet() (peut contenir des 0)
        max_nightly : Budget total maximal pour la soirée en $

    Returns:
        Liste de mises allouées, arrondies à 1$, même longueur que bets_raw.
    """
    total = sum(bets_raw)
    if total <= 0:
        return [0.0] * len(bets_raw)
    if max_nightly is None or max_nightly <= 0:
        return [round(max(1.0, b)) if b > 0 else 0.0 for b in bets_raw]
    # Plafonner seulement si le total brut dépasse le budget
    # Si Kelly brut < budget, conserver les proportions naturelles
    budget = round(max_nightly)
    scale = budget / total

    # Calcul des valeurs scalées avec partie entière et fractionnaire
    scaled_values = []
    for i, b in enumerate(bets_raw):
        if b > 0:
            s = b * scale
            floored = int(s)
            frac = s - floored
            scaled_values.append((i, floored, frac))

    result = [0.0] * len(bets_raw)
    for i, f, _ in scaled_values:
        result[i] = float(f)

    # Distribuer les dollars restants aux paris avec la plus grande partie fractionnaire
    current_total = sum(f for _, f, _ in scaled_values)
    remainder = budget - current_total
    for i, _, _ in sorted(scaled_values, key=lambda x: x[2], reverse=True)[:remainder]:
        result[i] += 1.0

    return result


def kelly_summary(opportunities: list, bankroll: float = 1000.0,
                   fraction: float = 0.25, max_nightly: float = None) -> dict:
    """
    Calcule un résumé Kelly pour une liste d'opportunités.

    Returns:
        dict avec total_wagered, expected_profit, top_bets
    """
    bets = []
    for opp in opportunities:
        bet_amount = kelly_bet(opp.fair_prob, opp.odds, bankroll, fraction, max_nightly)
        if bet_amount > 0:
            expected_profit = bet_amount * (opp.fair_prob * (opp.odds - 1) - (1 - opp.fair_prob))
            bets.append({
                "match":      f"{opp.match.away_team} @ {opp.match.home_team}",
                "selection":  opp.selection_label,
                "odds":       opp.odds,
                "fair_prob":  opp.fair_prob,
                "bet_amount": bet_amount,
                "potential":  round(bet_amount * opp.odds, 2),
                "exp_profit": round(expected_profit, 2),
            })

    total_wagered  = sum(b["bet_amount"] for b in bets)
    total_expected = sum(b["exp_profit"] for b in bets)

    return {
        "bankroll":      bankroll,
        "kelly_fraction": fraction,
        "total_wagered": round(total_wagered, 2),
        "expected_profit": round(total_expected, 2),
        "bets":          sorted(bets, key=lambda x: x["bet_amount"], reverse=True),
    }


def ev_positive(prob: float, odds: float) -> bool:
    """Retourne True si le pari a une espérance de valeur positive."""
    if prob <= 0 or odds <= 1:
        return False
    return prob * (odds - 1) - (1 - prob) > 0


def edge_percent(prob: float, odds: float) -> float:
    """Calcule l'avantage en % (EV / mise)."""
    if odds <= 1:
        return 0.0
    return round((prob * (odds - 1) - (1 - prob)) * 100, 2)
