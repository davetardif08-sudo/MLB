"""
Scraper pour miseojeu.lotoquebec.com — Sport MLB (Baseball)

Architecture HYBRIDE:
  1. Récupérer liste complète des matchs du jour via statsapi.mlb.com (officiel, pas de géo-blocage)
  2. Charger miseojeu.lotoquebec.com pour les cotes de paris
  3. Fusionner par match (away_team + home_team)
  4. Pour chaque match trouvé sur Loto-Québec, charger les cotes détaillées

Avantages :
  - 8+ matchs au lieu de 2 (couverture complète)
  - Cotes officielles Loto-Québec pour les matchs disponibles
  - Pas de géo-blocage
  - Pas d'API REST complexe, juste HTML
"""

import asyncio
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from dataclasses import dataclass, field
from typing import Optional
from playwright.async_api import async_playwright
import json

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
    # Live game state (depuis MLB.com)
    live_status: str = ""        # "Preview" | "Live" | "Final"
    detailed_status: str = ""    # "Scheduled", "In Progress", "Final", "Postponed", etc.
    away_score: int = 0
    home_score: int = 0
    current_inning: str = ""     # ex: "Top 5", "Bot 7", "Final"


# --- Configuration -----------------------------------------------------------

BASE_URL = "https://miseojeu.lotoquebec.com"
LIST_URL = f"{BASE_URL}/fr/offre-de-paris/baseball/mlb/matchs?idAct=10"
MATCH_URL_TEMPLATE = f"{BASE_URL}/fr/offre-de-paris/baseball/mlb/matchs?idEve={{eid}}"

# --- API REST (contourne géolocalisation comme dans app NHL) ---
# L'API content-service n'est PAS géo-bloquée contrairement au site HTML
API_BASE = "https://content.mojp-sgdigital-jel.com/content-service/api/v1/q"
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
# Tag IDs côté Mise-O-Jeu pour identifier les compétitions
# (À découvrir pour MLB - on essaiera plusieurs valeurs candidates)
BASEBALL_TAG_IDS_CANDIDATES = ["8", "9", "10", "11", "608", "609", "610"]
_API_SESSION = _requests_mod.Session()
_API_SESSION.headers.update({
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Accept": "application/json",
    "Referer": "https://miseojeuplus.espacejeux.com/",
    "Origin": "https://miseojeuplus.espacejeux.com",
})

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
        # URLs alternatives (fallback si une URL retourne 0 matchs)
        self.backup_list_url = "https://miseojeuplus.espacejeux.com/sports/fr/"

    async def scrape(self) -> list[Match]:
        """Scrape les événements MLB de Mise-O-Jeu Loto-Québec (+ fallback espacejeux)."""
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

            print(f"     {len(list_data)} matchs trouvés sur la liste Loto-Québec")

            # Si aucun match sur lotoquebec.com, essayer espacejeux.com en fallback
            if not list_data and self.backup_list_url != LIST_URL:
                print(f"  >> Fallback vers {self.backup_list_url}")
                try:
                    await page.goto(self.backup_list_url, wait_until='domcontentloaded', timeout=35000)
                    await asyncio.sleep(2)
                    list_data = await page.evaluate(r'''() => {
                        const all = document.querySelectorAll('a[href*="/evenement/"][href*="baseball"]');
                        const seen = new Set();
                        const result = [];
                        for (const a of all) {
                            const m = a.href.match(/\/evenement\/(\d+)/);
                            if (!m || seen.has(m[1])) continue;
                            seen.add(m[1]);
                            result.push({id: m[1], href: a.href, context: (a.parentElement?.innerText || '')});
                        }
                        return result;
                    }''')
                    print(f"     {len(list_data)} matchs trouvés sur espacejeux (fallback)")
                except Exception as e:
                    print(f"  >> Fallback échoué: {e}")

            await page.close()

            if not list_data:
                await browser.close()
                return []

            # Parser les contextes pour récupérer équipes + heure
            today = (datetime.utcnow() - timedelta(hours=4)).strftime('%Y-%m-%d')
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


