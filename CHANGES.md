# Fix: late timers and the end-of-day date lag (5 Nov 2026)

- GitHub's timers started the intraday check once or twice a day, 5-7 hours late (4 Nov: one run, 15:46 WIB), and
  the end-of-day job between 22:00 and 04:30 WIB. README.md: setup for a free outside timer (cron-job.org) that
  starts the runs through GitHub's "Run workflow" API. No timer in the workflow files changed.
- .github/workflows/seed-account.yml and kronos-check.yml: typed-in inputs now reach the script through env
  variables instead of being pasted into the shell command, so whoever can start a run can't make it run commands.
- All five workflows that save data now check out the latest main, not the commit from when the run was queued.
  On 5 Nov run #19 was queued at 07:16:12 UTC, #18 saved at 07:16:13, and #19's save then failed (exit 128).
- bot/tasks.py: every end-of-day run after midnight WIB got Yahoo data without the last session and processed the
  session before (4 Nov runs used 3 Nov). The job now skips without changing or sending anything when the newest
  finished session is missing from the prices, so a late run cannot process the wrong day.
- The failed Pages run on 5 Nov 02:29 WIB was a GitHub-side deploy timeout ("Timeout reached, aborting!"); the build
  itself passed. No change needed; the next push redeploys the dashboard.

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
