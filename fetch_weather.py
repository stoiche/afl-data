#!/usr/bin/env python3
"""
Fetches match-time weather for every game in the master CSV from the Open-Meteo
Historical Weather API (free, no key) and caches it in weather_cache.json.

  - One cache entry per match, keyed by match_id() below - the same id
    build_h2h_json.py uses to attach weather to the app's data.
  - Only uncached matches are fetched, batched as one request per venue covering
    that venue's uncached date range (split into chunks of MAX_DAYS_PER_REQUEST
    so the first full backfill doesn't ask for 11 years of hourly data at once).
  - Games under MIN_AGE_DAYS old are skipped (the archive lags); they fill in on
    a later run. Failures and incomplete data are never cached.
  - Statuses are re-derived from the cached numbers on every run, so changing a
    threshold below re-labels the whole history without re-fetching anything.

Usage:  python3 fetch_weather.py [master.csv] [weather_cache.json]
Defaults: afl_player_games_2015_2026.csv -> weather_cache.json

No third-party dependencies - stdlib only, so CI needs nothing installed.
"""
import csv, json, os, re, sys, time, datetime, collections
import urllib.request, urllib.parse, urllib.error

# ---------------------------------------------------------------------------
# Tunable thresholds
# ---------------------------------------------------------------------------
WET_MM          = 2.0    # rain over the game window >= this  -> Wet
PARTLY_WET_MM   = 0.2    # rain >= this (and < WET_MM)        -> Partly wet
WINDY_AVG_KMH   = 25.0   # average wind >= this               -> Windy
WINDY_GUST_KMH  = 45.0   # ...or max gust >= this             -> Windy
HOT_C           = 30.0   # max temperature >= this            -> Hot

GAME_HOURS      = 3      # window = start hour .. start + 3 hours, venue-local
MIN_AGE_DAYS    = 7      # archive lag: leave newer games for a later run
MAX_DAYS_PER_REQUEST = 370   # chunk size for big backfills
PAUSE_SECONDS   = 0.4    # politeness gap between API calls
RETRIES         = 3

# fitzRoy's AFL Tables data carries the *local* start time at the venue
# (its Local.start.time column), so no conversion is needed. If your CSV ever
# holds Melbourne time instead, set this to False and times are converted from
# Australia/Melbourne to each venue's own timezone before the window is cut.
CSV_TIMES_ARE_VENUE_LOCAL = True

API = "https://archive-api.open-meteo.com/v1/archive"
HOURLY = "precipitation,temperature_2m,wind_speed_10m,wind_gusts_10m"

MASTER = sys.argv[1] if len(sys.argv) > 1 else "afl_player_games_2015_2026.csv"
CACHE  = sys.argv[2] if len(sys.argv) > 2 else "weather_cache.json"

