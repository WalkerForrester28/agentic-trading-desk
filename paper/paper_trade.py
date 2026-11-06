#!/usr/bin/env python3
"""
paper_trade.py
==============
Paper-trading desk: runs the same deterministic framework as the live skill
(macro_pillar.py -> score.py) on public market data, applies the Mandate's
sizing and risk limits, simulates the fills, and logs everything.

No broker, no real orders. State lives in files under paper/ that are committed
to git (the cloud container is ephemeral):

  paper/ledger.json     cash, open positions, last equity (the account)
  paper/trades.csv      one row per simulated fill (append-only)
  paper/equity.csv      one row per run: equity, cash, invested, P&L
  paper/logs/*.md       full session report per run (scorecards + decisions)

Commands
  init   [--cash 10000]           start a fresh paper account (refuses to overwrite)
  run    [--dry-run] [--regular-hours-only] [--symbols A,B]
  status                          mark positions to market and print the book

Fill model
  Regular hours (Mon-Fri 09:30-15:58 ET): live price, slippage --slippage-bps
  against you. Outside regular hours: last official close, labelled as such
  ("EOD fill"). --regular-hours-only reproduces the live Mandate exactly
  (analysis only, no fills, when the market is closed).

Rules applied (from SKILL.md, Autonomous Execution Mandate)
  - Entries only on score.py decisions RE-ENTRY (new cycle) and
    TACTICAL REBOUND (counter-trend, half size). RE-ENTRY with macro <= -1 is
    also half size ("entry in reduced size").
  - Exits on EXIT and EXIT / TRIM (full position: capital rotation).
  - Base size 3% of equity, capped at $1,200 per buy. Max 3 new positions per
    run. 15% of equity always kept in cash. No adding to an open position.
  - Circuit breaker: equity down > 4% vs the previous run -> no buys.
  - Data check: a symbol whose last bar is > 4 days old is skipped, not traded.
  - Zero discretion: only framework decisions trade (no news, no judgment calls).
  - Not modelled: earnings blackout (no earnings calendar source), dividends.
stdlib only.
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import sys
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "..", "scripts"))
sys.path.insert(0, HERE)

import market_data as MD  # noqa: E402
import macro_pillar as MP  # noqa: E402
import score as S  # noqa: E402

LEDGER = os.path.join(HERE, "ledger.json")
TRADES = os.path.join(HERE, "trades.csv")
EQUITY = os.path.join(HERE, "equity.csv")
LOGDIR = os.path.join(HERE, "logs")
WATCHLIST = os.path.join(HERE, "watchlist.txt")

MACRO_SYMBOLS = ["SPY", "RSP", "IWM", "HYG", "LQD", "TLT", "XLY", "XLP"]
BASE_PCT = 0.03
BUY_CAP = 1200.0
MAX_NEW = 3
CASH_RESERVE = 0.15
BREAKER = -0.04
MIN_ORDER = 10.0
STALE_DAYS = 4

ENTRY_FULL = "RE-ENTRY (new cycle)"
ENTRY_HALF = "TACTICAL REBOUND (counter-trend)"
EXITS = ("EXIT", "EXIT / TRIM")

TRADE_FIELDS = ["timestamp_utc", "session_et", "symbol", "side", "qty", "price",
                "notional", "decision", "fill_basis", "realized_pnl", "cash_after"]
EQUITY_FIELDS = ["timestamp_utc", "session_et", "equity", "cash", "invested",
                 "positions", "total_return_pct", "realized_pnl_total"]


# --------------------------------------------------------------------------
# Ledger I/O
# --------------------------------------------------------------------------

def load_ledger() -> dict:
    if not os.path.exists(LEDGER):
        sys.exit("No paper account yet. Run: python3 paper/paper_trade.py init --cash 10000")
    with open(LEDGER) as f:
        return json.load(f)


def save_ledger(led: dict) -> None:
    tmp = LEDGER + ".tmp"
    with open(tmp, "w") as f:
        json.dump(led, f, indent=2, sort_keys=True)
        f.write("\n")
    os.replace(tmp, LEDGER)


def append_csv(path: str, fields: list[str], row: dict) -> None:
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        if new:
            w.writeheader()
        w.writerow(row)


def read_watchlist() -> list[str]:
    with open(WATCHLIST) as f:
        return [l.strip().upper() for l in f if l.strip() and not l.startswith("#")]


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def equity_of(led: dict, prices: dict) -> tuple[float, float]:
    invested = sum(p["qty"] * prices.get(s, p["last_price"]) for s, p in led["positions"].items())
    return led["cash"] + invested, invested


def stale(last_date: str, today: str) -> bool:
    return (date.fromisoformat(today) - date.fromisoformat(last_date)).days > STALE_DAYS


def compute_macro(bars: dict, spread: list[float] | None) -> MP.MacroResult:
    # Align every macro series on common dates so the ratios compare like with like.
    common = None
    for s in MACRO_SYMBOLS:
        if s in bars:
            d = set(bars[s]["dates"])
            common = d if common is None else common & d
    common = sorted(common or [])
    series = {}
    for s in MACRO_SYMBOLS:
        if s in bars:
            m = dict(zip(bars[s]["dates"], bars[s]["close"]))
            series[s] = [m[d] for d in common]
    data = {"as_of": common[-1] if common else None, "series": series}
    if spread:
        data["yield_spread"] = spread
    return MP.score_macro(data)


# --------------------------------------------------------------------------
# Commands
# --------------------------------------------------------------------------

def cmd_init(args) -> int:
    if os.path.exists(LEDGER) and not args.force:
        print("A paper account already exists (paper/ledger.json). Use --force to wipe it.",
              file=sys.stderr)
        return 1
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    save_ledger({"created_utc": now, "starting_cash": args.cash, "cash": args.cash,
                 "positions": {}, "last_equity": args.cash, "realized_pnl": 0.0,
                 "last_run_utc": None})
    for p in (TRADES, EQUITY):
        if args.force and os.path.exists(p):
            os.remove(p)
    print(f"Paper account opened with ${args.cash:,.2f}.")
    return 0


def cmd_status(args) -> int:
    led = load_ledger()
    prices = {}
    for s in led["positions"]:
        try:
            prices[s] = MD.daily_bars(s, "5d")["last_price"]
        except MD.DataError as e:
            print(f"warning: {e}; using last logged price", file=sys.stderr)
    print(render_book(led, prices))
    return 0


def render_book(led: dict, prices: dict) -> str:
    eq, inv = equity_of(led, prices)
    ret = (eq / led["starting_cash"] - 1) * 100
    L = [f"Equity ${eq:,.2f}  ·  cash ${led['cash']:,.2f}  ·  invested ${inv:,.2f}  ·  "
         f"return {ret:+.2f}% since {led['created_utc'][:10]}  ·  realized ${led['realized_pnl']:+,.2f}"]
    if led["positions"]:
        L.append("")
        L.append("| symbol | qty | avg cost | price | value | unrealized | opened | entry |")
        L.append("|---|---:|---:|---:|---:|---:|---|---|")
        for s, p in sorted(led["positions"].items()):
            px = prices.get(s, p["last_price"])
            val = p["qty"] * px
            upl = val - p["qty"] * p["avg_cost"]
            L.append(f"| {s} | {p['qty']:.4f} | {p['avg_cost']:.2f} | {px:.2f} | {val:,.2f} | "
                     f"{upl:+,.2f} ({(px / p['avg_cost'] - 1) * 100:+.1f}%) | {p['opened']} | {p['entry_decision']} |")
    else:
        L.append("No open positions.")
    return "\n".join(L)


def cmd_run(args) -> int:
    led = load_ledger()
    sess = MD.market_session()
    now_utc = datetime.now(timezone.utc).isoformat(timespec="seconds")
    watch = [s.strip().upper() for s in args.symbols.split(",")] if args.symbols else read_watchlist()
    # Open positions are always evaluated, even if dropped from the watchlist.
    universe = list(dict.fromkeys(watch + sorted(led["positions"])))

    notes: list[str] = []
    bars, errors = {}, {}
    for s in dict.fromkeys(universe + MACRO_SYMBOLS):
        try:
            bars[s] = MD.daily_bars(s)
        except MD.DataError as e:
            errors[s] = str(e)
    try:
        spread = MD.yield_spread()
    except MD.DataError as e:
        spread = None
        notes.append(f"10Y-2Y spread unavailable ({e}); macro weight redistributed.")

    macro = compute_macro(bars, spread)
    macro_score = macro.pillar_score

    live = sess["regular_hours"]
    can_trade = live or not args.regular_hours_only
    fill_basis = "live price" if live else "last close (market closed, EOD fill)"
    if not can_trade:
        notes.append("Outside regular hours and --regular-hours-only set: analysis only, no fills.")
    if args.dry_run:
        notes.append("Dry run: decisions shown, ledger not changed.")

    # ---- Score everything ----
    cards, prices = {}, {}
    for s in universe:
        if s in errors:
            continue
        b = bars[s]
        if stale(b["dates"][-1], sess["date"]):
            errors[s] = f"stale data: last bar {b['dates'][-1]}"
            continue
        prices[s] = b["last_price"] if live else b["close"][-1]
        cards[s] = S.score_symbol(b["close"], macro_score=macro_score, symbol=s,
                                  holding=s in led["positions"])

    eq_open, _ = equity_of(led, prices)
    change = eq_open / led["last_equity"] - 1 if led["last_equity"] else 0.0
    breaker = change < BREAKER
    if breaker:
        notes.append(f"Circuit breaker: equity {change * 100:+.1f}% vs last run (< -4%). No buys.")

    fills: list[dict] = []
    skipped: list[str] = []
    slip = args.slippage_bps / 10000 if live else 0.0

    def fill(sym, side, qty, decision):
        px = prices[sym] * (1 + slip if side == "buy" else 1 - slip)
        notional = round(qty * px, 2)
        realized = 0.0
        if side == "buy":
            led["cash"] -= notional
            led["positions"][sym] = {"qty": qty, "avg_cost": px, "opened": sess["date"],
                                     "entry_decision": decision, "last_price": px}
        else:
            pos = led["positions"].pop(sym)
            realized = round((px - pos["avg_cost"]) * qty, 2)
            led["cash"] += notional
            led["realized_pnl"] = round(led["realized_pnl"] + realized, 2)
        led["cash"] = round(led["cash"], 2)
        row = {"timestamp_utc": now_utc, "session_et": sess["now_et"], "symbol": sym,
               "side": side, "qty": f"{qty:.6f}", "price": f"{px:.4f}", "notional": f"{notional:.2f}",
               "decision": decision, "fill_basis": fill_basis,
               "realized_pnl": f"{realized:.2f}" if side == "sell" else "",
               "cash_after": f"{led['cash']:.2f}"}
        fills.append(row)
        if not args.dry_run:
            append_csv(TRADES, TRADE_FIELDS, row)

    # ---- Exits first (they free cash) ----
    for s in sorted(led["positions"]):
        c = cards.get(s)
        if c is None:
            skipped.append(f"{s}: held but no usable data ({errors.get(s)}) — not evaluated")
            continue
        led["positions"][s]["last_price"] = prices[s]
        act = c["decision"]["action"]
        if act in EXITS:
            if can_trade:
                fill(s, "sell", led["positions"][s]["qty"], act)
            else:
                skipped.append(f"{s}: {act} — carried to next regular session")

    # ---- Entries, strongest scorecard first ----
    eq_now, _ = equity_of(led, prices)
    base = min(BASE_PCT * eq_now, BUY_CAP)
    cands = [(c["pillar_total"], s, c) for s, c in cards.items()
             if s not in led["positions"] and c["decision"]["action"] in (ENTRY_FULL, ENTRY_HALF)]
    new = 0
    for _, s, c in sorted(cands, key=lambda x: (-x[0], x[1])):
        act = c["decision"]["action"]
        half = act == ENTRY_HALF or (macro_score is not None and macro_score <= -1)
        size = base / 2 if half else base
        size = min(size, led["cash"] - CASH_RESERVE * eq_now)
        reason = None
        if not can_trade:
            reason = "market closed (--regular-hours-only)"
        elif breaker:
            reason = "circuit breaker"
        elif new >= MAX_NEW:
            reason = f"max {MAX_NEW} new positions this run"
        elif size < MIN_ORDER:
            reason = "15% cash reserve reached"
        if reason:
            skipped.append(f"{s}: {act} — not entered ({reason})")
            continue
        fill(s, "buy", round(size / (prices[s] * (1 + slip)), 6), act)
        new += 1

    eq_close, inv = equity_of(led, prices)
    if not args.dry_run:
        led["last_equity"] = round(eq_close, 2)
        led["last_run_utc"] = now_utc
        save_ledger(led)
        append_csv(EQUITY, EQUITY_FIELDS, {
            "timestamp_utc": now_utc, "session_et": sess["now_et"],
            "equity": f"{eq_close:.2f}", "cash": f"{led['cash']:.2f}", "invested": f"{inv:.2f}",
            "positions": len(led["positions"]),
            "total_return_pct": f"{(eq_close / led['starting_cash'] - 1) * 100:.3f}",
            "realized_pnl_total": f"{led['realized_pnl']:.2f}"})

    report = render_report(sess, macro, cards, fills, skipped, errors, notes, led, prices,
                           fill_basis, args.dry_run)
    print(report)
    if not args.dry_run:
        os.makedirs(LOGDIR, exist_ok=True)
        name = datetime.now(MD.ET).strftime("%Y-%m-%d_%H%M") + ".md"
        with open(os.path.join(LOGDIR, name), "w") as f:
            f.write(report + "\n")
        print(f"\nLogged to paper/logs/{name}", file=sys.stderr)
    return 0


def render_report(sess, macro, cards, fills, skipped, errors, notes, led, prices,
                  fill_basis, dry) -> str:
    L = [f"# Paper desk session — {sess['now_et']}{'  (DRY RUN)' if dry else ''}", ""]
    L.append(f"Market: {'regular hours' if sess['regular_hours'] else 'closed'} · fills at {fill_basis}")
    L.append("")
    L.append(f"## Macro-Sentiment: {macro.pillar_score:+d} ({macro.pillar_label})")
    L.append(f"Regime: {macro.regime} · composite {macro.composite:+.3f} · as of {macro.as_of}")
    for c in macro.components:
        sig = f"{c['signal']:+.2f}" if c["available"] and c["signal"] is not None else "n/a"
        L.append(f"- {c['ratio']}: {sig} — {c['detail'] or 'no data'}")
    L.append("")
    L.append("## Trades")
    if fills:
        L.append("| side | symbol | qty | price | notional | decision | realized |")
        L.append("|---|---|---:|---:|---:|---|---:|")
        for r in fills:
            L.append(f"| {r['side'].upper()} | {r['symbol']} | {float(r['qty']):.4f} | {float(r['price']):.2f} | "
                     f"{float(r['notional']):,.2f} | {r['decision']} | {r['realized_pnl'] or '—'} |")
    else:
        L.append("No trades this session.")
    if skipped:
        L.append("")
        L.append("Not executed:")
        L += [f"- {x}" for x in skipped]
    L.append("")
    L.append("## Scorecards")
    L.append("| symbol | close | trend | mom | macro | total | decision | flags |")
    L.append("|---|---:|---:|---:|---:|---:|---|---|")
    for s, c in sorted(cards.items(), key=lambda kv: (-kv[1]["pillar_total"], kv[0])):
        p = c["pillars"]; f = c["decision"]["flags"]
        flags = "; ".join(
            [f"exh: {x}" for x in f["exhaustion"]] + [f"bear: {x}" for x in f["bearish"]]
            + [f"reb: {x}" for x in f["rebound"]]) or "—"
        L.append(f"| {s}{' ●' if s in led['positions'] else ''} | {prices[s]:.2f} | {p['trend']['score']:+d} | "
                 f"{p['momentum']['score']:+d} | {p['macro_sentiment']['score']:+d} | {c['pillar_total']:+d} | "
                 f"{c['decision']['action']} | {flags} |")
    L.append("")
    L.append("● = held at time of scoring")
    if errors:
        L.append("")
        L.append("## Data problems (not traded)")
        L += [f"- {s}: {e}" for s, e in errors.items()]
    if notes or macro.notes:
        L.append("")
        L.append("## Notes")
        L += [f"- {n}" for n in notes + list(macro.notes)]
    L.append("")
    L.append("## Book after session")
    L.append(render_book(led, prices))
    return "\n".join(L)


def main() -> int:
    ap = argparse.ArgumentParser(description="Paper-trading desk for the three-pillar framework.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("init"); p.add_argument("--cash", type=float, default=10000.0)
    p.add_argument("--force", action="store_true")
    p = sub.add_parser("run")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--regular-hours-only", action="store_true")
    p.add_argument("--symbols", help="Comma list overriding watchlist.txt")
    p.add_argument("--slippage-bps", type=float, default=5.0)
    sub.add_parser("status")
    args = ap.parse_args()
    return {"init": cmd_init, "run": cmd_run, "status": cmd_status}[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
