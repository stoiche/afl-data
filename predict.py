#!/usr/bin/env python3
"""
Builds predictions.json for the app's Tips tab.

Inputs : the master player-game CSV, weather_cache.json (fetch_weather.py),
         upcoming.csv (fetch_upcoming.R) and a game-day forecast from Open-Meteo.
Output : predictions.json - upcoming tips, forecast weather, the backtest record
         and a generated timestamp.

How it works, in one walk through history (oldest game first, a round at a time,
every round predicted with only what was known before it):

  * Elo-style team ratings in points, plus a ground advantage whose weights
    (interstate travel, familiarity with the ground, nominal home side) are
    re-fitted from the data as it accumulates. ELO_K is picked from ELO_K_GRID
    on the seasons BEFORE the backtest, so the backtest stays out of sample.
  * Recent-weighted scoring for/against gives a second margin view and a form
    total. Fitted weights blend Elo and form into the margin; the total adds a
    venue effect and a weather-status effect, both learned from residuals.
  * Head-to-head at the venue, and the weather / opponent adjustments to player
    disposals, are each carried as an alternative and only switched on if they
    beat the plain model over the backtest seasons.
  * Every probability comes from the EMPIRICAL distribution of the model's own
    past out-of-sample residuals (no normal curve): margins, totals, and
    standardised disposal residuals pooled across players.

Usage:  python3 predict.py [master.csv] [weather_cache.json] [upcoming.csv] [predictions.json]

No third-party dependencies - stdlib only, so CI needs nothing installed.
"""
import bisect, collections, csv, datetime, json, math, os, sys, time
import urllib.error, urllib.parse, urllib.request

import fetch_weather as fw   # VENUES, norm, classify, parse_hm, HOURLY, GAME_HOURS

# ---------------------------------------------------------------------------
# Tunable settings
# ---------------------------------------------------------------------------
MIN_PROB            = 0.90            # the ONE cut-off every market uses

BACKTEST_SEASONS    = (2025, 2026)
BUCKETS             = ((0.90, 0.93, "90\u201393"), (0.93, 0.96, "93\u201396"), (0.96, 1.01, "96+"))
BURN_IN_GAMES       = 200             # residuals/fits ignored until this many games seen
MIN_RESIDUALS       = 400             # no probabilities until this many residuals banked

# Team strength (Elo in points: rating gap = expected margin on neutral turf)
ELO_K_GRID          = (0.05, 0.065, 0.08, 0.095, 0.11)   # chosen on pre-backtest seasons
ELO_K_DEFAULT       = 0.08
ELO_CARRY           = 0.65            # share of a rating kept over the off-season
ELO_MARGIN_CAP      = 80              # blowouts count as this at most when updating
ADV_PRIOR           = (8.0, 2.0, 0.0) # pts: travel, familiarity (per log-game), nominal home
ADV_RIDGE           = 60.0            # pull of the prior until the data outweighs it
FAM_WINDOW_DAYS     = 1100            # "knows the ground" = games there in ~3 seasons

# Scoring form
FORM_ALPHA          = 0.12            # per-game weight of the newest score (recent-weighted)
FORM_CARRY          = 0.70            # share of for/against form kept over the off-season
LEAGUE_ALPHA        = 0.01            # how fast the league-average score drifts
LEAGUE_INIT         = 85.0
BLEND_PRIOR         = (1.0, 0.0)      # margin = b0*elo view + b1*form view (fitted)
BLEND_RIDGE         = 20000.0
TOTAL_FORM_PRIOR    = 0.6             # how much of the form total to believe (fitted)
TOTAL_RIDGE         = 20000.0
VENUE_SHRINK_GAMES  = 25              # venue effect = residual sum / (n + this)
WX_SHRINK_GAMES     = 30              # weather effect on totals, same shrink
SEASON_SCALE        = {2020: 1.25}    # 16-minute quarters: lift 2020 scores & disposals

# Head-to-head at the venue (only used if it beats the plain model in backtest)
H2H_LAST_N          = 5
H2H_MAX_AGE_DAYS    = 2200
H2H_SHRINK          = 6.0
H2H_MIN_GAIN        = 0.05            # points of margin MAE it must save to count

# Disposals
DISP_THRESHOLDS     = (15, 20, 25, 30, 35)
DISP_LEGS           = 4
DISP_LAST_N         = 10
DISP_DECAY          = 0.90            # weight of each older game relative to the next newer
DISP_MIN_GAMES      = 5
DISP_MIN_TOG        = 65.0
DISP_MAX_AGE_DAYS   = 550
DISP_SD_SHRINK      = 8.0             # pseudo-games of pooled spread mixed into a player's own
DISP_WX_SHRINK      = 1500            # player-games before a weather factor is believed
DISP_OPP_ALPHA      = 0.15            # recent-weighting of disposals conceded
DISP_OPP_WEIGHT     = 0.5             # share of the conceded ratio applied
DISP_MIN_GAIN       = 0.002           # relative Brier improvement needed to switch on
DISP_MIN_Z          = 3000            # pooled residuals needed before any disposal pick
NAMED_MIN_PLAYERS   = 18              # fewer than this in upcoming.csv = team not yet named
SKIP_POSITIONS      = ("EMERG", "EMG", "SUB")

FORECAST_API        = "https://api.open-meteo.com/v1/forecast"
FORECAST_RETRIES    = 2

MASTER   = sys.argv[1] if len(sys.argv) > 1 else "afl_player_games_2015_2026.csv"
WEATHER  = sys.argv[2] if len(sys.argv) > 2 else "weather_cache.json"
UPCOMING = sys.argv[3] if len(sys.argv) > 3 else "upcoming.csv"
OUT      = sys.argv[4] if len(sys.argv) > 4 else "predictions.json"

