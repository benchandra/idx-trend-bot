# AI committee + short evening note (22 Oct 2026)

Built from the GitHub copy of the repo. Four files, same paths:
- bot/committee.py (new): Claude reads the day's brief, returns market line / reason + agree per order / holding notes /
  watch line. Never changes an order. Writes docs/data/committee.json and appends docs/data/committee_log.csv.
- bot/tasks.py: compact Telegram note (action table with limit and price-limit floor/cap, one reason per order, stops,
  cash after fills, watch line); news, Kronos and stress-gauge detail stay on the dashboard. Calls the committee;
  falls back to rulebook reasons on any error or without a key.
- docs/index.html: "Tonight's note" block on the Today tab.
- README.md: setup section.

Needs the repository secret ANTHROPIC_API_KEY (Settings -> Secrets and variables -> Actions). status.json currently
says sentiment_method "keywords", which means the key is not set yet. Without it the note still goes out, with the
rulebook's own reasons.
