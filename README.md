# W/L Pickem 2026-27

League tracker for W/L Pickem: standings, per-drafter ledgers, daily game stakes with odds-based simulations, and streaks. The animated rules explainer lives at `how-it-works.html`.

- `data/picks.json`: the final draft (edit here for any traded picks).
- `data/games.json`: games, scores and pregame moneylines, written by `scripts/update.py` from ESPN's public scoreboard.
- `.github/workflows/update.yml`: refreshes the data hourly and deploys to GitHub Pages.
