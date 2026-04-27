"""
MLB Analyzer — Point d'entrée CLI

Usage :
    python main.py                    # Analyse complète
    python main.py --top 15           # Afficher le top 15 (défaut: 10)
    python main.py --bankroll 500     # Bankroll pour calcul Kelly (défaut: 1000)
    python main.py --kelly 0.5        # Fraction Kelly (défaut: 0.25 = quart Kelly)
    python main.py --detail           # Détail par match
    python main.py --visible          # Navigateur visible (debug)
    python main.py --demo             # Mode démo sans scraper
"""

import argparse
import sys
from rich.console import Console
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn

console = Console()


def parse_args():
    parser = argparse.ArgumentParser(
        description="MLB Analyzer — Prédictions & Mises (Mise-O-Jeu)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--top",      type=int,   default=10,   help="Nombre de paris à afficher")
    parser.add_argument("--bankroll", type=float, default=1000, help="Bankroll pour calcul Kelly ($)")
    parser.add_argument("--kelly",    type=float, default=0.25, help="Fraction Kelly (0.25 = quart Kelly)")
    parser.add_argument("--detail",   action="store_true",      help="Afficher le détail par match")
    parser.add_argument("--visible",  action="store_true",      help="Navigateur visible (debug)")
    parser.add_argument("--demo",     action="store_true",      help="Mode démo avec données d'exemple")
    return parser.parse_args()


# ─── Données de démo ──────────────────────────────────────────────────────────

def generate_demo_data():
    """Génère des données d'exemple réalistes pour la MLB."""
    from scraper import Match, BetGroup, Selection

    return [
        Match(
            sport="baseball", league="MLB",
            home_team="New York Yankees", away_team="Boston Red Sox",
            date="2026-04-03", time="19:05", event_id="demo1",
            home_pitcher="Gerrit Cole", away_pitcher="Brayan Bello",
            event_url="",
            bet_groups=[
                BetGroup(
                    bet_type="Gagnant du match – 2 issues",
                    selections=[
                        Selection(label="New York Yankees", odds=1.75, prediction_id="m1001"),
                        Selection(label="Boston Red Sox",   odds=2.10, prediction_id="m1002"),
                    ]
                ),
                BetGroup(
                    bet_type="Total de points – Plus/Moins",
                    selections=[
                        Selection(label="Plus de 8.5",  odds=1.90, prediction_id="m1003"),
                        Selection(label="Moins de 8.5", odds=1.90, prediction_id="m1004"),
                    ]
                ),
            ]
        ),
        Match(
            sport="baseball", league="MLB",
            home_team="Los Angeles Dodgers", away_team="San Francisco Giants",
            date="2026-04-03", time="22:10", event_id="demo2",
            home_pitcher="Tyler Glasnow", away_pitcher="Logan Webb",
            event_url="",
            bet_groups=[
                BetGroup(
                    bet_type="Gagnant du match – 2 issues",
                    selections=[
                        Selection(label="Los Angeles Dodgers", odds=1.55, prediction_id="m2001"),
                        Selection(label="San Francisco Giants", odds=2.55, prediction_id="m2002"),
                    ]
                ),
                BetGroup(
                    bet_type="Total de points – Plus/Moins",
                    selections=[
                        Selection(label="Plus de 7.5",  odds=1.85, prediction_id="m2003"),
                        Selection(label="Moins de 7.5", odds=1.95, prediction_id="m2004"),
                    ]
                ),
            ]
        ),
        Match(
            sport="baseball", league="MLB",
            home_team="Atlanta Braves", away_team="New York Mets",
            date="2026-04-03", time="19:20", event_id="demo3",
            home_pitcher="Spencer Strider", away_pitcher="Kodai Senga",
            event_url="",
            bet_groups=[
                BetGroup(
                    bet_type="Gagnant du match – 2 issues",
                    selections=[
                        Selection(label="Atlanta Braves", odds=1.80, prediction_id="m3001"),
                        Selection(label="New York Mets",  odds=2.00, prediction_id="m3002"),
                    ]
                ),
                BetGroup(
                    bet_type="Total de points – Plus/Moins",
                    selections=[
                        Selection(label="Plus de 7.0",  odds=1.88, prediction_id="m3003"),
                        Selection(label="Moins de 7.0", odds=1.92, prediction_id="m3004"),
                    ]
                ),
            ]
        ),
        Match(
            sport="baseball", league="MLB",
            home_team="Houston Astros", away_team="Texas Rangers",
            date="2026-04-03", time="20:10", event_id="demo4",
            home_pitcher="Framber Valdez", away_pitcher="Nathan Eovaldi",
            event_url="",
            bet_groups=[
                BetGroup(
                    bet_type="Gagnant du match – 2 issues",
                    selections=[
                        Selection(label="Houston Astros", odds=1.85, prediction_id="m4001"),
                        Selection(label="Texas Rangers",  odds=1.95, prediction_id="m4002"),
                    ]
                ),
            ]
        ),
        Match(
            sport="baseball", league="MLB",
            home_team="Chicago Cubs", away_team="Milwaukee Brewers",
            date="2026-04-04", time="14:20", event_id="demo5",
            home_pitcher="Justin Steele", away_pitcher="Freddy Peralta",
            event_url="",
            bet_groups=[
                BetGroup(
                    bet_type="Gagnant du match – 2 issues",
                    selections=[
                        Selection(label="Chicago Cubs",     odds=2.05, prediction_id="m5001"),
                        Selection(label="Milwaukee Brewers", odds=1.80, prediction_id="m5002"),
                    ]
                ),
                BetGroup(
                    bet_type="Total de points – Plus/Moins",
                    selections=[
                        Selection(label="Plus de 8.0",  odds=1.92, prediction_id="m5003"),
                        Selection(label="Moins de 8.0", odds=1.88, prediction_id="m5004"),
                    ]
                ),
            ]
        ),
        Match(
            sport="baseball", league="MLB",
            home_team="Toronto Blue Jays", away_team="Baltimore Orioles",
            date="2026-04-04", time="15:07", event_id="demo6",
            home_pitcher="Kevin Gausman", away_pitcher="Corbin Burnes",
            event_url="",
            bet_groups=[
                BetGroup(
                    bet_type="Gagnant du match – 2 issues",
                    selections=[
                        Selection(label="Toronto Blue Jays",  odds=2.15, prediction_id="m6001"),
                        Selection(label="Baltimore Orioles",  odds=1.70, prediction_id="m6002"),
                    ]
                ),
            ]
        ),
    ]


