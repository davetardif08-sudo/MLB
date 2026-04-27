import json, os, sys, re
from pathlib import Path
from collections import defaultdict

sys.stdout.reconfigure(encoding='utf-8')

snapshots_dir = Path('snapshots')
files = sorted([f for f in os.listdir(snapshots_dir) if f.endswith('.json')])

import statsapi
from mlb_stats import _find_team_id

def fetch_results(date_str):
    results = {}
    try:
        for g in statsapi.schedule(date=date_str):
            if g.get('status') not in ('Final', 'Game Over', 'Completed Early'):
                continue
            home_id = g.get('home_id'); away_id = g.get('away_id')
            hs = g.get('home_score', 0) or 0
            as_ = g.get('away_score', 0) or 0
            entry = {
                'home_score': hs, 'away_score': as_,
                'total': hs+as_,
                'winner': 'home' if hs > as_ else 'away',
                'home_name': g.get('home_name',''),
                'away_name': g.get('away_name',''),
            }
            if home_id: results[str(home_id)] = entry
            if away_id: results[str(away_id)] = entry
            results[g.get('home_name','').lower()] = entry
            results[g.get('away_name','').lower()] = entry
    except Exception as e:
        print(f'  [warn] {date_str}: {e}')
    return results

def resolve_pick(pick, results):
    home_raw = pick.get('home_team',''); away_raw = pick.get('away_team','')
    game = None
    try:
        hid = _find_team_id(home_raw); aid = _find_team_id(away_raw)
        if hid: game = results.get(str(hid))
        if not game and aid: game = results.get(str(aid))
    except: pass
    if not game:
        game = results.get(home_raw.lower()) or results.get(away_raw.lower())
    if not game: return 'pending'

    sel   = (pick.get('selection') or '').lower()
    btype = (pick.get('bet_type') or '').lower()
    hs, as_, total = game['home_score'], game['away_score'], game['total']

    if any(k in btype for k in ('gagnant','moneyline','victoire','winner','2 issues')):
        def tm(name, s):
            return any(w in s for w in re.findall(r'[a-z]+', name.lower()) if len(w) > 2)
        if tm(home_raw, sel): return 'win' if game['winner'] == 'home' else 'loss'
        if tm(away_raw, sel): return 'win' if game['winner'] == 'away' else 'loss'

    if any(k in btype for k in ('total','plus/moins','over','under')):
        def tm2(name, bt):
            return any(w in bt for w in re.findall(r'[a-z]+', name.lower()) if len(w) > 2)
        score = hs if tm2(home_raw, btype) else (as_ if tm2(away_raw, btype) else total)
        m = re.search(r'(\d+\.?\d*)', sel) or re.search(r'(\d+\.?\d*)', btype)
        if m:
            line = float(m.group(1))
            if 'plus' in sel or 'over' in sel:
                return 'win' if score > line else ('push' if score == line else 'loss')
            if 'moins' in sel or 'under' in sel:
                return 'win' if score < line else ('push' if score == line else 'loss')
    return 'pending'

# --- Charger et resoudre tous les paris ---
all_bets  = []
all_preds = []
results_cache = {}

for fname in files:
    with open(snapshots_dir / fname, encoding='utf-8') as f:
        snap = json.load(f)
    d = snap.get('date', fname[:10])
    if d not in results_cache:
        results_cache[d] = fetch_results(d)
    res = results_cache[d]

    for p in snap.get('picks', []):
        outcome = resolve_pick(p, res)
        is_bet  = p.get('is_bet', False)
        mise    = (p.get('mise') or 0) if is_bet else 0
        odds    = p.get('odds') or 2.0
        profit  = (mise*(odds-1) if outcome == 'win'
                   else -mise    if outcome == 'loss'
                   else 0.0)
        entry = {**p, 'outcome': outcome, 'profit': profit, 'snap_date': d}
        if is_bet:
            all_bets.append(entry)
        else:
            all_preds.append(entry)

# --- Bilan global ---
resolved = [b for b in all_bets if b['outcome'] != 'pending']
wins     = [b for b in resolved if b['outcome'] == 'win']
losses   = [b for b in resolved if b['outcome'] == 'loss']
pushes   = [b for b in resolved if b['outcome'] == 'push']
pending  = [b for b in all_bets  if b['outcome'] == 'pending']

total_mise   = sum(b.get('mise', 0) for b in resolved)
total_profit = sum(b['profit'] for b in resolved)
roi_pct      = total_profit / total_mise * 100 if total_mise > 0 else 0
wr           = len(wins)/(len(wins)+len(losses))*100 if (len(wins)+len(losses)) > 0 else 0

print('='*55)
print('  BILAN GLOBAL MLB (paris mises uniquement)')
print('='*55)
print(f'  Snapshots couverts   : {len(files)} soirees')
print(f'  Paris resolus        : {len(resolved)}  ({len(wins)}V / {len(losses)}D / {len(pushes)} nuls)')
print(f'  En attente           : {len(pending)}')
print(f'  Win rate             : {wr:.1f}%')
print(f'  Mise totale engagee  : ${total_mise:.2f}')
print(f'  Profit net           : ${total_profit:+.2f}')
print(f'  ROI                  : {roi_pct:+.1f}%')

