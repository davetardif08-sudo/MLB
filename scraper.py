"""
Scraper pour miseojeu.lotoquebec.com — Sport MLB (Baseball)

Architecture :
  1. Charger la page liste : /fr/offre-de-paris/baseball/mlb/matchs?idAct=10
  2. Extraire tous les liens de match (idEve=ID)
  3. Pour chaque match, charger la page détail : ?idEve=ID
  4. Parser le innerText de la page pour extraire marchés + cotes

Avantages vs miseojeuplus.espacejeux.com :
  - HTML statique, pas de SPA React
  - Pas de géo-blocage sur baseball
  - Plus rapide (pas d'API REST séparée)
  - Format des cotes simple (virgule → conversion en point)
"""

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional
from playwright.async_api import async_playwright

import requests as _requests_mod


# --- Structures de données ---------------------------------------------------

@dataclass
class Selection:
    label: str
    odds: float
    prediction_id: str


@dataclass
class BetGroup:
    bet_type: str
    selections: list['Selection'] = field(default_factory=list)


@dataclass
class Match:
    sport: str          # "baseball"
    league: str         # "MLB"
    home_team: str
    away_team: str
    date: str           # YYYY-MM-DD heure locale (Montréal)
    time: str           # HH:MM heure locale
    event_id: str
    home_pitcher: str = ""
    away_pitcher: str = ""
    event_url: str = ""
    bet_groups: list['BetGroup'] = field(default_factory=list)


# --- Configuration -----------------------------------------------------------

BASE_URL = "https://miseojeu.lotoquebec.com"
LIST_URL = f"{BASE_URL}/fr/offre-de-paris/baseball/mlb/matchs?idAct=10"
MATCH_URL_TEMPLATE = f"{BASE_URL}/fr/offre-de-paris/baseball/mlb/matchs?idEve={{eid}}"

# Marchés à GARDER (titre exact ou contenu)
MARKET_KEYWORDS_KEEP = [
    "gagnant du match",                    # Moneyline + Run Line
    "total de points dans le match",       # Over/Under runs
    "total de coups sûrs dans le match",   # Hits over/under
]

# Marchés à EXCLURE (manches isolées, props joueurs)
MARKET_KEYWORDS_EXCLUDE = [
    "première manche",
    "1re manche",
    "5 premières manches",
    "retraits au bâton",
    "total de buts",        # props joueurs (home runs / total bases)
    "circuit",
    "coup sûr de",          # props joueurs
    "joueur",
]


# --- Utilitaires -------------------------------------------------------------

def _odds_to_float(s: str) -> Optional[float]:
    """Convertit une cote française (1,95) en float (1.95)."""
    s = s.strip().replace(',', '.')
    try:
        v = float(s)
        if v <= 1.0 or v > 50.0:
            return None
        return v
    except ValueError:
        return None


def _normalize_market_name(name: str) -> str:
    """Normalise les noms de marchés pour le matching."""
    return name.lower().strip()


def _should_keep_market(name: str) -> bool:
    """Détermine si un marché doit être inclus."""
    n = _normalize_market_name(name)

    # Exclusions prioritaires
    for kw in MARKET_KEYWORDS_EXCLUDE:
        if kw in n:
            return False

    # Inclusions
    for kw in MARKET_KEYWORDS_KEEP:
        if kw in n:
            return True

    return False


def _team_name_clean(name: str) -> str:
    """Nettoie un nom d'équipe (enlève suffixes type -T.B, -CLE)."""
    # Enlever les suffixes -XXX
    name = re.sub(r'-[A-Z]{1,4}(\.[A-Z])?$', '', name).strip()
    # Nettoyer accents corrompus
    name = name.replace('\ufffd', 'e')
    return name