def _fetch_event_ids_via_api() -> list[tuple[str, str]]:
    """
    Récupère la liste des matchs MLB via l'API event-list de Mise-O-Jeu.
    Utilise les MÊMES paramètres que le scraper NHL pour contourner géo-blocage.
    Retourne list[(event_id, event_url)].
    """
    # Essayer plusieurs tag IDs candidats pour le baseball
    for tag_id in BASEBALL_TAG_IDS_CANDIDATES:
        api_url = (
            f"{API_BASE}/event-list"
            f"?eventSortsIncluded=MTCH"
            f"&includeChildMarkets=false"
            f"&drilldownTagIds={tag_id}"
            f"&lang=fr-CA&channel=I"
        )
        try:
            resp = _API_SESSION.get(api_url, timeout=15)
            if resp.status_code != 200:
                print(f"  >> API event-list tag={tag_id}: HTTP {resp.status_code}")
                continue
            data = resp.json()
            events = data.get("data", {}).get("events") or []
            if not events:
                continue

            # Vérifier que c'est bien du baseball (regarder le premier event)
            first_evt = events[0]
            sport_name = (first_evt.get("sportName", "") or
                         first_evt.get("competition", {}).get("sportName", "") or "").lower()
            comp_name = (first_evt.get("competitionName", "") or
                        first_evt.get("competition", {}).get("name", "") or "").lower()
            if "baseball" not in sport_name and "mlb" not in comp_name and "baseball" not in comp_name:
                # Pas du baseball, essayer le tag ID suivant
                print(f"  >> Tag ID {tag_id} = {sport_name}/{comp_name} (pas baseball)")
                continue

            result = []
            for ev in events:
                eid = str(ev.get("id", ""))
                if not eid:
                    continue
                url = f"https://miseojeuplus.espacejeux.com/sports/fr/sportif/evenement/{eid}"
                result.append((eid, url))
            print(f"  >> API event-list (baseball, tag={tag_id}): {len(result)} matchs")
            return result
        except Exception as e:
            print(f"  [!] API event-list tag={tag_id} erreur: {type(e).__name__}: {e}")
            continue

    print(f"  >> Aucun tag ID baseball valide trouvé")
    return []


def _fetch_one_event_api(event_id: str, url_map: dict) -> Optional[Match]:
    """Récupère les cotes d'un événement via l'API events-by-ids."""
    api_url = f"{API_BASE}/events-by-ids?eventIds={event_id}&{API_PARAMS}"
    try:
        resp = _API_SESSION.get(api_url, timeout=12)
        resp.raise_for_status()
        data = resp.json()
        events_list = data.get("data", {}).get("events", [])
        for ev in events_list:
            match = _parse_event_api(ev)
            if match:
                match.event_url = url_map.get(match.event_id, "")
                print(f"     API: {match.away_team} @ {match.home_team} - {len(match.bet_groups)} marchés")
                return match
        return None
    except Exception as e:
        print(f"    [!] Erreur event API {event_id}: {e}")
        return None


def _parse_event_api(ev: dict) -> Optional[Match]:
    """Parse un événement API en objet Match avec ses cotes."""
    try:
        eid = str(ev.get("id", ""))
        if not eid:
            return None

        # Extraire les équipes
        participants = ev.get("participants", [])
        if len(participants) < 2:
            return None

        # Convention Mise-O-Jeu : participant[0] = away, participant[1] = home
        away_raw = participants[0].get("name", "").strip()
        home_raw = participants[1].get("name", "").strip()
        if not away_raw or not home_raw:
            return None

        # Date et heure
        start_iso = ev.get("startTimeUtc") or ev.get("startTime", "")
        try:
            dt_utc = datetime.fromisoformat(start_iso.replace('Z', '+00:00'))
            local = dt_utc.replace(tzinfo=None) - timedelta(hours=4)
            date_str = local.strftime('%Y-%m-%d')
            time_str = local.strftime('%H:%M')
        except Exception:
            date_str = (datetime.utcnow() - timedelta(hours=4)).strftime('%Y-%m-%d')
            time_str = ''

        match = Match(
            sport="baseball",
            league="MLB",
            home_team=_normalize_team_name(home_raw),
            away_team=_normalize_team_name(away_raw),
            date=date_str,
            time=time_str,
            event_id=eid,
        )

        # Extraire les marchés (markets)
        markets = ev.get("markets", []) or []
        for mkt in markets:
            mkt_name = (mkt.get("name", "") or mkt.get("displayName", "")).strip()
            if not mkt_name:
                continue

            # Filtrer les marchés intéressants
            if not _should_keep_market(mkt_name):
                continue

            grp = BetGroup(bet_type=mkt_name)
            for outcome in mkt.get("outcomes", []) or mkt.get("selections", []):
                sel_name = (outcome.get("name", "") or outcome.get("displayName", "")).strip()
                price = outcome.get("price", {})
                # Le prix peut être un dict {decimal: 1.95} ou un float direct
                if isinstance(price, dict):
                    odds_val = price.get("decimal") or price.get("decimalOdds")
                else:
                    odds_val = price
                try:
                    odds_f = float(odds_val) if odds_val else 0
                except (ValueError, TypeError):
                    odds_f = 0
                if odds_f > 1.0 and sel_name:
                    sel_id = outcome.get("id", "") or outcome.get("selectionId", "")
                    grp.selections.append(Selection(
                        label=sel_name,
                        odds=odds_f,
                        prediction_id=str(sel_id),
                    ))
            if len(grp.selections) >= 2:
                match.bet_groups.append(grp)

        return match if match.bet_groups else None
    except Exception as e:
        print(f"    [!] Parse event erreur: {e}")
        return None


