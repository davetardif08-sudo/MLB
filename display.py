"""
Interface d'affichage CLI pour le MLB Analyzer.
Utilise Rich pour un rendu coloré et structuré.
"""

import sys
import io

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.text import Text
from rich import box

from analyzer import AnalyzedMatch, BettingOpportunity, OddsAnalyzer
from scraper import Match

if sys.platform == "win32":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

console = Console(highlight=False, width=200)


REC_STYLE = {
    "Excellent ***": "bold green",
    "Bon **":        "green",
    "Neutre *":      "yellow",
    "Éviter":        "dim red",
}


def _odds_color(odds: float) -> str:
    if odds <= 1.60:
        return "cyan"
    elif odds <= 2.20:
        return "bright_white"
    elif odds <= 3.50:
        return "yellow"
    else:
        return "bright_yellow"


def _margin_color(margin: float) -> str:
    if margin < 5:
        return "green"
    elif margin < 8:
        return "yellow"
    else:
        return "red"


def _kelly_color(bet: float) -> str:
    if bet <= 0:
        return "dim"
    elif bet >= 50:
        return "bold green"
    elif bet >= 20:
        return "green"
    else:
        return "yellow"


def _score_bar(score: float, width: int = 10) -> str:
    filled = int(score / 100 * width)
    return "#" * filled + "-" * (width - filled)


def print_header():
    console.print()
    console.print(Panel.fit(
        "[bold #22c55e]MLB Analyzer[/bold #22c55e] [white]-- Prédictions & Mises[/white]\n"
        "[dim]Baseball MLB  |  Mise-O-Jeu (Loto-Québec)  |  Kelly Criterion[/dim]",
        border_style="#22c55e",
        padding=(1, 4),
    ))
    console.print()


def print_top_opportunities(
    opportunities: list[BettingOpportunity],
    title: str = "Meilleures Opportunités MLB",
    bankroll: float = 1000.0,
):
    """Affiche le tableau des meilleures opportunités avec colonne Kelly."""
    if not opportunities:
        console.print(Panel(
            "[yellow]Aucune opportunité trouvée.[/yellow]\n"
            "[dim]Vérifiez votre connexion ou réessayez plus tard.[/dim]",
            title=title,
            border_style="yellow",
        ))
        return

    from kelly import kelly_bet

    table = Table(
        title=title,
        box=box.ROUNDED,
        border_style="#22c55e",
        header_style="bold cyan",
        show_lines=True,
        padding=(0, 1),
    )

    table.add_column("#",             style="dim",       width=3,  justify="right")
    table.add_column("Match",         style="white",     min_width=26)
    table.add_column("Type de pari",  style="white",     min_width=22)
    table.add_column("Sélection",     style="bold",      min_width=14)
    table.add_column("Cote",          justify="center",  width=6)
    table.add_column("Marge",         justify="center",  width=7)
    table.add_column("Score",         justify="center",  width=13)
    table.add_column("Avis",          justify="center",  width=14)
    table.add_column("Kelly Mise",    justify="right",   width=11)

    for i, opp in enumerate(opportunities, 1):
        rec_style  = REC_STYLE.get(opp.recommendation, "white")
        odds_style = _odds_color(opp.odds)
        marg_style = _margin_color(opp.house_margin)
        bar        = _score_bar(opp.value_score)

        bet_amount = kelly_bet(opp.fair_prob, opp.odds, bankroll)
        kelly_str  = f"${bet_amount:.0f}" if bet_amount > 0 else "—"
        kelly_sty  = _kelly_color(bet_amount)

        match_str  = f"{opp.match.away_team} @ {opp.match.home_team}"
        date_str   = opp.display_date
        pitcher    = opp.pitcher_info
        bet_type   = opp.bet_type[:25] + ("…" if len(opp.bet_type) > 25 else "")
        sel_label  = opp.selection_label[:16]

        match_cell = Text()
        match_cell.append(match_str, style="white")
        match_cell.append(f"\n{date_str}", style="dim")
        if pitcher:
            match_cell.append(f"\n⚾ {pitcher}", style="dim cyan")

        table.add_row(
            str(i),
            match_cell,
            bet_type,
            sel_label,
            f"[{odds_style}]{opp.odds:.2f}[/{odds_style}]",
            f"[{marg_style}]{opp.house_margin:.1f}%[/{marg_style}]",
            f"[dim]{bar}[/dim] [bold]{opp.value_score:.0f}[/bold]",
            f"[{rec_style}]{opp.recommendation}[/{rec_style}]",
            f"[{kelly_sty}]{kelly_str}[/{kelly_sty}]",
        )

    console.print(table)
    console.print()