# ─── Main ─────────────────────────────────────────────────────────────────────

def main():
    args = parse_args()

    from display import (
        print_header, print_top_opportunities, print_kelly_summary,
        print_match_detail, print_summary, print_legend,
    )
    from analyzer import OddsAnalyzer

    print_header()

    # ── Chargement des données ─────────────────────────────────────────────
    all_matches = []

    if args.demo:
        console.print("[bold yellow]Mode DÉMO[/bold yellow] — données d'exemple\n")
        all_matches = generate_demo_data()
    else:
        console.print("[bold]Scraping de miseojeu.lotoquebec.com (MLB)…[/bold]\n")
        with Progress(
            SpinnerColumn(),
            TextColumn("[progress.description]{task.description}"),
            TimeElapsedColumn(),
            console=console,
        ) as progress:
            task = progress.add_task("Chargement des cotes MLB...", total=None)
            try:
                from scraper import scrape_sync
                all_matches = scrape_sync(headless=not args.visible)
            except Exception as e:
                progress.stop()
                console.print(f"\n[red]Erreur scraping :[/red] {e}")
                console.print("[yellow]Conseil : --demo pour tester avec données d'exemple[/yellow]")
                sys.exit(1)
            progress.update(task, description="Scraping terminé ✓")

    if not all_matches:
        console.print("[red]Aucun match MLB trouvé.[/red]")
        console.print("[dim]Conseil : --demo pour voir un exemple[/dim]")
        sys.exit(0)

    console.print(f"\n[green]{len(all_matches)} match(s) MLB chargé(s)[/green]\n")

    # ── Analyse ───────────────────────────────────────────────────────────
    analyzer = OddsAnalyzer()
    analyzed = analyzer.analyze_matches(all_matches)
    opps     = analyzer.get_top_opportunities(
        analyzed,
        n=args.top,
        bankroll=args.bankroll,
        kelly_fraction=args.kelly,
    )

    # ── Affichage ─────────────────────────────────────────────────────────
    print_top_opportunities(opps, title=f"Top {args.top} Paris MLB ⚾", bankroll=args.bankroll)
    print_kelly_summary(opps, bankroll=args.bankroll)

    if args.detail:
        console.print("\n[bold]Détail par match :[/bold]\n")
        for am in analyzed:
            if am.top_picks:
                print_match_detail(am)

    print_summary(opps, len(all_matches), bankroll=args.bankroll)
    print_legend()

    console.print("[dim]⚠  Jouer comporte des risques. Jouez de façon responsable.[/dim]\n")


if __name__ == "__main__":
    main()