# ---------------------------------------------------------------------------
# Venues. (lat, lon, IANA timezone, roofed?, [names it goes by])
# Names are matched case-insensitively with punctuation ignored, so "M.C.G."
# and "MCG" are the same key. AFL Tables names come first, then sponsor names.
# ---------------------------------------------------------------------------
_V = [
    (-37.8200, 144.9834, "Australia/Melbourne", False,
        ["M.C.G.", "MCG", "Melbourne Cricket Ground"]),
    (-37.8165, 144.9475, "Australia/Melbourne", True,
        ["Docklands", "Marvel Stadium", "Etihad Stadium", "Docklands Stadium"]),
    (-38.1580, 144.3546, "Australia/Melbourne", False,
        ["Kardinia Park", "GMHBA Stadium", "Simonds Stadium"]),
    (-37.5394, 143.8481, "Australia/Melbourne", False,
        ["Eureka Stadium", "Mars Stadium", "Ballarat"]),
    (-33.8917, 151.2247, "Australia/Sydney", False,
        ["S.C.G.", "SCG", "Sydney Cricket Ground"]),
    (-33.8431, 151.0678, "Australia/Sydney", False,
        ["Sydney Showground", "Sydney Showground Stadium", "Giants Stadium",
         "ENGIE Stadium", "Spotless Stadium", "Skoda Stadium", "Showground Stadium"]),
    (-33.8472, 151.0634, "Australia/Sydney", False,
        ["Stadium Australia", "ANZ Stadium", "Accor Stadium"]),
    (-35.3181, 149.1347, "Australia/Sydney", False,
        ["Manuka Oval", "Manuka", "UNSW Canberra Oval", "StarTrack Oval",
         "Corroboree Group Oval Manuka"]),
    (-27.4858, 153.0381, "Australia/Brisbane", False,
        ["Gabba", "The Gabba", "Brisbane Cricket Ground"]),
    (-28.0063, 153.3672, "Australia/Brisbane", False,
        ["Carrara", "Metricon Stadium", "Heritage Bank Stadium",
         "People First Stadium", "Carrara Stadium"]),
    (-16.9357, 145.7490, "Australia/Brisbane", False,
        ["Cazaly's Stadium", "Cazalys Stadium", "Cazaly's"]),
    (-19.3175, 146.7311, "Australia/Brisbane", False,
        ["Riverway Stadium", "Riverway"]),
    (-34.9156, 138.5961, "Australia/Adelaide", False,
        ["Adelaide Oval"]),
    (-34.8800, 138.4956, "Australia/Adelaide", False,
        ["Football Park", "AAMI Stadium"]),
    (-34.9197, 138.6300, "Australia/Adelaide", False,
        ["Norwood Oval", "Norwood", "Coopers Stadium"]),
    (-35.0870, 138.8730, "Australia/Adelaide", False,
        ["Summit Sports Park", "Summit Sport and Recreation Park",
         "Adelaide Hills", "Mount Barker"]),
    (-34.6010, 138.8900, "Australia/Adelaide", False,
        ["Barossa Park", "Barossa", "Lyndoch Recreation Park", "Lyndoch"]),
    (-31.9512, 115.8890, "Australia/Perth", False,
        ["Perth Stadium", "Optus Stadium"]),
    (-31.9444, 115.8300, "Australia/Perth", False,
        ["Subiaco", "Subiaco Oval", "Domain Stadium", "Patersons Stadium"]),
    (-33.3400, 115.6470, "Australia/Perth", False,
        ["Hands Oval", "Bunbury"]),
    (-42.8772, 147.3736, "Australia/Hobart", False,
        ["Bellerive Oval", "Blundstone Arena", "Ninja Stadium", "Bellerive"]),
    (-41.4259, 147.1389, "Australia/Hobart", False,
        ["York Park", "UTAS Stadium", "University of Tasmania Stadium",
         "Aurora Stadium"]),
    (-12.3992, 130.8872, "Australia/Darwin", False,
        ["Marrara Oval", "Marrara", "TIO Stadium"]),
    (-23.7090, 133.8750, "Australia/Darwin", False,
        ["Traeger Park", "TIO Traeger Park"]),
    (31.3075, 121.5172, "Asia/Shanghai", False,
        ["Jiangwan Stadium", "Adelaide Arena at Jiangwan Stadium", "Shanghai"]),
    (-41.2730, 174.7859, "Pacific/Auckland", False,
        ["Wellington", "Westpac Stadium", "Sky Stadium", "Wellington Regional Stadium"]),
]

def norm(name):
    return re.sub(r"[^a-z0-9]", "", name.lower())

VENUES = {}
for lat, lon, tz, roof, names in _V:
    for n in names:
        VENUES[norm(n)] = {"lat": lat, "lon": lon, "tz": tz, "roof": roof}


# ---------------------------------------------------------------------------
def match_id(date, time_, venue):
    """Shared with build_h2h_json.py - raw CSV values, same fields it groups on."""
    return f"{date}|{time_}|{venue}"

def classify(w):
    if w.get("status") == "Roof closed" and w.get("rain_mm") is None:
        return "Roof closed"
    wet    = w["rain_mm"] >= WET_MM
    partly = w["rain_mm"] >= PARTLY_WET_MM
    windy  = w["wind_avg_kmh"] >= WINDY_AVG_KMH or w["gust_max_kmh"] >= WINDY_GUST_KMH
    hot    = w["temp_max_c"] >= HOT_C
    if wet and windy: return "Wet & windy"
    if wet:           return "Wet"
    if partly:        return "Partly wet"
    if windy:         return "Windy"
    if hot:           return "Hot"
    return "Dry"

def parse_hm(t):
    """'19:40', '19:40:00', '1940' or '7:40 PM' -> (19, 40). None if unreadable."""
    t = (t or "").strip()
    m = re.match(r"^(\d{1,2}):?(\d{2})(?::\d{2})?\s*([AaPp][Mm])?$", t)
    if not m:
        return None
    h, mi = int(m.group(1)), int(m.group(2))
    if m.group(3):
        h = h % 12 + (12 if m.group(3).lower() == "pm" else 0)
    return (h, mi) if h < 24 and mi < 60 else None

def local_start(date, hm, tz):
    """Naive venue-local datetime of the bounce."""
    dt = datetime.datetime.strptime(date, "%Y-%m-%d").replace(hour=hm[0], minute=hm[1])
    if CSV_TIMES_ARE_VENUE_LOCAL:
        return dt
    from zoneinfo import ZoneInfo
    return (dt.replace(tzinfo=ZoneInfo("Australia/Melbourne"))
              .astimezone(ZoneInfo(tz)).replace(tzinfo=None))

