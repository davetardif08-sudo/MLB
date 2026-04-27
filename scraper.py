"""
Scraper pour miseojeuplus.espacejeux.com — Sport MLB (Baseball)
Réutilise le même pattern que l'app NHL/NBA.

Architecture :
  1. La page d'accueil contient des JSON-LD schema.org/SportsEvent avec les event IDs
  2. L'API content-service retourne les marchés et cotes pour chaque événement
  3. Endpoint : content.mojp-sgdigital-jel.com/content-service/api/v1/q/events-by-ids
  4. Sport : baseball MLB
"""

import asyncio
import re
import json
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
    home_pitcher: str = ""  # Lanceur partant domicile (si disponible)
    away_pitcher: str = ""  # Lanceur partant visiteur
    event_url: str = ""
    bet_groups: list['BetGroup'] = field(default_factory=list)


# --- Configuration -----------------------------------------------------------

BASE_SITE     = "https://miseojeuplus.espacejeux.com/sports/fr/"
BASE_SITE_MLB = "https://miseojeuplus.espacejeux.com/sports/fr/baseball/amerique-du-nord/mlb"
API_BASE  = "https://content.mojp-sgdigital-jel.com/content-service/api/v1/q"
API_PARAMS = (
    "includeChildMarkets=true"
    "&includeCollections=true"
    "&includePriorityCollectionChildMarkets=true"
    "&includePriceHistory=false"
    "&includeCommentary=false"
    "&includeIncidents=false"
    "&includeRace=false"
    "&includeMedia=false"
    "&includePools=false"
    "&includeNonFixedOdds=false"
    "&lang=fr-CA"
    "&channel=I"
)

# Types de marchés à inclure (focus sur match + over/under + run line)
MARKET_GROUPS_WANTED = {
    "MATCH_RESULT_WIN_DRAW_WIN",
    "WIN_DRAW_WIN",
    "MATCH_WINNER",
    "MATCH_RESULT",
    "WINNER_2_WAY",
    "TOTAL_GOALS",       # utilisé aussi pour baseball (points/runs)
    "GOALS_OVER_UNDER",
    "OVER_UNDER",
    "MATCH_HANDICAP_2_WAY",
    "HANDICAP_2_WAY",
    "RUN_LINE",
    "MONEYLINE",
}

MARKET_NAME_KEYWORDS_WANTED = [
    "gagnant",
    "victoire",
    "total de points",
    "total de coups",
    "plus/moins",
    "2 issues",
    "pointage",
    "écart",
    "handicap",
    "moneyline",
]

MARKET_NAME_KEYWORDS_EXCLUDE = [
    "manche",          # Paris par manche (inning)
    "5 premières",     # F5 (first 5 innings bet)
    "5 premiers",
    "1re mi",
    "2e mi",
    "joueur",          # Props joueurs
    "circuit",         # Home run props
    "retrait",         # Strikeout props
    "coup sûr",        # Hit props
    "lanceur",         # Pitcher props
    "premier",         # First to score
    "paires",
    "impair",
    "avantage",
    "barrage",
    "prolongation",
    "marge de",
    "quand",
]


# --- Conversion UTC -> heure Montréal ----------------------------------------

def _utc_to_local(utc_str: str) -> tuple[str, str]:
    """Convertit une date UTC ISO en date/heure locale (UTC-4 été, UTC-5 hiver)."""
    try:
        dt = datetime.fromisoformat(utc_str.replace('Z', '+00:00'))
        # Avril-octobre MLB = heure été (UTC-4)
        local = dt - timedelta(hours=4)
        return local.strftime('%Y-%m-%d'), local.strftime('%H:%M')
    except Exception:
        return utc_str[:10], ""


# --- Filtrage des marchés ----------------------------------------------------

def _should_include_market(name: str, group_code: str) -> bool:
    """Retourne True si ce type de pari doit être affiché."""
    name_lower = name.lower()
    for kw in MARKET_NAME_KEYWORDS_EXCLUDE:
        if kw in name_lower:
            return False
    if group_code in MARKET_GROUPS_WANTED:
        return True
    for kw in MARKET_NAME_KEYWORDS_WANTED:
        if kw in name_lower:
            return True
    return False


# --- Parsing API -------------------------------------------------------------