def _fetch_api_baseball_matches() -> list[Match]:
    """
    Récupère les matchs MLB via l'API REST Mise-O-Jeu (contourne géolocalisation).
    Cette API utilise les mêmes paramètres que le scraper NHL.
    """
    event_pairs = _fetch_event_ids_via_api()
    if not event_pairs:
        return []

    url_map = {eid: url for eid, url in event_pairs}
    event_ids = [eid for eid, _ in event_pairs]

    matches = []
    with ThreadPoolExecutor(max_workers=8) as ex:
        futures = {ex.submit(_fetch_one_event_api, eid, url_map): eid
                   for eid in event_ids}
        for future in as_completed(futures):
            try:
                m = future.result()
                if m:
                    matches.append(m)
            except Exception as e:
                print(f"    [!] Erreur fetch parallèle: {e}")

    print(f"  >> API: {len(matches)} matchs avec cotes récupérés")
    return matches


def _get_mlb_com_matches() -> dict:
    """
    Récupère la liste officielle des matchs du jour (heure Montréal) depuis MLB.com,
    avec scores en direct via hydrate=linescore.
    Retourne un dict: {(away_name, home_name): {time, status, scores, ...}}
    """
    try:
        # "Aujourd'hui" en heure de Montréal (UTC-4 EDT en avril/oct, UTC-5 EST l'hiver)
        # On utilise UTC-4 par défaut (saison MLB = avril-octobre = EDT)
        montreal_now = datetime.utcnow() - timedelta(hours=4)
        today_mtl = montreal_now.strftime('%Y-%m-%d')

        # Récupérer 2 jours pour gérer les fuseaux (jeux ET tard = lendemain UTC)
        # mais on filtrera ensuite sur la date locale Montréal
        url = (f"https://statsapi.mlb.com/api/v1/schedule"
               f"?sportId=1&startDate={today_mtl}&endDate={today_mtl}"
               f"&hydrate=linescore,team")
        r = _requests_mod.get(url, timeout=15)
        r.raise_for_status()
        data = r.json()

        matches_map = {}
        for date_block in data.get('dates', []):
            for g in date_block.get('games', []):
                away_name = g.get('teams', {}).get('away', {}).get('team', {}).get('name', '')
                home_name = g.get('teams', {}).get('home', {}).get('team', {}).get('name', '')
                game_dt_iso = g.get('gameDate', '')  # ISO UTC

                # Filtrer : ne garder que les matchs dont la date LOCALE Montréal == aujourd'hui
                try:
                    game_dt_utc = datetime.fromisoformat(game_dt_iso.replace('Z', '+00:00'))
                    game_dt_mtl = game_dt_utc.replace(tzinfo=None) - timedelta(hours=4)
                    if game_dt_mtl.strftime('%Y-%m-%d') != today_mtl:
                        continue
                except Exception:
                    continue

                status_obj = g.get('status', {})
                abstract_state = status_obj.get('abstractGameState', '')  # Preview/Live/Final
                detailed_state = status_obj.get('detailedState', '')

                # Extraire scores via linescore
                linescore = g.get('linescore', {}) or {}
                away_runs = linescore.get('teams', {}).get('away', {}).get('runs', 0) or 0
                home_runs = linescore.get('teams', {}).get('home', {}).get('runs', 0) or 0
                inning = linescore.get('currentInning', 0) or 0
                inning_state = linescore.get('inningState', '')  # Top/Middle/Bottom/End

                # Texte d'état
                if abstract_state == 'Live':
                    inning_short = {'Top': 'Haut', 'Bottom': 'Bas', 'Middle': 'Mil', 'End': 'Fin'}.get(inning_state, inning_state)
                    current_inning_str = f"{inning_short} {inning}" if inning else 'En cours'
                elif abstract_state == 'Final':
                    current_inning_str = 'Final'
                else:
                    current_inning_str = ''

                if away_name and home_name:
                    away_norm = _normalize_team_name_mlb(away_name)
                    home_norm = _normalize_team_name_mlb(home_name)
                    key = (away_norm, home_norm)
                    matches_map[key] = {
                        'time': game_dt_iso[:16],
                        'status': abstract_state,
                        'detailed_status': detailed_state,
                        'away_score': int(away_runs),
                        'home_score': int(home_runs),
                        'current_inning': current_inning_str,
                        'mlb_away': away_name,
                        'mlb_home': home_name,
                    }

        print(f"     MLB.com (Montréal {today_mtl}): {len(matches_map)} matchs aujourd'hui")
        return matches_map
    except Exception as e:
        print(f"  >> Erreur MLB.com: {e}")
        return {}