# ---------------------------------------------------------------------------
# Clubs. AFL Tables, fitzRoy and the AFL API all spell them differently, so
# everything is matched on a key; the app shows the master CSV's spelling.
# ---------------------------------------------------------------------------
_T = [
    ("ADE", "Australia/Adelaide",  ["Adelaide", "Adelaide Crows", "Crows"]),
    ("BRL", "Australia/Brisbane",  ["Brisbane Lions", "Brisbane", "Lions"]),
    ("CAR", "Australia/Melbourne", ["Carlton", "Carlton Blues", "Blues"]),
    ("COL", "Australia/Melbourne", ["Collingwood", "Collingwood Magpies", "Magpies"]),
    ("ESS", "Australia/Melbourne", ["Essendon", "Essendon Bombers", "Bombers"]),
    ("FRE", "Australia/Perth",     ["Fremantle", "Fremantle Dockers", "Dockers"]),
    ("GEE", "Australia/Melbourne", ["Geelong", "Geelong Cats", "Cats"]),
    ("GCS", "Australia/Brisbane",  ["Gold Coast", "Gold Coast Suns", "Suns"]),
    ("GWS", "Australia/Sydney",    ["Greater Western Sydney", "GWS", "GWS Giants",
                                    "Greater Western Sydney Giants", "Giants"]),
    ("HAW", "Australia/Melbourne", ["Hawthorn", "Hawthorn Hawks", "Hawks"]),
    ("MEL", "Australia/Melbourne", ["Melbourne", "Melbourne Demons", "Demons"]),
    ("NTH", "Australia/Melbourne", ["North Melbourne", "North Melbourne Kangaroos",
                                    "Kangaroos", "North"]),
    ("PTA", "Australia/Adelaide",  ["Port Adelaide", "Port Adelaide Power", "Power"]),
    ("RIC", "Australia/Melbourne", ["Richmond", "Richmond Tigers", "Tigers"]),
    ("STK", "Australia/Melbourne", ["St Kilda", "St Kilda Saints", "Saints"]),
    ("SYD", "Australia/Sydney",    ["Sydney", "Sydney Swans", "Swans"]),
    ("WCE", "Australia/Perth",     ["West Coast", "West Coast Eagles", "Eagles"]),
    ("WBD", "Australia/Melbourne", ["Western Bulldogs", "Bulldogs", "Footscray"]),
    ("TAS", "Australia/Hobart",    ["Tasmania", "Tasmania Devils", "Devils"]),
]
TEAM_KEY, TEAM_TZ = {}, {}
for _k, _tz, _names in _T:
    TEAM_TZ[_k] = _tz
    for _n in _names:
        TEAM_KEY[fw.norm(_n)] = _k

def team_key(name):
    return TEAM_KEY.get(fw.norm(name or ""), fw.norm(name or "") or "?")

def venue_key(name):
    """All of a ground's names share one lat/lon in fetch_weather.VENUES."""
    v = fw.VENUES.get(fw.norm(name or ""))
    return (v["lat"], v["lon"]) if v else ("?", fw.norm(name or ""))

def venue_info(name):
    return fw.VENUES.get(fw.norm(name or ""))

def ordinal(date):
    return datetime.date(int(date[:4]), int(date[5:7]), int(date[8:10])).toordinal()

def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------------------
# Small numeric helpers
# ---------------------------------------------------------------------------
class Emp:
    """Empirical distribution of residuals. Probabilities are plain tail counts."""
    def __init__(self):
        self.v = []
    def add(self, x):
        bisect.insort(self.v, x)
    def __len__(self):
        return len(self.v)
    def p_gt(self, x):
        n = len(self.v)
        return (n - bisect.bisect_right(self.v, x) + 0.5) / (n + 1)
    def p_lt(self, x):
        n = len(self.v)
        return (bisect.bisect_left(self.v, x) + 0.5) / (n + 1)
    def q(self, p):
        n = len(self.v)
        return self.v[min(n - 1, max(0, int(p * n)))]


class Ridge:
    """Online least squares, shrunk towards a prior. solve() is cheap (<=3 terms)."""
    def __init__(self, prior, lam):
        self.p, self.lam, k = list(prior), lam, len(prior)
        self.A = [[0.0] * k for _ in range(k)]
        self.b = [0.0] * k
        self.coef = list(prior)
    def add(self, x, y):
        for i, xi in enumerate(x):
            self.b[i] += xi * y
            for j, xj in enumerate(x):
                self.A[i][j] += xi * xj
    def solve(self):
        k = len(self.p)
        M = [self.A[i][:] + [self.b[i] + self.lam * self.p[i]] for i in range(k)]
        for i in range(k):
            M[i][i] += self.lam
        for c in range(k):
            piv = max(range(c, k), key=lambda r: abs(M[r][c]))
            if abs(M[piv][c]) < 1e-9:
                return
            M[c], M[piv] = M[piv], M[c]
            for r in range(k):
                if r != c:
                    f = M[r][c] / M[c][c]
                    M[r] = [a - f * b for a, b in zip(M[r], M[c])]
        self.coef = [M[i][k] / M[i][i] for i in range(k)]


def dot(a, b):
    return sum(x * y for x, y in zip(a, b))


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------
def parse_score(s):
    """'12.10' (goals.behinds) -> 82. A bare integer is taken as points."""
    s = (s or "").strip()
    if not s:
        return None
    try:
        if "." in s:
            g, b = s.split(".", 1)
            return int(g) * 6 + int(b)
        return int(float(s))
    except ValueError:
        return None