def _parse_event(data: dict) -> Optional[Match]:
    """Convertit un dict d'événement API en objet Match."""
    if not data.get('displayed') or not data.get('active'):
        return None

    event_id = str(data.get('id', ''))
    name     = data.get('name', '')
    start    = data.get('startTime', '')

    # Vérifier que c'est bien du baseball/MLB
    type_info    = data.get('type', {})
    league       = type_info.get('name', '')
    category     = data.get('category', {}).get('name', '')
    sport_name   = data.get('sport', {}).get('name', '')

    league_upper   = league.upper()
    category_upper = category.upper()
    sport_upper    = sport_name.upper()

    is_mlb = (
        "MLB" in league_upper
        or "BASEBALL" in league_upper
        or "BASEBALL" in category_upper
        or "BASEBALL" in sport_upper
    )
    # Bannir explicitement NCAA et autres ligues non-MLB
    is_ncaa = "NCAA" in league_upper or "COLLEGE" in league_upper or "COLLEGE" in category_upper

    if is_ncaa:
        # Rejeter NCAA, college baseball, etc.
        return None

    if not is_mlb and league:
        # Si ce n'est pas identifié comme baseball, rejeter
        return None

    # Équipes
    teams = data.get('teams', [])
    home_team = away_team = ""
    for t in teams:
        if t.get('side') == 'HOME':
            home_team = t['name']
        elif t.get('side') == 'AWAY':
            away_team = t['name']

    if not home_team or not away_team:
        m = re.match(r'^(.+?)\s+[aà@]\s+(.+)$', name, re.IGNORECASE)
        if m:
            away_team, home_team = m.group(1).strip(), m.group(2).strip()
        else:
            return None

    # Nettoyer les noms
    home_team = home_team.replace('\ufffd', 'e').replace('\u00e9', 'e').replace('\u00e8', 'e')
    away_team = away_team.replace('\ufffd', 'e').replace('\u00e9', 'e').replace('\u00e8', 'e')

    date_str, time_str = _utc_to_local(start)

    if not league:
        league = "MLB"

    match = Match(
        sport="baseball",
        league=league if league else "MLB",
        home_team=home_team,
        away_team=away_team,
        date=date_str,
        time=time_str,
        event_id=event_id,
    )

    # Marchés et cotes
    seen_group_codes = set()
    for market in data.get('markets', []):
        if not market.get('displayed') or not market.get('active'):
            continue

        market_name = market.get('name', '')
        group_code  = market.get('groupCode', '')

        if not _should_include_market(market_name, group_code):
            continue

        if group_code and group_code in seen_group_codes:
            continue
        if group_code:
            seen_group_codes.add(group_code)

        outcomes = market.get('outcomes', [])
        if not outcomes:
            continue

        grp = BetGroup(bet_type=market_name)

        for outcome in outcomes:
            if not outcome.get('displayed') or not outcome.get('active'):
                continue
            prices = outcome.get('prices', [])
            if not prices:
                continue
            dec = prices[0].get('decimal')
            if not dec or float(dec) <= 1.0:
                continue

            sel_name = outcome.get('name', '')
            sel_name = sel_name.replace('\ufffd', 'e').replace('\u00e9', 'e').replace('\u00e8', 'e')

            # Ajouter la ligne pour Plus de / Moins de
            if sel_name in ('Plus de', 'Moins de', 'Over', 'Under'):
                line = (outcome.get('line') or outcome.get('points')
                        or outcome.get('attr') or market.get('line')
                        or market.get('attr') or '')
                if line:
                    sel_name = f"{sel_name} {line}"

            grp.selections.append(Selection(
                label=sel_name,
                odds=float(dec),
                prediction_id=str(outcome.get('id', '')),
            ))

        if len(grp.selections) >= 2:
            match.bet_groups.append(grp)

    return match if match.bet_groups else None


# --- Fetch API via requests ---------------------------------------------------

_API_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept":     "application/json",
    "Referer":    "https://miseojeuplus.espacejeux.com/",
    "Origin":     "https://miseojeuplus.espacejeux.com",
}

_API_SESSION = _requests_mod.Session()
_API_SESSION.headers.update(_API_HEADERS)


def _fetch_one_event(event_id: str, url_map: dict) -> list[Match]:
    """Récupère les cotes d'un événement via requests."""
    api_url = f"{API_BASE}/events-by-ids?eventIds={event_id}&{API_PARAMS}"
    try:
        resp = _API_SESSION.get(api_url, timeout=12)
        resp.raise_for_status()
        data = resp.json()
        events_list = data.get("data", {}).get("events", [])
        result = []
        for ev in events_list:
            match = _parse_event(ev)
            if match:
                match.event_url = url_map.get(match.event_id, "")
                print(f"     {match.away_team} @ {match.home_team} - {len(match.bet_groups)} marchés")
                result.append(match)
        return result
    except Exception as e:
        print(f"    [!] Erreur event {event_id}: {e}")
        return []


def _fetch_events_parallel(event_ids: list[str], url_map: dict,
                            max_workers: int = 8) -> list[Match]:
    """Récupère tous les événements en parallèle."""
    matches: list[Match] = []
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futures = {ex.submit(_fetch_one_event, eid, url_map): eid
                   for eid in event_ids}
        for future in as_completed(futures):
            try:
                matches.extend(future.result())
            except Exception:
                pass
    return matches