def _normalize_team_name_mlb(name: str) -> str:
    """Convertit un nom MLB.com en nom Loto-Québec."""
    # MLB.com: "Tampa Bay Rays" → Loto-Q: "Tampa Bay (Rays)"
    mapping = {
        "Tampa Bay Rays": "Tampa Bay (Rays)",
        "Cleveland Guardians": "Cleveland (Guardians)",
        "St. Louis Cardinals": "Saint-Louis (Cardinals)",
        "Pittsburgh Pirates": "Pittsburgh (Pirates)",
        "Boston Red Sox": "Boston (Red Sox)",
        "Toronto Blue Jays": "Toronto (Blue Jays)",
        "Los Angeles Angels": "Los Angeles (Angels)",
        "Chicago White Sox": "Chicago (White Sox)",
        "Seattle Mariners": "Seattle (Mariners)",
        "Minnesota Twins": "Minnesota (Twins)",
        "New York Yankees": "New York (Yankees)",
        "Texas Rangers": "Texas (Rangers)",
        "Chicago Cubs": "Chicago (Cubs)",
        "San Diego Padres": "San Diego (Padres)",
        "Miami Marlins": "Miami (Marlins)",
        "Los Angeles Dodgers": "Los Angeles (Dodgers)",
        "New York Mets": "New York (Mets)",
        "Atlanta Braves": "Atlanta (Braves)",
        "Philadelphia Phillies": "Philadelphia (Phillies)",
        "Washington Nationals": "Washington (Nationals)",
        "Milwaukee Brewers": "Milwaukee (Brewers)",
        "Cincinnati Reds": "Cincinnati (Reds)",
        "Detroit Tigers": "Detroit (Tigers)",
        "Kansas City Royals": "Kansas City (Royals)",
        "Houston Astros": "Houston (Astros)",
        "Oakland Athletics": "Oakland (Athletics)",
        "Colorado Rockies": "Colorado (Rockies)",
        "Arizona Diamondbacks": "Arizona (Diamondbacks)",
        "San Francisco Giants": "San Francisco (Giants)",
        "Baltimore Orioles": "Baltimore (Orioles)",
    }
    return mapping.get(name, name)


async def _enrich_matches_with_mlb(matches: list[Match]) -> list[Match]:
    """
    Ajoute les matchs de MLB.com qui ne sont pas dans les cotes Loto-Québec.
    Crée des matchs "info only" sans cotes.
    """
    mlb_matches = _get_mlb_com_matches()
    seen_keys = set()

    for m in matches:
        key = (m.away_team, m.home_team)
        seen_keys.add(key)

    # Créer des matchs sans cotes pour les matchs manquants
    enriched = list(matches)
    for key, mlb_info in mlb_matches.items():
        if key not in seen_keys:
            away_team, home_team = key
            # Parser le temps ISO
            try:
                dt = datetime.fromisoformat(mlb_info['time'].replace('Z', '+00:00'))
                # Convertir en heure locale (UTC-4 en avril)
                local = dt - timedelta(hours=4)
                date_str = local.strftime('%Y-%m-%d')
                time_str = local.strftime('%H:%M')
            except Exception:
                date_str = (datetime.utcnow() - timedelta(hours=4)).strftime('%Y-%m-%d')
                time_str = ''

            # Créer un match sans cotes (pour le carousel uniquement)
            new_match = Match(
                sport="baseball",
                league="MLB",
                home_team=home_team,
                away_team=away_team,
                date=date_str,
                time=time_str,
                event_id=f"mlb_{key[0]}_{key[1]}",  # ID synthétique
                event_url=f"https://mlb.com",
            )
            enriched.append(new_match)

    return enriched