def _normalize_team_name(name: str) -> str:
    """
    Normalise un nom d'équipe Loto-Québec en nom complet MLB.
    Loto-Québec utilise des noms courts (Tampa Bay, Saint Louis, etc.)
    """
    name = name.strip()
    # Mapping pour les noms ambigus avec suffixes
    short_to_full = {
        "Tampa Bay":      "Tampa Bay (Rays)",
        "Cleveland":      "Cleveland (Guardians)",
        "Saint Louis":    "Saint-Louis (Cardinals)",
        "Pittsburgh":     "Pittsburgh (Pirates)",
        "Boston":         "Boston (Red Sox)",
        "Toronto":        "Toronto (Blue Jays)",
        "Los Angeles-A":  "Los Angeles (Angels)",
        "Chicago-WS":     "Chicago (White Sox)",
        "Seattle":        "Seattle (Mariners)",
        "Minnesota":      "Minnesota (Twins)",
        "New York-Y":     "New York (Yankees)",
        "Texas":          "Texas (Rangers)",
        "Chicago-C":      "Chicago (Cubs)",
        "San Diego":      "San Diego (Padres)",
        "Miami":          "Miami (Marlins)",
        "Los Angeles-D":  "Los Angeles (Dodgers)",
        "New York-M":     "New York (Mets)",
        "Atlanta":        "Atlanta (Braves)",
        "Philadelphia":   "Philadelphia (Phillies)",
        "Washington":     "Washington (Nationals)",
        "Milwaukee":      "Milwaukee (Brewers)",
        "Cincinnati":     "Cincinnati (Reds)",
        "Detroit":        "Detroit (Tigers)",
        "Kansas City":    "Kansas City (Royals)",
        "Houston":        "Houston (Astros)",
        "Oakland":        "Oakland (Athletics)",
        "Sacramento":     "Sacramento (Athletics)",
        "Athletics":      "Oakland (Athletics)",
        "Colorado":       "Colorado (Rockies)",
        "Arizona":        "Arizona (Diamondbacks)",
        "San Francisco":  "San Francisco (Giants)",
        "Baltimore":      "Baltimore (Orioles)",
        "Chicago":        "Chicago (Cubs)",  # Si pas de suffixe
    }

    return short_to_full.get(name, name)


# --- Parsing d'une page de match --------------------------------------------