# --- Scraper principal -------------------------------------------------------

class MiseOJeuMLBScraper:
    def __init__(self, headless: bool = True):
        self.headless = headless

    async def scrape(self) -> list[Match]:
        """Scrape les événements MLB de Mise-O-Jeu."""
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(
                headless=self.headless,
                args=[
                    '--disable-blink-features=AutomationControlled',
                    '--disable-dev-shm-usage',  # Railway: no /dev/shm, use swap
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

            # Charger la page principale (même approche que NHL scraper)
            # La page contient tous les sports, on filtre par baseball dans _parse_event()
            print("  >> Chargement de la page principale Mise-O-Jeu...")
            page = await context.new_page()
            try:
                await page.goto(BASE_SITE, wait_until='networkidle', timeout=30000)
            except Exception:
                await asyncio.sleep(2)

            html    = await page.content()
            cookies = await context.cookies()
            for ck in cookies:
                _API_SESSION.cookies.set(ck["name"], ck["value"], domain=ck.get("domain", ""))

            await page.close()

            # Extraire TOUS les event IDs (tous les sports)
            # _parse_event() filtre par baseball
            event_data = self._extract_all_event_ids(html)

            # Filtrer pour garder seulement les liens avec "baseball" dans l'URL
            event_data = [
                (eid, url) for eid, url in event_data
                if 'baseball' in url.lower() or 'mlb' in url.lower()
            ]

            print(f"     {len(event_data)} événements MLB trouvés")

            if not event_data:
                await browser.close()
                return []

            url_map   = {eid: url for eid, url in event_data}
            event_ids = [eid for eid, _ in event_data]

            await browser.close()
            matches = _fetch_events_parallel(event_ids, url_map, max_workers=10)

            # Fallback Playwright si cookies insuffisants
            if not matches:
                print("  >> Fallback Playwright pour les events MLB...")
                browser2 = await pw.chromium.launch(headless=self.headless)
                context2 = await browser2.new_context(
                    user_agent=(
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/122.0.0.0 Safari/537.36"
                    ),
                    locale="fr-CA",
                )
                tasks = [self._fetch_event_async(context2, eid, url_map) for eid in event_ids]
                results = await asyncio.gather(*tasks, return_exceptions=True)
                await browser2.close()
                for r in results:
                    if isinstance(r, list):
                        matches.extend(r)

            return matches

    async def _fetch_event_async(self, context, event_id: str, url_map: dict) -> list[Match]:
        """Récupère les cotes d'un événement via Playwright (fallback)."""
        api_url = f"{API_BASE}/events-by-ids?eventIds={event_id}&{API_PARAMS}"
        page = await context.new_page()
        try:
            await page.goto(api_url, wait_until='domcontentloaded', timeout=20000)
            raw = await page.content()
            m = re.search(r'<pre[^>]*>(.*?)</pre>', raw, re.DOTALL)
            json_str = m.group(1) if m else raw
            if not json_str.strip().startswith('{'):
                m2 = re.search(r'(\{.*\})', raw, re.DOTALL)
                if m2:
                    json_str = m2.group(1)
            data = json.loads(json_str)
            events_list = data.get('data', {}).get('events', [])
            result = []
            for ev in events_list:
                match = _parse_event(ev)
                if match:
                    match.event_url = url_map.get(match.event_id, "")
                    result.append(match)
            return result
        except Exception as e:
            print(f"    [!] Erreur event {event_id}: {e}")
            return []
        finally:
            await page.close()

    def _extract_all_event_ids(self, html: str) -> list[tuple[str, str]]:
        """
        Extrait les IDs et URLs de tous les événements (même approche que NHL scraper).
        Retourne une liste de tuples (event_id, event_url).

        Supporte deux formats d'URL:
        - Ancien: /sports/fr/en-jeux/evenement/ID/baseball/amerique-du-nord/mlb/nom
        - Nouveau: /sports/fr/sportif/evenement/ID  (sport détecté via contexte HTML)
        """
        seen   = set()
        result = []

        # Cherche les chemins relatifs dans les attributs href
        # Pattern similaire à NHL, mais incluant baseball
        base = "https://miseojeuplus.espacejeux.com"
        patterns = [
            (r'href="(/sports/fr/(?:en-jeux|sportif)/evenement/(\d+)/baseball/amerique-du-nord/mlb/[^"\'<>\s]*)"',
             "baseball"),
        ]

        for pattern, sport in patterns:
            for path, eid in re.findall(pattern, html):
                if eid not in seen:
                    seen.add(eid)
                    result.append((eid, base + path.rstrip('/')))

        return result


def scrape_sync(headless: bool = True) -> list[Match]:
    """Point d'entrée synchrone pour le scraping MLB."""
    scraper = MiseOJeuMLBScraper(headless=headless)
    return asyncio.run(scraper.scrape())
