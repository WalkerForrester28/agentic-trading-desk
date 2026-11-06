---
name: report1
description: Show the full daily paper-trading report. Use whenever the user types "report1" (alone or in a sentence), "/report1", or asks for the daily paper desk report.
---

# report1 — daily paper desk report

Paper trading only. Never call a Robinhood or broker tool here.

1. Get the latest ledger (the scheduled daily run pushes from another session):

   ```bash
   git fetch origin claude/festive-edison-yfwaw2 && git merge --ff-only origin/claude/festive-edison-yfwaw2
   ```

   If the fast-forward fails because of local changes, say so and report from the local files.

2. Run:

   ```bash
   python3 paper/paper_trade.py report
   ```

3. Reply with the report output in full, as markdown, keeping every section in order:
   account right now, performance, trade history, and the latest session log
   (macro, trades, not-executed signals, scorecards, book). Do not summarize it away.

4. After the report, add 2–4 plain-English lines: what changed since the previous
   session, why the framework did or did not trade, and anything that stands out
   (a position near an exit signal, a ticker close to a rebound trigger, a data problem).
   If the latest log is not from today's date (ET) on a weekday after 2:35 pm ET, say the
   daily run has not landed yet or failed.
