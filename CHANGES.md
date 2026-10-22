# Fix: mid-day runs and committee JSON (22 Oct 2026, afternoon)

- bot/tasks.py: before 16:30 WIB the end-of-day job drops today's bar. Yahoo shows a bar for today while the market
  is open; the 10:51 WIB manual run treated that mid-day price as the close, marked 22 Oct as done (so the 11:35 run
  and tonight's 17:30 run would send nothing) and built 23 Oct's orders from mid-day prices.
- docs/data/portfolio.json and docs/data/sentiment_log.csv: put back to their state before that run
  (last processed session 20 Oct), so tonight's run processes 22 Oct from the real close and sends the note.
- bot/committee.py: the reply comes back through a forced tool call (parsed JSON from the API), so a stray quote in
  the model's text no longer fails the Sonnet call and drops to Haiku.
- .github/workflows/end-of-day.yml: manual runs get a "Resend the last note" tick box (passes --force).

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