def _parse_match_page_text(text: str, event_id: str, event_url: str,
                            list_data: dict) -> Optional[Match]:
    """
    Parse le innerText d'une page de match pour extraire toutes les cotes.

    list_data contient les infos venant de la page liste:
        {'home_team', 'away_team', 'time', 'date'}
    """
    lines = [l.strip() for l in text.split('\n') if l.strip()]

    # Trouver l'indice du titre du match (format: "Team1 c. Team2")
    match_title_idx = -1
    title_pattern = re.compile(r'^(.+?)\s+c\.\s+(.+)$')
    teams_from_title = None
    date_from_page = None

    for i, line in enumerate(lines):
        m = title_pattern.match(line)
        if m and len(line) < 80 and 'paris' not in line.lower():
            teams_from_title = (m.group(1).strip(), m.group(2).strip())
            match_title_idx = i
            # La date est généralement la ligne suivante
            if i + 1 < len(lines):
                date_line = lines[i + 1]
                if re.match(r'^\d{4}-\d{2}-\d{2}', date_line):
                    date_from_page = date_line[:10]
            break

    if match_title_idx == -1 or not teams_from_title:
        return None

    away_short, home_short = teams_from_title
    away_team = _normalize_team_name(away_short)
    home_team = _normalize_team_name(home_short)

    # Construire le Match
    match = Match(
        sport="baseball",
        league="MLB",
        home_team=home_team,
        away_team=away_team,
        date=date_from_page or list_data.get('date', ''),
        time=list_data.get('time', ''),
        event_id=event_id,
        event_url=event_url,
    )

    # --- Parser les marchés ---
    # Pattern: chaque marché a un titre, suivi d'une heure HH:MM, puis de paires (label, cote)
    # Le marché finit quand on rencontre un nouveau titre (ligne sans virgule décimale et sans HH:MM)

    time_re = re.compile(r'^\d{1,2}:\d{2}$')
    odds_re = re.compile(r'^\d+,\d{2}$')

    i = match_title_idx + 1
    current_market_name = None
    current_selections: list[tuple[str, float]] = []
    seen_market_names = set()

    def flush_market():
        if current_market_name and current_selections:
            if _should_keep_market(current_market_name):
                # Dédup par nom de marché
                key = _normalize_market_name(current_market_name)
                if key not in seen_market_names:
                    seen_market_names.add(key)
                    grp = BetGroup(bet_type=current_market_name)
                    for label, odds in current_selections:
                        # Pour Plus de / Moins de : enlever ", par X pt(s)" si présent
                        clean_label = label.strip()
                        # Pour Run Line : nettoyer
                        grp.selections.append(Selection(
                            label=clean_label,
                            odds=odds,
                            prediction_id=f"{event_id}_{key}_{clean_label}",
                        ))
                    if len(grp.selections) >= 2:
                        match.bet_groups.append(grp)

    while i < len(lines):
        line = lines[i]

        # Une heure HH:MM marque le début des sélections d'un marché
        if time_re.match(line):
            i += 1
            continue

        # Une cote numérique avec virgule
        if odds_re.match(line):
            # On devrait avoir un label avant cette cote
            if i > 0 and current_selections is not None and current_market_name:
                # Le label est la ligne précédente
                label = lines[i - 1] if i - 1 < len(lines) else ""
                # Vérifier que le label n'est pas une heure ou une cote
                if not time_re.match(label) and not odds_re.match(label) and label:
                    odds_val = _odds_to_float(line)
                    if odds_val is not None:
                        # Éviter les doublons (label déjà ajouté)
                        if not current_selections or current_selections[-1][0] != label or current_selections[-1][1] != odds_val:
                            current_selections.append((label, odds_val))
            i += 1
            continue

        # Une ligne sans cote et sans heure : potentiellement un nouveau titre de marché
        # On flush le marché en cours et on commence un nouveau
        # Heuristique : doit contenir un mot-clé typique ET ne pas être un nom d'équipe simple
        line_lower = line.lower()
        market_keywords = (
            'gagnant', 'total de points', 'total de coups',
            'qui gagnera', 'qui effectuera', 'qui accumulera',
            'quelle équipe', 'écart additionnel', 'total additionnel',
            'point dans le match', 'manche', 'retraits au bâton',
            'total de buts', 'imprimer'
        )
        # Vérifier si la ligne contient un mot-clé de marché ET fait au moins 12 chars
        # (ou contient "match" + au moins 12 chars)
        has_keyword = any(kw in line_lower for kw in market_keywords)
        is_match_phrase = 'match' in line_lower and len(line) >= 12

        if (has_keyword or is_match_phrase) and len(line) >= 12:
            flush_market()
            current_market_name = line
            current_selections = []

        i += 1

    # Flush le dernier marché
    flush_market()

    if not match.bet_groups:
        return None

    return match


# --- Scraper principal -------------------------------------------------------

