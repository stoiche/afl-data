#!/usr/bin/env python3
"""
Turns the master player-game CSV into the compact JSON the head-to-head app fetches.

Keeps only the last 5 meetings per club pair (153 pairs x 5 = ~765 matches) and
normalises teams, players and venues into lookup tables, which gets ~11MB of CSV
down to ~600KB of JSON.

If weather_cache.json (written by fetch_weather.py) sits alongside, each match
also gets a compact weather entry; matches without one get null.

Usage:  python3 build_h2h_json.py [master.csv] [out.json]
Defaults: afl_player_games_2015_2026.csv -> h2h_compact.json

No third-party dependencies - stdlib only, so CI needs nothing installed.
"""
import csv, json, collections, sys, os

MASTER = sys.argv[1] if len(sys.argv) > 1 else "afl_player_games_2015_2026.csv"
OUT    = sys.argv[2] if len(sys.argv) > 2 else "h2h_compact.json"
WEATHER = sys.argv[3] if len(sys.argv) > 3 else "weather_cache.json"
LAST_N = 5

rows = list(csv.DictReader(open(MASTER)))
print(f"read {len(rows):,} player-game rows from {MASTER}")

# Group into matches. The key MUST include time: two clubs can play at the same
# ground on the same day (double-headers at Carrara in 2020), and keying on
# date|venue alone merges them into one four-team "match".
matches = collections.defaultdict(lambda: collections.defaultdict(list))
meta = {}
for r in rows:
    k = (r["date"], r["venue"], r["time"])
    matches[k][r["team"]].append(r)
    meta[k] = r["round"]

bad = [k for k, v in matches.items() if len(v) != 2]
print(f"{len(matches):,} matches | {len(bad)} with != 2 teams")
if bad:
    for k in bad[:5]:
        print("  BAD:", k, list(matches[k]))
    sys.exit("Refusing to build: match grouping is broken.")

# Last N meetings per unordered pair.
pairs = collections.defaultdict(list)
for k, sides in matches.items():
    pairs[tuple(sorted(sides))].append(k)

keep = set()
for ks in pairs.values():
    keep.update(sorted(ks, reverse=True)[:LAST_N])
print(f"{len(pairs)} club pairs | {len(keep)} matches kept")

teams, players, venues = {}, {}, {}
def idx(d, v):
    if v not in d:
        d[v] = len(d)
    return d[v]

recs = []
for k in sorted(keep, reverse=True):
    date, venue, time = k
    parts = [f"{date.replace('-','')},{time.replace(':','')},{idx(venues,venue)},{meta[k]}"]
    for tm, ps in matches[k].items():
        plist = "|".join(
            f"{idx(players,p['player'])},{p['disposals'] or 0},{p['kicks'] or 0},"
            f"{p['handballs'] or 0},{p['goals'] or 0},{p['time_on_ground_pct'] or 0}"
            for p in ps)
        parts.append(f"{idx(teams,tm)},{ps[0]['team_score']}~{plist}")
    recs.append("^".join(parts))

# Weather, parallel to "m": null, or [status index into "ws", rain mm, avg wind
# km/h, max gust km/h, max temp C]. The id must match fetch_weather.match_id().
weather = {}
if os.path.exists(WEATHER):
    try:
        weather = json.load(open(WEATHER))
    except ValueError as e:
        print(f"warning: couldn't read {WEATHER} ({e}) - building without weather")
statuses, wx = {}, []
for k in sorted(keep, reverse=True):
    date, venue, time = k
    w = weather.get(f"{date}|{time}|{venue}")
    wx.append(None if not w or not w.get("status") else
              [idx(statuses, w["status"]), w.get("rain_mm"), w.get("wind_avg_kmh"),
               w.get("gust_max_kmh"), w.get("temp_max_c")])
print(f"weather attached to {sum(x is not None for x in wx)} of {len(wx)} matches")

blob = {
    "t": list(teams), "p": list(players), "v": list(venues), "m": recs,
    "ws": list(statuses), "w": wx,
    "built": __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M"),
    "latest": sorted(keep, reverse=True)[0][0],   # date of most recent match included
}
json.dump(blob, open(OUT, "w"), separators=(",", ":"))
print(f"wrote {OUT}  {os.path.getsize(OUT)/1e6:.3f} MB  "
      f"({len(teams)} teams, {len(players)} players, {len(venues)} venues)")
print(f"most recent match included: {blob['latest']}")