def num(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return 0.0

def load_games(path, wx_cache):
    with open(path, newline="") as f:
        rd = csv.DictReader(f)
        cols = {fw.norm(c): c for c in (rd.fieldnames or [])}
        home_team_col = next((cols[c] for c in ("hometeam", "home") if c in cols), None)
        home_flag_col = next((cols[c] for c in ("homeaway", "ishome", "ha") if c in cols), None)
        groups = collections.OrderedDict()
        for r in rd:
            k = (r["date"], r["time"], r["venue"])
            groups.setdefault(k, collections.OrderedDict()).setdefault(r["team"], []).append(r)

    games, skipped = [], 0
    for (date, tm, venue), sides in groups.items():
        if len(sides) != 2:
            skipped += 1
            continue
        teams = list(sides)
        scores = [parse_score(sides[t][0].get("team_score")) for t in teams]
        if None in scores:
            skipped += 1
            continue
        home = None
        if home_team_col:
            hv = team_key(sides[teams[0]][0].get(home_team_col))
            home = next((t for t in teams if team_key(t) == hv), None)
        elif home_flag_col:
            for t in teams:
                if fw.norm(sides[t][0].get(home_flag_col, "")) in ("home", "h", "true", "1", "yes"):
                    home = t
        if home == teams[1] or (home is None and teams[1] < teams[0]):
            teams.reverse(); scores.reverse()
        year = int(date[:4])
        sc = SEASON_SCALE.get(year, 1.0)
        w = wx_cache.get(fw.match_id(date, tm, venue)) or {}
        games.append({
            "date": date, "time": tm, "ord": ordinal(date), "year": year,
            "round": sides[teams[0]][0].get("round", ""),
            "venue": venue, "vkey": venue_key(venue), "vinfo": venue_info(venue),
            "teams": teams, "keys": [team_key(t) for t in teams],
            "scores": [s * sc for s in scores], "has_home": home is not None,
            "wx": w.get("status"),
            "players": [[(p["player"], num(p.get("disposals")) * sc,
                          num(p.get("time_on_ground_pct"))) for p in sides[t]] for t in teams],
        })
    games.sort(key=lambda g: (g["date"], fw.parse_hm(g["time"]) or (0, 0)))
    if skipped:
        log(f"warning: {skipped} matches skipped (not two teams, or no score)")
    return games

def rounds_of(games):
    """Consecutive games sharing season+round. A postponed game forms its own group."""
    out, cur, key = [], [], None
    for g in games:
        k = (g["year"], g["round"])
        if k != key and cur:
            out.append(cur); cur = []
        key = k
        cur.append(g)
    if cur:
        out.append(cur)
    return out


# ---------------------------------------------------------------------------
# Team model
# ---------------------------------------------------------------------------
class TeamModel:
    def __init__(self, k):
        self.k = k
        self.elo = collections.defaultdict(float)
        self.pf, self.pa = {}, {}
        self.lg = LEAGUE_INIT
        self.adv = Ridge(ADV_PRIOR, ADV_RIDGE)
        self.blend = Ridge(BLEND_PRIOR, BLEND_RIDGE)
        self.tot = Ridge((TOTAL_FORM_PRIOR,), TOTAL_RIDGE)
        self.venue = collections.defaultdict(lambda: [0.0, 0])
        self.wx = collections.defaultdict(lambda: [0.0, 0])
        self.visits = collections.defaultdict(list)
        self.h2h = collections.defaultdict(list)
        self.season, self.n = None, 0

    def _fam(self, key, vkey, o):
        v = self.visits.get((key, vkey))
        if not v:
            return 0.0
        return math.log1p(len(v) - bisect.bisect_left(v, o - FAM_WINDOW_DAYS))

    def new_season(self, year):
        if self.season is not None and year != self.season:
            for t in self.elo:
                self.elo[t] *= ELO_CARRY
            for d in (self.pf, self.pa):
                for t in d:
                    d[t] = self.lg + FORM_CARRY * (d[t] - self.lg)
        self.season = year

    def predict(self, g, wx=None):
        """g needs keys, vkey, vinfo, ord, has_home. Margin is teams[0] minus teams[1]."""
        a, b = g["keys"]
        vtz = g["vinfo"]["tz"] if g["vinfo"] else None
        away = lambda k: 1.0 if (vtz and TEAM_TZ.get(k) and TEAM_TZ[k] != vtz) else 0.0
        x = [away(b) - away(a),
             self._fam(a, g["vkey"], g["ord"]) - self._fam(b, g["vkey"], g["ord"]),
             1.0 if g["has_home"] else 0.0]
        gap = self.elo[a] - self.elo[b]
        elo_view = gap + dot(self.adv.coef, x)
        lg = self.lg
        ea = self.pf.get(a, lg) + self.pa.get(b, lg) - lg
        eb = self.pf.get(b, lg) + self.pa.get(a, lg) - lg
        margin = dot(self.blend.coef, (elo_view, ea - eb))

        hist = [r for o, r in self.h2h.get((min(a, b), max(a, b), g["vkey"]), [])
                if g["ord"] - o <= H2H_MAX_AGE_DAYS][-H2H_LAST_N:]
        h = (sum(hist) / (len(hist) + H2H_SHRINK)) if hist else 0.0
        if a > b:
            h = -h

        tot_base = 2 * lg + self.tot.coef[0] * (ea + eb - 2 * lg)
        ve = self.venue[g["vkey"]]; v_eff = ve[0] / (ve[1] + VENUE_SHRINK_GAMES)
        we = self.wx[wx] if wx else [0.0, 0]; w_eff = we[0] / (we[1] + WX_SHRINK_GAMES)
        return {"x": x, "gap": gap, "elo_view": elo_view, "form_m": ea - eb, "form_t": ea + eb,
                "lg": lg, "margin": margin, "margin_h2h": margin + h, "tot_base": tot_base,
                "v_eff": v_eff, "w_eff": w_eff, "total": tot_base + v_eff + w_eff}

    def update(self, g, p):
        a, b = g["keys"]
        sa, sb = g["scores"]
        m, t = sa - sb, sa + sb
        if self.n >= BURN_IN_GAMES // 2:
            self.adv.add(p["x"], m - p["gap"])
            self.blend.add((p["elo_view"], p["form_m"]), m)
            self.tot.add((p["form_t"] - 2 * p["lg"],), t - 2 * p["lg"])
            ve = self.venue[g["vkey"]]; ve[0] += t - p["tot_base"]; ve[1] += 1
            if g["wx"]:
                we = self.wx[g["wx"]]; we[0] += t - p["tot_base"] - p["v_eff"]; we[1] += 1
        r = m - p["margin"]
        self.h2h[(min(a, b), max(a, b), g["vkey"])].append((g["ord"], r if a < b else -r))
        d = self.k * (max(-ELO_MARGIN_CAP, min(ELO_MARGIN_CAP, m)) - p["elo_view"])
        self.elo[a] += d; self.elo[b] -= d
        lg = self.lg
        for k_, f, ag in ((a, sa, sb), (b, sb, sa)):
            self.pf[k_] = self.pf.get(k_, lg) + FORM_ALPHA * (f - self.pf.get(k_, lg))
            self.pa[k_] = self.pa.get(k_, lg) + FORM_ALPHA * (ag - self.pa.get(k_, lg))
            self.visits[(k_, g["vkey"])].append(g["ord"])
        self.lg += LEAGUE_ALPHA * ((sa + sb) / 2 - self.lg)
        self.n += 1

    def refit(self):
        if self.n >= BURN_IN_GAMES:
            self.adv.solve(); self.blend.solve(); self.tot.solve()


def tune_k(games):
    train = [g for g in games if g["year"] < min(BACKTEST_SEASONS)]
    if len(train) < 3 * BURN_IN_GAMES:
        return ELO_K_DEFAULT
    best = (None, 1e9)
    for k in ELO_K_GRID:
        tm, err, n = TeamModel(k), 0.0, 0
        for rnd in rounds_of(train):
            tm.new_season(rnd[0]["year"]); tm.refit()
            ps = [tm.predict(g, g["wx"]) for g in rnd]
            for g, p in zip(rnd, ps):
                if tm.n >= BURN_IN_GAMES:
                    err += abs(g["scores"][0] - g["scores"][1] - p["margin"]); n += 1
                tm.update(g, p)
        mae = err / max(n, 1)
        log(f"  ELO_K {k:<6} margin MAE {mae:.2f} on {n} pre-backtest games")
        if mae < best[1]:
            best = (k, mae)
    return best[0]


# ---------------------------------------------------------------------------
# Disposals model. Variants: 0 plain, 1 +weather, 2 +opponent, 3 +both.
# ---------------------------------------------------------------------------
VARIANTS = ("plain", "weather", "opponent", "weather+opponent")

class DispModel:
    def __init__(self):
        self.hist = collections.defaultdict(list)   # name -> [(ord, team key, disposals, tog)]
        self.z = [Emp() for _ in VARIANTS]
        self.c_sum, self.c_n = 0.0, 0                # pooled spread: var ~ c * mean
        self.all = [0.0, 0.0]                        # actual, expected
        self.wx = collections.defaultdict(lambda: [0.0, 0.0, 0])
        self.opp = {}                                # defending team -> conceded ratio (EW)

    def base(self, name, tkey, o):
        """(mean, spread) from the last ~10 games, newest weighted most. None if ineligible."""
        h = [e for e in self.hist.get(name, ())[-40:] if o - e[0] <= DISP_MAX_AGE_DAYS]
        own = [e for e in h if e[1] == tkey]
        if len(own) >= DISP_MIN_GAMES:        # also separates namesakes at different clubs
            h = own
        h = h[-DISP_LAST_N:]
        if len(h) < DISP_MIN_GAMES:
            return None
        ws = [DISP_DECAY ** i for i in range(len(h) - 1, -1, -1)]
        W = sum(ws)
        togs = [(w, e[3]) for w, e in zip(ws, h) if e[3] > 0]
        if togs and sum(w * t for w, t in togs) / sum(w for w, _ in togs) < DISP_MIN_TOG:
            return None
        mu = sum(w * e[2] for w, e in zip(ws, h)) / W
        if mu < 3:
            return None
        var = sum(w * (e[2] - mu) ** 2 for w, e in zip(ws, h)) / W
        c = self.c_sum / self.c_n if self.c_n > 500 else 1.6
        n = len(h)
        sd = math.sqrt((n * var + DISP_SD_SHRINK * c * mu) / (n + DISP_SD_SHRINK))
        return mu, max(sd, 2.0)

    def factors(self, wx, opp_key):
        r_all = self.all[0] / self.all[1] if self.all[1] else 1.0
        fw_ = 1.0
        if wx and self.wx[wx][2]:
            a, e, n = self.wx[wx]
            fw_ = 1 + ((a / e) / r_all - 1) * n / (n + DISP_WX_SHRINK)
        fo = 1 + DISP_OPP_WEIGHT * (self.opp.get(opp_key, 1.0) - 1)
        return fw_, fo

    def means(self, mu, wx, opp_key):
        fw_, fo = self.factors(wx, opp_key)
        return (mu, mu * fw_, mu * fo, mu * fw_ * fo)

    def prob(self, v, mu_v, sd, thr):
        return self.z[v].p_gt((thr - 0.5 - mu_v) / sd)

    def best_leg(self, v, mu_v, sd):
        for thr in reversed(DISP_THRESHOLDS):
            p = self.prob(v, mu_v, sd, thr)
            if p >= MIN_PROB:
                return thr, p
        return None

    def update(self, g, preds):
        """preds[side] = {name: (mu, sd, means)} made before the game."""
        r_all = self.all[0] / self.all[1] if self.all[1] else 1.0
        for side in (0, 1):
            tkey, okey = g["keys"][side], g["keys"][1 - side]
            sa = se = 0.0
            for name, d, tog in g["players"][side]:
                pr = preds[side].get(name)
                if pr:
                    mu, sd, means = pr
                    for v in range(len(VARIANTS)):
                        self.z[v].add((d - means[v]) / sd)
                    self.c_sum += (d - mu) ** 2 / mu; self.c_n += 1
                    sa += d; se += mu
                    if g["wx"]:
                        w = self.wx[g["wx"]]; w[0] += d; w[1] += mu; w[2] += 1
            if se > 0:
                self.all[0] += sa; self.all[1] += se
                ratio = (sa / se) / r_all
                self.opp[okey] = self.opp.get(okey, 1.0) + DISP_OPP_ALPHA * (ratio - self.opp.get(okey, 1.0))
        for side in (0, 1):
            for name, d, tog in g["players"][side]:
                self.hist[name].append((g["ord"], g["keys"][side], d, tog))


# ---------------------------------------------------------------------------
# Markets - all from empirical residuals, all against MIN_PROB
# ---------------------------------------------------------------------------
def winner_market(margin, emp):
    pa = emp.p_gt(-margin)
    return pa, 1 - pa

def line_market(margin, emp):
    """Smallest start (to .5) each side covers at MIN_PROB; the smaller one pays best."""
    best = None
    q = emp.q(1 - MIN_PROB)
    for side in (0, 1):
        ms = margin if side == 0 else -margin
        h = math.floor(-q - ms) - 3 + 0.5
        while emp.p_gt(-(ms + h)) < MIN_PROB:
            h += 1
        if best is None or h < best[1]:
            best = (side, h, emp.p_gt(-(ms + h)))
    return best

def total_market(total, emp):
    o = math.floor(total + emp.q(1 - MIN_PROB)) + 3.5
    while emp.p_gt(o - total) < MIN_PROB:
        o -= 1
    u = math.floor(total + emp.q(MIN_PROB)) - 3.5
    while emp.p_lt(u - total) < MIN_PROB:
        u += 1
    over, under = ("over", o, emp.p_gt(o - total)), ("under", u, emp.p_lt(u - total))
    return (over, under) if total - o <= u - total else (under, over)


class Record:
    def __init__(self):
        self.rows = []                     # (prob, landed)
    def add(self, p, landed):
        self.rows.append((p, bool(landed)))
    def summary(self):
        n = len(self.rows); hit = sum(l for _, l in self.rows)
        out = {"picks": n, "landed": hit, "rate": round(hit / n, 4) if n else None, "buckets": []}
        for lo, hi, label in BUCKETS:
            b = [l for p, l in self.rows if lo <= p < hi]
            out["buckets"].append({"label": label, "picks": len(b), "landed": sum(b),
                                   "rate": round(sum(b) / len(b), 4) if b else None})
        return out


# ---------------------------------------------------------------------------
# Walk forward through history
# ---------------------------------------------------------------------------
def disp_preds(dm, g, lineups=None):
    """Per side {name: (mu, sd, means)}. lineups overrides who is playing (upcoming games)."""
    out = []
    for side in (0, 1):
        names = lineups[side] if lineups else [p[0] for p in g["players"][side]]
        d = {}
        for name in names:
            b = dm.base(name, g["keys"][side], g["ord"])
            if b:
                d[name] = (b[0], b[1], dm.means(b[0], g["wx"], g["keys"][1 - side]))
        out.append(d)
    return out

def pick_legs(dm, v, preds, teams):
    legs = []
    for side in (0, 1):
        for name, (mu, sd, means) in preds[side].items():
            best = dm.best_leg(v, means[v], sd)
            if best:
                legs.append({"player": name, "team": teams[side], "threshold": best[0],
                             "prob": best[1], "avg": means[v]})
    legs.sort(key=lambda l: (-l["threshold"], -l["prob"]))
    return legs[:DISP_LEGS]

def walk(games, k):
    tm, dm = TeamModel(k), DispModel()
    res_m = [Emp(), Emp()]            # margin residuals: plain, with h2h (kept symmetric)
    res_t = Emp()
    bt = {"winner": [Record(), Record()], "line": [Record(), Record()], "total": Record(),
          "disp": [Record() for _ in VARIANTS]}
    multi = [[0, 0, 0.0] for _ in VARIANTS]       # multis, landed, summed model prob
    mae = [0.0, 0.0, 0.0, 0]                       # margin plain, margin h2h, total, n
    brier = [[0.0, 0] for _ in VARIANTS]
    bt_games = 0

    for rnd in rounds_of(games):
        tm.new_season(rnd[0]["year"]); tm.refit()
        tps = [tm.predict(g, g["wx"]) for g in rnd]
        dps = [disp_preds(dm, g) for g in rnd]
        testing = rnd[0]["year"] in BACKTEST_SEASONS and len(res_t) >= MIN_RESIDUALS

        if testing:
            for g, p, dp in zip(rnd, tps, dps):
                m, t = g["scores"][0] - g["scores"][1], g["scores"][0] + g["scores"][1]
                bt_games += 1
                mae[0] += abs(m - p["margin"]); mae[1] += abs(m - p["margin_h2h"])
                mae[2] += abs(t - p["total"]); mae[3] += 1
                for i, key in enumerate(("margin", "margin_h2h")):
                    pa, pb = winner_market(p[key], res_m[i])
                    if pa >= MIN_PROB: bt["winner"][i].add(pa, m > 0)
                    if pb >= MIN_PROB: bt["winner"][i].add(pb, m < 0)
                    side, h, ph = line_market(p[key], res_m[i])
                    bt["line"][i].add(ph, (m if side == 0 else -m) + h > 0)
                (kind, ln, pl), _ = total_market(p["total"], res_t)
                bt["total"].add(pl, t > ln if kind == "over" else t < ln)
                if len(dm.z[0]) >= DISP_MIN_Z:
                    actual = [{n_: d for n_, d, _ in g["players"][s]} for s in (0, 1)]
                    for v in range(len(VARIANTS)):
                        for s in (0, 1):
                            for name, (mu, sd, means) in dp[s].items():
                                for thr in DISP_THRESHOLDS:
                                    pr = dm.prob(v, means[v], sd, thr)
                                    brier[v][0] += (pr - (actual[s][name] >= thr)) ** 2
                                    brier[v][1] += 1
                        legs = pick_legs(dm, v, dp, (0, 1))
                        hits = [actual[l["team"]][l["player"]] >= l["threshold"] for l in legs]
                        for l, hit in zip(legs, hits):
                            bt["disp"][v].add(l["prob"], hit)
                        if len(legs) == DISP_LEGS:
                            multi[v][0] += 1; multi[v][1] += all(hits)
                            multi[v][2] += math.prod(l["prob"] for l in legs)

        for g, p, dp in zip(rnd, tps, dps):
            if tm.n >= BURN_IN_GAMES:
                m, t = g["scores"][0] - g["scores"][1], g["scores"][0] + g["scores"][1]
                for i, key in enumerate(("margin", "margin_h2h")):
                    res_m[i].add(m - p[key]); res_m[i].add(p[key] - m)
                res_t.add(t - p["total"])
            tm.update(g, p)
            dm.update(g, dp)

    n = max(mae[3], 1)
    use_h2h = mae[3] > 0 and (mae[0] - mae[1]) / n >= H2H_MIN_GAIN
    bs = [b[0] / b[1] if b[1] else None for b in brier]
    dv = 0
    if bs[0]:
        cand = min(range(len(VARIANTS)), key=lambda v: bs[v])
        if bs[cand] < bs[0] * (1 - DISP_MIN_GAIN):
            dv = cand
    i = 1 if use_h2h else 0
    mv = multi[dv]
    backtest = {
        "seasons": list(BACKTEST_SEASONS), "games": bt_games,
        "margin_mae": round(mae[i] / n, 2), "total_mae": round(mae[2] / n, 2),
        "markets": {"winner": bt["winner"][i].summary(), "line": bt["line"][i].summary(),
                    "total": bt["total"].summary(), "disposals": bt["disp"][dv].summary()},
        "multi": {"legs": DISP_LEGS, "multis": mv[0], "landed": mv[1],
                  "rate": round(mv[1] / mv[0], 4) if mv[0] else None,
                  "avg_model_prob": round(mv[2] / mv[0], 4) if mv[0] else None},
        "checks": {"margin_mae_plain": round(mae[0] / n, 3), "margin_mae_h2h": round(mae[1] / n, 3),
                   "disposal_brier": {VARIANTS[v]: (round(bs[v], 5) if bs[v] else None)
                                      for v in range(len(VARIANTS))}},
    }
    return tm, dm, res_m[i], res_t, use_h2h, dv, backtest


# ---------------------------------------------------------------------------
# Upcoming games
# ---------------------------------------------------------------------------
def read_upcoming(path):
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        return []
    with open(path, newline="") as f:
        rows = [r for r in csv.DictReader(f) if (r.get("home_team") or "").strip()]
    fx = collections.OrderedDict()
    for r in rows:
        k = (r["date"], r["time"], team_key(r["home_team"]), team_key(r["away_team"]))
        g = fx.setdefault(k, {"row": r, "lineup": {}})
        if (r.get("player") or "").strip() and (r.get("position") or "").strip().upper() not in SKIP_POSITIONS:
            g["lineup"].setdefault(team_key(r.get("team")), []).append(
                ((r.get("given") or "").strip(), (r.get("surname") or "").strip(), r["player"].strip()))
    return list(fx.values())

def name_key(s):
    s = s or ""
    if "," in s:                                   # "Surname, Given" -> "Given Surname"
        a, b = s.split(",", 1)
        s = f"{b} {a}"
    return fw.norm(s)

def match_names(lineup, roster):
    """Lineup (given, surname, full) -> the master CSV's spelling, via the club's recent list."""
    by_key = {name_key(n): n for n in roster}
    out = []
    for given, surname, full in lineup:
        hit = by_key.get(name_key(full)) or by_key.get(name_key(f"{given} {surname}"))
        if not hit and surname:
            sk, gi = fw.norm(surname), fw.norm(given)[:1]
            c = [n for k_, n in by_key.items() if k_.endswith(sk) and k_[:1] == gi]
            hit = c[0] if len(c) == 1 else None
        if hit:
            out.append(hit)
    return out

def forecast(v, date, hm):
    """Game-window forecast in the same shape and with the same labels as the history."""
    if v is None or hm is None:
        return None
    if v["roof"]:
        return {"status": "Roof closed", "rain_mm": None, "wind_avg_kmh": None,
                "gust_max_kmh": None, "temp_max_c": None}
    d0 = datetime.date.fromisoformat(date)
    q = urllib.parse.urlencode({
        "latitude": v["lat"], "longitude": v["lon"], "hourly": fw.HOURLY, "timezone": "auto",
        "start_date": d0.isoformat(), "end_date": (d0 + datetime.timedelta(days=1)).isoformat(),
        "wind_speed_unit": "kmh", "precipitation_unit": "mm", "temperature_unit": "celsius"})
    for attempt in range(FORECAST_RETRIES):
        try:
            req = urllib.request.Request(f"{FORECAST_API}?{q}", headers={"User-Agent": "afl-h2h-tips/1.0"})
            with urllib.request.urlopen(req, timeout=45) as r:
                h = json.load(r)["hourly"]
            hourly = {t: (p, tc, ws, wg) for t, p, tc, ws, wg in zip(
                h["time"], h["precipitation"], h["temperature_2m"],
                h["wind_speed_10m"], h["wind_gusts_10m"])}
            base = datetime.datetime(d0.year, d0.month, d0.day, hm[0])
            slots = [hourly.get((base + datetime.timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M"))
                     for i in range(fw.GAME_HOURS + 1)]
            if any(s is None or any(x is None for x in s) for s in slots):
                return None
            w = {"rain_mm": round(sum(s[0] for s in slots), 1),
                 "wind_avg_kmh": round(sum(s[2] for s in slots) / len(slots), 1),
                 "gust_max_kmh": round(max(s[3] for s in slots), 1),
                 "temp_max_c": round(max(s[1] for s in slots), 1)}
            w["status"] = fw.classify(w)
            return w
        except (urllib.error.URLError, OSError, KeyError, ValueError) as e:
            log(f"  forecast attempt {attempt + 1} failed: {e}")
            time.sleep(2)
    return None

def tips(fixtures, games, tm, dm, res_m, res_t, use_h2h, dv):
    shown = {}                                    # club key -> the master CSV's spelling
    last_game = {}
    for g in games:
        for s in (0, 1):
            shown[g["keys"][s]] = g["teams"][s]
            last_game[g["keys"][s]] = (g, s)
    played = {(g["date"], frozenset(g["keys"])) for g in games}
    roster = collections.defaultdict(dict)
    cutoff = (games[-1]["ord"] - 800) if games else 0
    for g in games:
        if g["ord"] >= cutoff:
            for s in (0, 1):
                for name, _, _ in g["players"][s]:
                    roster[g["keys"][s]][name] = 1

    today = datetime.date.today().toordinal()
    out, season, round_name = [], None, None
    for f in fixtures:
        r = f["row"]
        hk, ak = team_key(r["home_team"]), team_key(r["away_team"])
        try:
            o = ordinal(r["date"])
        except (ValueError, IndexError):
            continue
        if o < today - 1 or (r["date"], frozenset((hk, ak))) in played:
            continue
        season, round_name = r.get("season"), r.get("round_name") or r.get("round")
        v = venue_info(r["venue"])
        wx = forecast(v, r["date"], fw.parse_hm(r["time"]))
        g = {"keys": [hk, ak], "vkey": venue_key(r["venue"]), "vinfo": v, "ord": o,
             "has_home": tm.adv.A[2][2] > 0, "wx": wx["status"] if wx else None}
        tm.new_season(int(r["date"][:4]))
        p = tm.predict(g, g["wx"])
        margin = p["margin_h2h"] if use_h2h else p["margin"]
        names = [shown.get(hk, r["home_team"]), shown.get(ak, r["away_team"])]

        notes, lineups = [], []
        for s, k_ in enumerate((hk, ak)):
            lu = f["lineup"].get(k_, [])
            if len(lu) >= NAMED_MIN_PLAYERS:
                lineups.append(match_names(lu, roster[k_]))
            else:
                lg_, ls = last_game.get(k_, (None, 0))
                lineups.append([n for n, _, _ in lg_["players"][ls]] if lg_ else [])
                notes.append(f"{names[s]}: team not yet named \u2013 last game\u2019s side used")

        markets = {"winner": None, "line": None, "total": None, "disposals": None}
        if len(res_m) >= 2 * MIN_RESIDUALS:
            pa, pb = winner_market(margin, res_m)
            fav = 0 if pa >= pb else 1
            markets["winner"] = {"pick": names[fav] if max(pa, pb) >= MIN_PROB else None,
                                 "lean": names[fav], "prob": round(max(pa, pb), 4)}
            side, h, ph = line_market(margin, res_m)
            sign = "+" if h > 0 else "\u2212"
            markets["line"] = {"team": names[side], "line": h, "prob": round(ph, 4),
                               "pick": f"{names[side]} {sign}{abs(h):.1f}"}
            (kind, ln, pl), (k2, l2, p2) = total_market(p["total"], res_t)
            markets["total"] = {"side": kind, "line": ln, "prob": round(pl, 4),
                                "pick": f"{kind.capitalize()} {ln:.1f}",
                                "other": f"{k2.capitalize()} {l2:.1f}"}
        if len(dm.z[dv]) >= DISP_MIN_Z:
            legs = pick_legs(dm, dv, disp_preds(dm, g, lineups), names)
            comb = math.prod(l["prob"] for l in legs) if legs else None
            markets["disposals"] = {
                "legs": [{"player": l["player"], "team": l["team"], "threshold": l["threshold"],
                          "prob": round(l["prob"], 4), "avg": round(l["avg"], 1)} for l in legs],
                "combined": round(comb, 4) if comb else None}

        out.append({
            "id": f"{r['date']}|{hk}|{ak}", "home": names[0], "away": names[1],
            "date": r["date"], "time": r["time"], "venue": r["venue"],
            "round": r.get("round_name") or r.get("round") or "",
            "weather": None if not wx else {"status": wx["status"], "rain": wx["rain_mm"],
                "wind": wx["wind_avg_kmh"], "gust": wx["gust_max_kmh"], "temp": wx["temp_max_c"]},
            "pred": {"margin": round(margin, 1), "total": round(p["total"], 1),
                     "home_score": round((p["total"] + margin) / 2), "away_score": round((p["total"] - margin) / 2)},
            "notes": notes, "markets": markets})
        log(f"  {names[0]} v {names[1]}: margin {margin:+.1f}, total {p['total']:.0f}, "
            f"weather {g['wx'] or 'n/a'}")
    out.sort(key=lambda t: (t["date"], t["time"]))
    return out, season, round_name


# ---------------------------------------------------------------------------
def main():
    wx_cache = {}
    if os.path.exists(WEATHER):
        try:
            with open(WEATHER) as f:
                wx_cache = json.load(f)
        except ValueError as e:
            log(f"warning: couldn't read {WEATHER} ({e}) - modelling without weather")
    games = load_games(MASTER, wx_cache)
    log(f"{len(games):,} matches loaded from {MASTER} | weather on {sum(1 for g in games if g['wx']):,}")
    if not games:
        sys.exit("No matches to model.")
    med = sorted(s for g in games for s in g["scores"])[len(games)]
    if not 50 <= med <= 120:
        log(f"warning: median team score is {med:.0f} - check how team_score is parsed")

    k = tune_k(games)
    log(f"ELO_K = {k}")
    tm, dm, res_m, res_t, use_h2h, dv, backtest = walk(games, k)
    tm.refit()

    log(f"ground advantage (pts): travel {tm.adv.coef[0]:.1f} | per log-game of familiarity "
        f"{tm.adv.coef[1]:.1f} | nominal home {tm.adv.coef[2]:.1f}")
    log("weather effect on totals: " + " | ".join(
        f"{s} {v[0] / (v[1] + WX_SHRINK_GAMES):+.1f} ({v[1]})" for s, v in sorted(tm.wx.items())))
    log(f"head-to-head at venue: {'ON' if use_h2h else 'off'} "
        f"(MAE {backtest['checks']['margin_mae_h2h']} vs {backtest['checks']['margin_mae_plain']})")
    log(f"disposals adjustment: {VARIANTS[dv]}  {backtest['checks']['disposal_brier']}")
    log(f"backtest {backtest['seasons']}: {backtest['games']} games | margin MAE "
        f"{backtest['margin_mae']} | total MAE {backtest['total_mae']}")
    for name, m in backtest["markets"].items():
        bk = " | ".join(f"{b['label']}: {b['landed']}/{b['picks']}" for b in m["buckets"])
        rate = f"{m['rate']:.1%}" if m["rate"] is not None else "n/a"
        log(f"  {name:<10} {m['landed']}/{m['picks']} landed ({rate})   {bk}")
    mu = backtest["multi"]
    log(f"  {DISP_LEGS}-leg multi {mu['landed']}/{mu['multis']} landed "
        f"(model expected {mu['avg_model_prob']})")

    fixtures = read_upcoming(UPCOMING)
    log(f"{len(fixtures)} upcoming fixtures in {UPCOMING}")
    games_out, season, round_name = tips(fixtures, games, tm, dm, res_m, res_t, use_h2h, dv)

    blob = {
        "generated": datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "min_prob": MIN_PROB, "season": season, "round": round_name,
        "data_to": games[-1]["date"],
        "model": {"elo_k": k, "h2h_used": use_h2h, "disposals_adjustment": VARIANTS[dv],
                  "advantage_pts": {"travel": round(tm.adv.coef[0], 2),
                                    "familiarity": round(tm.adv.coef[1], 2),
                                    "home": round(tm.adv.coef[2], 2)},
                  "weather_total_pts": {s: round(v[0] / (v[1] + WX_SHRINK_GAMES), 1)
                                        for s, v in tm.wx.items()},
                  "residuals": {"margin": len(res_m) // 2, "total": len(res_t),
                                "disposals": len(dm.z[dv])}},
        "backtest": backtest, "games": games_out,
    }
    try:                                  # nothing but the timestamp changed: no commit noise
        with open(OUT) as f:
            prev = json.load(f)
        if {**prev, "generated": None} == {**json.loads(json.dumps(blob)), "generated": None}:
            log(f"{OUT} unchanged")
            return
    except (OSError, ValueError):
        pass
    tmp = OUT + ".tmp"
    with open(tmp, "w") as f:
        json.dump(blob, f, separators=(",", ":"), ensure_ascii=False)
    os.replace(tmp, OUT)
    log(f"wrote {OUT}  {os.path.getsize(OUT) / 1e3:.1f} KB  ({len(games_out)} tips)")


if __name__ == "__main__":
    main()
