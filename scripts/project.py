"""Project end-of-season standings from betting-market game lines.

1. Fit one rating per team (plus home court) to the no-vig moneylines saved in
   data/games.json: logit(P home wins) = r_home - r_away + h. Recent lines count more,
   and a light ridge penalty keeps teams with few lines near average.
2. Simulate the rest of the season many times. Each simulated season every team's rating
   drifts by a random amount that shrinks as the season runs out (injuries, trades,
   development), so October projections aren't overconfident.
3. Score every drafter in each simulated season and write drafter-level results only.
   Team ratings and side-level projections are deliberately not written: the site is public.

Standard library only.
"""
import json
import math
import os
import random
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAMES = os.path.join(ROOT, "data", "games.json")
PICKS = os.path.join(ROOT, "data", "picks.json")
OUT = os.path.join(ROOT, "data", "projection.json")
HIST = os.path.join(ROOT, "data", "projection_history.json")

SIMS = 10000
HALF_LIFE_DAYS = 30      # weight of a line halves every 30 days
RIDGE = 1.0              # pull toward average, in units of "games of evidence"
DRIFT_FULL_SEASON = 0.30  # sd of a team's rating change over a full season (logit units, ~6 wins)
SEASON_GAMES = 82
ET = ZoneInfo("America/New_York")


def logit(p):
    p = min(max(p, 0.02), 0.98)
    return math.log(p / (1 - p))


def solve(a, b):
    """Gaussian elimination with partial pivoting for a small dense system."""
    n = len(b)
    m = [row[:] + [b[i]] for i, row in enumerate(a)]
    for c in range(n):
        piv = max(range(c, n), key=lambda r: abs(m[r][c]))
        m[c], m[piv] = m[piv], m[c]
        for r in range(n):
            if r != c and m[r][c]:
                f = m[r][c] / m[c][c]
                for k in range(c, n + 1):
                    m[r][k] -= f * m[c][k]
    return [m[i][n] / m[i][i] for i in range(n)]


def fit_ratings(games, teams, today):
    idx = {t: i for i, t in enumerate(teams)}
    n = len(teams) + 1  # last slot = home court
    ata = [[0.0] * n for _ in range(n)]
    atb = [0.0] * n
    used = 0
    for g in games:
        o = g.get("odds")
        if not o or g["home"] not in idx or g["away"] not in idx:
            continue
        age = max(0, (today - date.fromisoformat(g["date"])).days)
        w = 0.5 ** (age / HALF_LIFE_DAYS)
        x = {idx[g["home"]]: 1.0, idx[g["away"]]: -1.0, n - 1: 1.0}
        y = logit(o["homeProb"])
        for i, xi in x.items():
            atb[i] += w * xi * y
            for j, xj in x.items():
                ata[i][j] += w * xi * xj
        used += 1
    for i in range(n - 1):
        ata[i][i] += RIDGE
    ata[n - 1][n - 1] += 1e-6
    sol = solve(ata, atb)
    return {t: sol[idx[t]] for t in teams}, sol[n - 1], used