def scrape_sync(headless: bool = True) -> list[Match]:
    """
    Point d'entrée synchrone.
    PRIMARY: MLB.com (toujours fiable, liste officielle des matchs du jour).
    SECONDARY: Loto-Québec (cotes), enrichit les matchs MLB.com quand disponibles.

    Garantit qu'on retourne TOUJOURS la liste des matchs du jour, même si
    Loto-Québec est vide / inaccessible / géo-bloqué (ex: sur Railway).
    """
    print("[scraper] === Démarrage scrape MLB ===")

    # 1) MLB.com en premier (source primaire, toujours fiable)
    print("[scraper] 1/2 — MLB.com (source primaire)...")
    mlb_matches_map = _get_mlb_com_matches()
    print(f"[scraper]      MLB.com: {len(mlb_matches_map)} matchs")

    # 2) Loto-Québec en secondaire (cotes)
    # 2a) Essayer l'API REST en premier (NON géo-bloquée comme app NHL)
    print("[scraper] 2/2 — Loto-Québec API (cotes)...")
    odds_matches = _fetch_api_baseball_matches()
    if odds_matches:
        print(f"[scraper]      API: {len(odds_matches)} matchs avec cotes")

    # 2b) Fallback : Playwright si l'API ne donne rien
    if not odds_matches:
        print("[scraper]      API vide — fallback Playwright...")
        try:
            scraper = MiseOJeuMLBScraper(headless=headless)
            odds_matches = asyncio.run(scraper.scrape())
            print(f"[scraper]      Playwright: {len(odds_matches)} matchs avec cotes")
        except Exception as e:
            print(f"[scraper]      Playwright ERREUR: {type(e).__name__}: {e}")
            odds_matches = []

    # 3) Construire la liste finale : MLB.com base + cotes Loto-Q quand disponibles
    if not mlb_matches_map and not odds_matches:
        print("[scraper] AUCUNE source disponible — retour liste vide")
        return []

    # Si MLB.com a fonctionné, on l'utilise comme base
    if mlb_matches_map:
        # Indexer les matchs Loto-Québec par (away, home) pour merger les cotes
        odds_index = {(m.away_team, m.home_team): m for m in odds_matches}
        final_matches = []

        for key, mlb_info in mlb_matches_map.items():
            away_team, home_team = key
            # Si on a des cotes Loto-Québec pour ce match, on les enrichit avec les scores live
            if key in odds_index:
                m = odds_index[key]
                m.live_status     = mlb_info.get('status', '')
                m.detailed_status = mlb_info.get('detailed_status', '')
                m.away_score      = mlb_info.get('away_score', 0)
                m.home_score      = mlb_info.get('home_score', 0)
                m.current_inning  = mlb_info.get('current_inning', '')
                final_matches.append(m)
                continue

            # Sinon, créer un match "info only" pour le carousel
            try:
                dt = datetime.fromisoformat(mlb_info['time'].replace('Z', '+00:00'))
                local = dt - timedelta(hours=4)
                date_str = local.strftime('%Y-%m-%d')
                time_str = local.strftime('%H:%M')
            except Exception:
                date_str = (datetime.utcnow() - timedelta(hours=4)).strftime('%Y-%m-%d')
                time_str = ''

            final_matches.append(Match(
                sport="baseball",
                league="MLB",
                home_team=home_team,
                away_team=away_team,
                date=date_str,
                time=time_str,
                event_id=f"mlb_{away_team}_{home_team}",
                event_url="https://mlb.com",
                live_status=mlb_info.get('status', ''),
                detailed_status=mlb_info.get('detailed_status', ''),
                away_score=mlb_info.get('away_score', 0),
                home_score=mlb_info.get('home_score', 0),
                current_inning=mlb_info.get('current_inning', ''),
            ))

        print(f"[scraper] === Total: {len(final_matches)} matchs "
              f"({len(odds_matches)} avec cotes, {len(final_matches) - len(odds_matches)} sans cotes) ===")
        return final_matches

    # Fallback: MLB.com a échoué mais Loto-Québec a fonctionné
    print(f"[scraper] === Fallback Loto-Québec seul: {len(odds_matches)} matchs ===")
    return odds_matches