class MiseOJeuMLBScraper:
    def __init__(self, headless: bool = True):
        self.headless = headless

    async def scrape(self) -> list[Match]:
        """Scrape les événements MLB de Mise-O-Jeu Loto-Québec."""
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.headless,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',
                    '--no-sandbox',
                ]
            )
            context = await browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/122.0.0.0 Safari/537.36"
                ),
                locale="fr-CA",
                viewport={"width": 1280, "height": 900},
            )

            # --- Étape 1 : page liste, extraire les IDs ---
            print("  >> Chargement de la liste des matchs MLB...")
            page = await context.new_page()
            try:
                await page.goto(LIST_URL, wait_until='domcontentloaded', timeout=35000)
            except Exception as e:
                print(f"  >> Timeout liste: {type(e).__name__}")

            try:
                await page.wait_for_selector('a[href*="idEve="]', timeout=15000)
            except Exception:
                print("  >> Aucun lien idEve trouvé après 15s")

            await asyncio.sleep(2)

            # Extraire les matchs avec leurs infos de base
            list_data = await page.evaluate(r'''() => {
                const all = document.querySelectorAll('a[href*="idEve="]');
                const seen = new Set();
                const result = [];
                for (const a of all) {
                    const m = a.href.match(/idEve=(\d+)/);
                    if (!m || seen.has(m[1])) continue;
                    seen.add(m[1]);
                    // Trouver le contexte parent contenant équipes + heure + cotes
                    let parent = a;
                    for (let i = 0; i < 6 && parent; i++) parent = parent.parentElement;
                    const txt = parent ? (parent.innerText || '') : '';
                    result.push({id: m[1], href: a.href, context: txt});
                }
                return result;
            }''')

            print(f"     {len(list_data)} matchs trouvés sur la liste")

            await page.close()

            if not list_data:
                await browser.close()
                return []

            # Parser les contextes pour récupérer équipes + heure
            today = datetime.now().strftime('%Y-%m-%d')
            list_info = {}
            for item in list_data:
                eid = item['id']
                ctx = item['context']
                # Extraire heure (HH:MM)
                time_match = re.search(r'(\d{1,2}:\d{2})', ctx)
                time_str = time_match.group(1) if time_match else ""

                # Extraire 2 équipes : on cherche les lignes avant des cotes
                # Pattern : Équipe \n cote \n Équipe2 \n cote
                lines = [l.strip() for l in ctx.split('\n') if l.strip()]
                away = home = ""
                for j, l in enumerate(lines):
                    if re.match(r'^\d+,\d{2}$', l) and j >= 1:
                        candidate = lines[j-1]
                        if not re.match(r'^\d+[:,]\d+', candidate) and not re.match(r'^\d+\s*paris', candidate):
                            if not away:
                                away = candidate
                            elif not home and candidate != away:
                                home = candidate
                                break

                list_info[eid] = {
                    'away_team': _normalize_team_name(away),
                    'home_team': _normalize_team_name(home),
                    'time': time_str,
                    'date': today,
                }

            # --- Étape 2 : récupérer les pages détail en parallèle ---
            # On utilise plusieurs onglets Playwright en parallèle (pas Python requests
            # car le site bloque les SSL handshakes sans navigateur)
            print(f"  >> Récupération des cotes pour {len(list_data)} matchs...")
            tasks = []
            for item in list_data:
                eid = item['id']
                tasks.append(self._fetch_match_page(context, eid, item['href'], list_info[eid]))

            results = await asyncio.gather(*tasks, return_exceptions=True)
            await browser.close()

            matches = []
            for r in results:
                if isinstance(r, Match):
                    print(f"     {r.away_team} @ {r.home_team} - {len(r.bet_groups)} marchés")
                    matches.append(r)
                elif isinstance(r, Exception):
                    print(f"  >> Erreur match: {r}")

            return matches

    async def _fetch_match_page(self, context, event_id: str, event_url: str,
                                 list_data: dict) -> Optional[Match]:
        """Charge une page de match et parse les cotes."""
        page = await context.new_page()
        try:
            await page.goto(event_url, wait_until='domcontentloaded', timeout=25000)
            await asyncio.sleep(1.5)
            text = await page.evaluate("() => document.body.innerText")
            return _parse_match_page_text(text, event_id, event_url, list_data)
        except Exception as e:
            print(f"    [!] Erreur match {event_id}: {type(e).__name__}")
            return None
        finally:
            await page.close()


def scrape_sync(headless: bool = True) -> list[Match]:
    """Point d'entrée synchrone."""
    scraper = MiseOJeuMLBScraper(headless=headless)
    return asyncio.run(scraper.scrape())