def main():
    store = json.load(open(GAMES))
    picks = json.load(open(PICKS))
    games = sorted(store["games"].values(), key=lambda g: g["start"])
    teams = sorted({g["home"] for g in games} | {g["away"] for g in games} - {"TBD"})
    today = datetime.now(ET).date()

    rating, hca, used = fit_ratings(games, teams, today)

    wins = {t: 0 for t in teams}
    losses = {t: 0 for t in teams}
    known = {t: 0 for t in teams}
    remaining = []
    for g in games:
        if g["home"] not in rating or g["away"] not in rating:
            continue  # TBD Cup knockout slots
        known[g["home"]] += 1
        known[g["away"]] += 1
        if g["final"]:
            lose = g["away"] if g["winner"] == g["home"] else g["home"]
            wins[g["winner"]] += 1
            losses[lose] += 1
        else:
            # Use the game's own line when the market has posted one; otherwise the ratings.
            base = logit(g["odds"]["homeProb"]) if g.get("odds") else rating[g["home"]] - rating[g["away"]] + hca
            remaining.append((g["home"], g["away"], base))
    # Games the NBA hasn't scheduled yet (post-Cup slots): neutral site vs. an average team.
    unscheduled = [(t, SEASON_GAMES - known[t]) for t in teams if known[t] < SEASON_GAMES]

    played = sum(wins.values())
    frac_left = 1 - played / (len(teams) * SEASON_GAMES / 2)
    drift_sd = DRIFT_FULL_SEASON * math.sqrt(max(frac_left, 0.0))

    drafters = picks["drafters"]
    sides = [(p["drafter"], p["team"], p["side"]) for p in picks["picks"]]
    totals = {d: [] for d in drafters}
    first = {d: 0.0 for d in drafters}
    place = {d: [0.0] * len(drafters) for d in drafters}

    rng = random.Random(20270411)
    exp = math.exp
    rand = rng.random
    for _ in range(SIMS):
        shift = {t: rng.gauss(0, drift_sd) for t in teams}
        w = dict(wins)
        l = dict(losses)
        for h, a, base in remaining:
            if rand() < 1 / (1 + exp(-(base + shift[h] - shift[a]))):
                w[h] += 1; l[a] += 1
            else:
                w[a] += 1; l[h] += 1
        for t, n in unscheduled:
            p = 1 / (1 + exp(-(rating[t] + shift[t])))
            k = sum(1 for _ in range(n) if rand() < p)
            w[t] += k; l[t] += n - k
        pts = {d: 0 for d in drafters}
        for d, t, s in sides:
            pts[d] += w[t] if s == "W" else l[t]
        order = sorted(drafters, key=lambda d: -pts[d])
        best = pts[order[0]]
        tied = [d for d in drafters if pts[d] == best]
        for d in tied:
            first[d] += 1 / len(tied)
        # Finishing place with ties sharing the average of the places they span.
        i = 0
        while i < len(order):
            j = i
            while j + 1 < len(order) and pts[order[j + 1]] == pts[order[i]]:
                j += 1
            for d in order[i:j + 1]:
                for k in range(i, j + 1):
                    place[d][k] += 1 / (j - i + 1)
            i = j + 1
        for d in drafters:
            totals[d].append(pts[d])

    def q(v, p):
        return v[min(len(v) - 1, int(p * len(v)))]

    out = {"updated": datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
           "date": today.isoformat(), "sims": SIMS, "linesUsed": used,
           "gamesLeft": len(remaining) + sum(n for _, n in unscheduled) // 2, "drafters": {}}
    # Distribution curve per drafter: share of simulated seasons in each 4-point bin.
    lo = min(min(v) for v in totals.values()) // 4 * 4
    hi = max(max(v) for v in totals.values()) // 4 * 4 + 4
    out["bins"] = {"start": lo, "width": 4, "count": (hi - lo) // 4}
    for d in drafters:
        v = sorted(totals[d])
        hist = [0] * out["bins"]["count"]
        for x in v:
            hist[(x - lo) // 4] += 1
        out["drafters"][d] = {
            "curve": [round(c / SIMS, 4) for c in hist],
            "mean": round(sum(v) / len(v), 1), "p10": q(v, 0.1), "p50": q(v, 0.5), "p90": q(v, 0.9),
            "first": round(first[d] / SIMS, 4),
            "place": [round(x / SIMS, 4) for x in place[d]],
        }
    with open(OUT, "w") as f:
        json.dump(out, f, separators=(",", ":"))

    try:
        hist = json.load(open(HIST))
    except FileNotFoundError:
        hist = []
    snap = {"date": today.isoformat(), **{d: [out["drafters"][d]["first"], out["drafters"][d]["mean"]] for d in drafters}}
    hist = [h for h in hist if h["date"] != snap["date"]] + [snap]
    with open(HIST, "w") as f:
        json.dump(hist, f, separators=(",", ":"))
    print(f"lines used {used}; games left {out['gamesLeft']}; drift sd {drift_sd:.3f}")
    for d in sorted(drafters, key=lambda d: -out["drafters"][d]["first"]):
        r = out["drafters"][d]
        print(f"{d:8s} mean {r['mean']:6.1f}  80% {r['p10']}-{r['p90']}  first {r['first']:.1%}")


if __name__ == "__main__":
    main()