def fetch(v, start, end):
    q = urllib.parse.urlencode({
        "latitude": v["lat"], "longitude": v["lon"],
        "start_date": start.isoformat(), "end_date": end.isoformat(),
        "hourly": HOURLY, "timezone": "auto",
        "wind_speed_unit": "kmh", "precipitation_unit": "mm", "temperature_unit": "celsius",
    })
    last = None
    for attempt in range(RETRIES):
        try:
            req = urllib.request.Request(f"{API}?{q}", headers={"User-Agent": "afl-h2h-weather/1.0"})
            with urllib.request.urlopen(req, timeout=60) as r:
                h = json.load(r)["hourly"]
            return {t: (p, tc, ws, wg) for t, p, tc, ws, wg in zip(
                h["time"], h["precipitation"], h["temperature_2m"],
                h["wind_speed_10m"], h["wind_gusts_10m"])}
        except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
            last = e
            time.sleep(2 * (attempt + 1))
    raise RuntimeError(last)


# ---------------------------------------------------------------------------
def main():
    seen = set()
    with open(MASTER, newline="") as f:
        for r in csv.DictReader(f):
            seen.add((r["date"], r["time"], r["venue"]))
    print(f"{len(seen):,} unique matches in {MASTER}")

    missing = sorted({v for _, _, v in seen if norm(v) not in VENUES})
    if missing:
        print("Venues in the CSV with no entry in VENUES - add them and re-run:")
        for v in missing:
            print("   ", repr(v))
        sys.exit(1)

    cache = {}
    if os.path.exists(CACHE):
        with open(CACHE) as f:
            cache = json.load(f)
    before = json.dumps(cache, sort_keys=True)

    today = datetime.date.today()
    todo = collections.defaultdict(list)       # venue key -> [(id, local start dt)]
    too_new = bad_time = odd_hour = 0
    for date, tm, venue in seen:
        mid = match_id(date, tm, venue)
        if mid in cache:
            continue
        v = VENUES[norm(venue)]
        if v["roof"]:
            cache[mid] = {"rain_mm": None, "wind_avg_kmh": None, "gust_max_kmh": None,
                          "temp_max_c": None, "status": "Roof closed"}
            continue
        hm = parse_hm(tm)
        if hm is None:
            bad_time += 1
            continue
        dt = local_start(date, hm, v["tz"])
        if not 10 <= dt.hour <= 21:
            odd_hour += 1
        if (today - dt.date()).days < MIN_AGE_DAYS:
            too_new += 1
            continue
        todo[norm(venue)].append((mid, dt))

    if bad_time:
        print(f"warning: {bad_time} matches have an unreadable start time and were skipped")
    if odd_hour:
        print(f"warning: {odd_hour} matches start before 10:00 or after 21:59 local - "
              f"check CSV_TIMES_ARE_VENUE_LOCAL if that looks wrong")
    print(f"{sum(map(len, todo.values()))} matches to fetch across {len(todo)} venues "
          f"| {too_new} too recent, left for a later run")

    fetched = failed = calls = 0
    for vkey, games in sorted(todo.items()):
        v = VENUES[vkey]
        games.sort(key=lambda g: g[1])
        # Chunk the venue's uncached date range. Normally that's a single request.
        chunks, cur = [], [games[0]]
        for g in games[1:]:
            if (g[1].date() - cur[0][1].date()).days > MAX_DAYS_PER_REQUEST:
                chunks.append(cur); cur = [g]
            else:
                cur.append(g)
        chunks.append(cur)

        for chunk in chunks:
            start = chunk[0][1].date()
            # +1 day so a late game's window can run past midnight
            end = min(chunk[-1][1].date() + datetime.timedelta(days=1),
                      today - datetime.timedelta(days=1))
            try:
                hourly = fetch(v, start, end)
                calls += 1
            except RuntimeError as e:
                failed += len(chunk)
                print(f"  {vkey} {start}..{end}: request failed ({e}) - will retry next run")
                continue
            for mid, dt in chunk:
                base = dt.replace(minute=0)
                slots = [hourly.get((base + datetime.timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M"))
                         for i in range(GAME_HOURS + 1)]
                if any(s is None or any(x is None for x in s) for s in slots):
                    failed += 1            # archive hasn't caught up; never cache gaps
                    continue
                w = {"rain_mm":      round(sum(s[0] for s in slots), 1),
                     "wind_avg_kmh": round(sum(s[2] for s in slots) / len(slots), 1),
                     "gust_max_kmh": round(max(s[3] for s in slots), 1),
                     "temp_max_c":   round(max(s[1] for s in slots), 1)}
                w["status"] = classify(w)
                cache[mid] = w
                fetched += 1
            time.sleep(PAUSE_SECONDS)

    # Re-label everything so threshold changes apply to the whole history.
    for w in cache.values():
        w["status"] = classify(w)

    if json.dumps(cache, sort_keys=True) != before:
        tmp = CACHE + ".tmp"
        with open(tmp, "w") as f:
            json.dump(cache, f, separators=(",", ":"), sort_keys=True)
        os.replace(tmp, CACHE)

    tally = collections.Counter(w["status"] for w in cache.values())
    print(f"{calls} API calls | {fetched} fetched | {failed} not available yet | "
          f"{len(cache):,} matches cached")
    print("  " + " | ".join(f"{k}: {n}" for k, n in tally.most_common()))


if __name__ == "__main__":
    main()