print()
print('=== PAR DATE ===')
for d in sorted(set(b['snap_date'] for b in all_bets)):
    day   = [b for b in all_bets if b['snap_date'] == d]
    res_d = [b for b in day if b['outcome'] != 'pending']
    w  = sum(1 for b in res_d if b['outcome'] == 'win')
    l  = sum(1 for b in res_d if b['outcome'] == 'loss')
    pn = sum(1 for b in day  if b['outcome'] == 'pending')
    pft = sum(b['profit'] for b in res_d)
    msd = sum(b.get('mise', 0) for b in res_d)
    wr_s  = f' ({w/(w+l)*100:.0f}%)' if w+l > 0 else ''
    pnd_s = f' + {pn} pending' if pn else ''
    print(f'  {d}: {w}V/{l}D{wr_s}  => ${pft:+.2f} / ${msd:.2f}{pnd_s}')

print()
print('=== PAR TYPE DE PARI ===')
by_type = defaultdict(list)
for b in resolved:
    bt = b.get('bet_type','?').lower()
    if any(k in bt for k in ('gagnant','moneyline','2 issues')): t = 'Moneyline/Gagnant'
    elif any(k in bt for k in ('total','plus/moins')): t = 'Total O/U'
    else: t = 'Autre'
    by_type[t].append(b)
for t, picks in sorted(by_type.items()):
    w = sum(1 for p in picks if p['outcome']=='win')
    l = sum(1 for p in picks if p['outcome']=='loss')
    pft = sum(p['profit'] for p in picks)
    msd = sum(p.get('mise',0) for p in picks)
    wr2 = w/(w+l)*100 if w+l > 0 else 0
    print(f'  {t}: {w}V/{l}D = {wr2:.0f}%  =>  ${pft:+.2f} / ${msd:.2f}')

print()
print('=== PAR RECOMMANDATION ===')
by_rec = defaultdict(list)
for b in resolved:
    rec = (b.get('recommendation') or '').lower()
    if 'excellent' in rec:  r = 'Excellent'
    elif rec.startswith('bon'): r = 'Bon'
    else: r = 'Autre'
    by_rec[r].append(b)
for r, picks in sorted(by_rec.items()):
    w = sum(1 for p in picks if p['outcome']=='win')
    l = sum(1 for p in picks if p['outcome']=='loss')
    pft = sum(p['profit'] for p in picks)
    msd = sum(p.get('mise',0) for p in picks)
    wr2 = w/(w+l)*100 if w+l > 0 else 0
    print(f'  {r}: {w}V/{l}D = {wr2:.0f}%  =>  ${pft:+.2f} / ${msd:.2f}')

print()
print('=== PAR FOURCHETTE DE COTES ===')
ranges = [('1.50-1.79',1.50,1.79),('1.80-2.09',1.80,2.09),('2.10-2.49',2.10,2.49),('2.50+',2.50,99)]
for label, lo, hi in ranges:
    picks = [b for b in resolved if lo <= (b.get('odds') or 0) < hi]
    if not picks: continue
    w = sum(1 for p in picks if p['outcome']=='win')
    l = sum(1 for p in picks if p['outcome']=='loss')
    pft = sum(p['profit'] for p in picks)
    msd = sum(p.get('mise',0) for p in picks)
    wr2 = w/(w+l)*100 if w+l > 0 else 0
    print(f'  {label}: {w}V/{l}D = {wr2:.0f}%  =>  ${pft:+.2f} / ${msd:.2f}')

if pending:
    print()
    print('=== EN ATTENTE (2026-04-15) ===')
    for b in pending:
        print(f'  {b.get("away_team","?")} @ {b.get("home_team","?")}')
        print(f'    {b.get("selection","?")} | cote {b.get("odds",0):.2f} | mise ${b.get("mise",0):.2f}')

# Calibration predictions (non-misees)
print()
print('=== CALIBRATION (toutes predictions, bet + non-bet) ===')
all_resolved_preds = [p for p in all_preds if p['outcome'] in ('win','loss')]
if all_resolved_preds:
    buckets = [(0.4,0.5),(0.5,0.6),(0.6,0.7),(0.7,0.8),(0.8,0.9)]
    for lo, hi in buckets:
        bp = [p for p in all_resolved_preds if lo <= (p.get('fair_prob') or 0) < hi]
        if not bp: continue
        w = sum(1 for p in bp if p['outcome']=='win')
        actual = w/len(bp)*100
        predicted = (lo+hi)/2*100
        print(f'  Prob {int(lo*100)}-{int(hi*100)}%: {len(bp)} picks, predit={predicted:.0f}%, reel={actual:.1f}%  (biais {actual-predicted:+.1f}%)')
else:
    print('  (Pas assez de predictions non-misees resolues - correction bug key en place pour prochains snapshots)')
