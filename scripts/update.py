"""Fetch NBA regular-season games, scores and pregame moneylines from ESPN's public
scoreboard and merge them into data/games.json.

Run with no arguments. Standard library only, so it runs on a bare GitHub Actions runner.
"""
import json
import os
import sys
import time
import urllib.request
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
GAMES_PATH = os.path.join(ROOT, "data", "games.json")
SCOREBOARD = "https://site.api.espn.com/apis/site/v2/sports/basketball/nba/scoreboard?dates={}"
SEASON_START = date(2026, 10, 20)
SEASON_END = date(2027, 4, 11)
LOOKAHEAD_DAYS = 10
RECHECK_DAYS = 2
ET = ZoneInfo("America/New_York")

# ESPN abbreviations that differ from the ones the league uses.
ABBR = {"GS": "GSW", "NO": "NOP", "NY": "NYK", "SA": "SAS", "UTAH": "UTA", "WSH": "WAS"}

# Games that do not count toward standings (the NBA Cup final). Add ESPN event ids here
# if the notes-based check below ever misses one.
EXCLUDE_IDS = set()


def fetch(day):
    url = SCOREBOARD.format(day.strftime("%Y%m%d"))
    req = urllib.request.Request(url, headers={"User-Agent": "wl-pickem-tracker"})
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception as e:  # network hiccup: retry, then give up on this day
            if attempt == 2:
                print(f"fetch failed {day}: {e}", file=sys.stderr)
                return None
            time.sleep(2)


def american_to_prob(odds):
    o = float(str(odds).replace("+", ""))
    return 100 / (o + 100) if o > 0 else -o / (-o + 100)


def parse_moneyline(comp):
    """Return (home_ml, away_ml, provider) from ESPN odds, preferring the current line."""
    for o in comp.get("odds") or []:
        ml = o.get("moneyline") or {}
        lines = []
        for side in ("home", "away"):
            s = ml.get(side) or {}
            v = (s.get("close") or {}).get("odds") or (s.get("open") or {}).get("odds")
            lines.append(v)
        lines = ["+100" if v == "EVEN" else v for v in lines]
        if all(v and v != "OFF" for v in lines):
            return lines[0], lines[1], (o.get("provider") or {}).get("name")
    return None


def is_excluded(event, comp):
    if event["id"] in EXCLUDE_IDS:
        return True
    notes = " ".join(n.get("headline", "") for n in comp.get("notes") or []).lower()
    return "cup" in notes and ("final" in notes or "championship" in notes) and "semi" not in notes and "quarter" not in notes


def parse_day(payload):
    games = {}
    for e in payload.get("events", []):
        if (e.get("season") or {}).get("type") != 2:
            continue
        comp = e["competitions"][0]
        if is_excluded(e, comp):
            continue
        teams = {c["homeAway"]: c for c in comp["competitors"]}
        home, away = teams["home"], teams["away"]
        start = datetime.fromisoformat(e["date"].replace("Z", "+00:00"))
        status = comp["status"]["type"]
        g = {
            "id": e["id"],
            "date": start.astimezone(ET).date().isoformat(),
            "start": start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "home": ABBR.get(home["team"]["abbreviation"], home["team"]["abbreviation"]),
            "away": ABBR.get(away["team"]["abbreviation"], away["team"]["abbreviation"]),
            "state": status.get("state"),  # pre / in / post
            "final": bool(status.get("completed")),
            "detail": status.get("shortDetail"),
        }
        if g["state"] != "pre":
            g["homeScore"] = int(home.get("score") or 0)
            g["awayScore"] = int(away.get("score") or 0)
        if g["final"]:
            g["winner"] = g["home"] if home.get("winner") else g["away"]
        ml = parse_moneyline(comp)
        if ml:
            hp, ap = american_to_prob(ml[0]), american_to_prob(ml[1])
            g["odds"] = {"home": ml[0], "away": ml[1], "book": ml[2],
                         "homeProb": round(hp / (hp + ap), 4)}
        games[g["id"]] = g
    return games


def main():
    try:
        with open(GAMES_PATH) as f:
            store = json.load(f)
    except FileNotFoundError:
        store = {"games": {}}
    games = store["games"]

    today = datetime.now(ET).date()
    last = min(today + timedelta(days=LOOKAHEAD_DAYS), SEASON_END)
    by_date = {}
    for g in games.values():
        by_date.setdefault(g["date"], []).append(g)

    day = SEASON_START
    fetched = 0
    while day <= last:
        known = by_date.get(day.isoformat(), [])
        settled = known and all(g["final"] for g in known) and day < today - timedelta(days=RECHECK_DAYS)
        if not settled:
            payload = fetch(day)
            if payload is not None:
                fetched += 1
                for gid, g in parse_day(payload).items():
                    old = games.get(gid, {})
                    # ESPN drops odds once a game starts: keep the last pregame line we saw.
                    if "odds" not in g and "odds" in old:
                        g["odds"] = old["odds"]
                    games[gid] = g
        day += timedelta(days=1)

    store["games"] = dict(sorted(games.items(), key=lambda kv: (kv[1]["start"], kv[0])))
    store["updated"] = datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")
    os.makedirs(os.path.dirname(GAMES_PATH), exist_ok=True)
    with open(GAMES_PATH, "w") as f:
        json.dump(store, f, separators=(",", ":"))
    finals = sum(g["final"] for g in games.values())
    print(f"fetched {fetched} days; {len(games)} games stored, {finals} final")


if __name__ == "__main__":
    main()