def print_kelly_summary(opportunities: list[BettingOpportunity], bankroll: float = 1000.0):
    """Affiche un résumé des mises Kelly."""
    from kelly import kelly_summary
    summary = kelly_summary(opportunities, bankroll=bankroll)

    bets = summary.get("bets", [])
    if not bets:
        return

    total_w  = summary["total_wagered"]
    total_ep = summary["expected_profit"]
    ep_color = "green" if total_ep > 0 else "red"
    ep_sign  = "+" if total_ep > 0 else ""

    text = (
        f"[bold]Bankroll :[/bold] ${bankroll:.0f}    "
        f"[bold]Total misé (Kelly 0.25x) :[/bold] ${total_w:.0f}    "
        f"[bold]Profit espéré :[/bold] [{ep_color}]{ep_sign}${total_ep:.2f}[/{ep_color}]    "
        f"[dim]({len(bets)} paris avec valeur positive)[/dim]"
    )
    console.print(Panel(text, title="Résumé Kelly Criterion", border_style="#22c55e"))
    console.print()


def print_match_detail(am: AnalyzedMatch):
    """Affiche le détail d'un match analysé."""
    match    = am.match
    title    = f"[MLB] {match.away_team} @ {match.home_team}"
    subtitle = f"[dim]{match.date} {match.time}[/dim]"

    content = Text()
    content.append(subtitle + "\n\n")

    if match.home_pitcher or match.away_pitcher:
        content.append(f"  Lanceurs : {match.away_pitcher or '?'} (V) vs "
                       f"{match.home_pitcher or '?'} (D)\n\n", style="cyan")

    for ag in am.analyzed_groups:
        if not ag.selections:
            continue
        content.append(f"  {ag.bet_group.bet_type}\n", style="bold cyan")
        content.append("  Marge : ", style="dim")
        content.append(f"{ag.house_margin:.1f}%\n", style=_margin_color(ag.house_margin))
        content.append("\n")

        for sel in sorted(ag.selections, key=lambda x: x.value_score, reverse=True):
            rec_style = REC_STYLE.get(sel.recommendation, "white")
            bar = _score_bar(sel.value_score, width=8)
            content.append(
                f"    [{bar}] {sel.value_score:.0f}  "
                f"{sel.selection.label:<18}  "
                f"Cote: {sel.selection.odds:.2f}  "
                f"({sel.fair_prob*100:.1f}% juste)  ",
                style="white",
            )
            content.append(f"{sel.recommendation}\n", style=rec_style)
        content.append("\n")

    console.print(Panel(content, title=title, border_style="#22c55e"))


def print_summary(opps: list[BettingOpportunity], total_matches: int, bankroll: float = 1000.0):
    """Affiche un résumé de la session."""
    from kelly import kelly_bet
    n_excellent = sum(1 for o in opps if "Excellent" in o.recommendation)
    n_bon       = sum(1 for o in opps if o.recommendation.startswith("Bon"))
    total_kelly = sum(kelly_bet(o.fair_prob, o.odds, bankroll) for o in opps)

    text = (
        f"[bold]Matchs analysés :[/bold] {total_matches}    "
        f"[bold]Paris trouvés :[/bold] {len(opps)}  "
        f"([bold green]{n_excellent} Excellent[/bold green] + "
        f"[green]{n_bon} Bon[/green])    "
        f"[bold]Total Kelly :[/bold] [#22c55e]${total_kelly:.0f}[/#22c55e] "
        f"[dim](sur ${bankroll:.0f} bankroll)[/dim]"
    )
    console.print(Panel(text, title="Résumé", border_style="green"))
    console.print()


def print_legend():
    """Affiche la légende."""
    legend = Table(box=box.SIMPLE, show_header=False, padding=(0, 2))
    legend.add_column(style="bold")
    legend.add_column()

    legend.add_row("[bold green]Excellent ***[/bold green]",
                   "Score >= 70, faible marge maison — Paris fortement recommandé")
    legend.add_row("[green]Bon **[/green]",
                   "Score 50-69 — Paris intéressant à considérer")
    legend.add_row("[yellow]Neutre *[/yellow]",
                   "Score 30-49 — Ni bon ni mauvais")
    legend.add_row("[dim red]Éviter[/dim red]",
                   "Score < 30 ou forte marge — Non recommandé")
    legend.add_row("[#22c55e]Kelly Mise[/#22c55e]",
                   "Mise suggérée (quart Kelly 0.25x) pour une bankroll de $1000")
    legend.add_row("[cyan]Cote optimale[/cyan]",
                   "Zone 1.60 - 2.80 : meilleur rapport risque/rendement en MLB")

    console.print(Panel(legend, title="Légende", border_style="dim", padding=(0, 1)))
