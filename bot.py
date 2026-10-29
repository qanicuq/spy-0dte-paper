"""Paper trades Version B: buy the nearest-to-the-money SPY 0DTE call at 10:00 AM ET,
sell all at +25% or stop out at -80%, otherwise sell at 3:55 PM. Paper money only.

Prices come from Cboe's free delayed quotes (every SPY option's bid and ask).
Fills: buy at the ask; target is a limit sell that fills when the bid reaches it;
stop and 3:55 exits sell at the bid. Whole contracts only.
"""
import csv, json, math, os, sys, time, urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
URL = "https://cdn.cboe.com/api/global/delayed_quotes/options/SPY.json"
START_BALANCE, STAKE, FEE = 2000.0, 500.0, 0.02   # FEE is per contract, each side
TARGET, STOP = 1.25, 0.20
EARLY_CLOSE = {"2026-11-27", "2026-12-24"}         # exit at 12:55 PM on these days
TRADES, STATE = "trades.csv", "open_trade.json"
FIELDS = ["date", "strike", "contracts", "entry_time", "entry_ask", "entry_bid",
          "exit_time", "exit_price", "exit_reason", "result", "balance"]


def notify(title, msg):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("(no NTFY_TOPIC set, alert not sent)"); return
    req = urllib.request.Request("https://ntfy.sh/" + topic, data=msg.encode(), headers={"Title": title})
    urllib.request.urlopen(req, timeout=20)


def snapshot():
    """Return (as_of ET datetime, underlying price, {strike: (bid, ask)}) for today's calls."""
    req = urllib.request.Request(URL, headers={"User-Agent": "Mozilla/5.0"})
    data = json.load(urllib.request.urlopen(req, timeout=60))["data"]
    today = datetime.now(ET).strftime("%y%m%d")
    calls, puts, last = {}, {}, None
    for o in data["options"]:
        sym = o["option"]
        if sym[3:9] != today:
            continue
        k = int(sym[10:]) / 1000
        quote = (float(o["bid"] or 0), float(o["ask"] or 0))
        (calls if sym[9] == "C" else puts)[k] = quote
        t = o.get("last_trade_time")
        if t and t[:10] == datetime.now(ET).strftime("%Y-%m-%d"):
            last = max(last, t) if last else t
    if not calls or last is None:
        return None, None, {}
    as_of = datetime.fromisoformat(last).replace(tzinfo=ET)   # newest trade = how current the data is
    # SPY price from put-call parity at the strike where call and put are closest
    both = [k for k in calls if k in puts and min(calls[k] + puts[k]) > 0]
    mid = lambda q: (q[0] + q[1]) / 2
    k0 = min(both, key=lambda k: abs(mid(calls[k]) - mid(puts[k])))
    spy = k0 + mid(calls[k0]) - mid(puts[k0])
    return as_of, spy, calls


def load_trades():
    if not os.path.exists(TRADES):
        return []
    return list(csv.DictReader(open(TRADES)))


def save_trade(row):
    rows = load_trades() + [row]
    with open(TRADES, "w", newline="") as f:
        w = csv.DictWriter(f, FIELDS); w.writeheader(); w.writerows(rows)


def summary(rows):
    done = [r for r in rows if r["exit_reason"] not in ("skipped",)]
    res = [float(r["result"]) for r in done]
    wins = sum(x > 0 for x in res)
    rate = wins / len(res) * 100 if res else 0
    return f"{wins}W-{len(res) - wins}L ({rate:.0f}% wins; backtest 82%, break-even 76%) | total ${sum(res):+.2f}"


def main(test=False, check=False):
    today = datetime.now(ET).strftime("%Y-%m-%d")
    rows = load_trades()
    if any(r["date"] == today for r in rows):
        print("Today's trade is already done."); return
    balance = float(rows[-1]["balance"]) if rows else START_BALANCE
    exit_at = "12:55" if today in EARLY_CLOSE else "15:55"
    trade = json.load(open(STATE)) if os.path.exists(STATE) else None
    if trade and trade["date"] != today:
        trade = None   # a stale leftover from a crashed day; 0DTE options are gone
    deadline = datetime.now(ET).replace(hour=16, minute=20, second=0)
    started = datetime.now(ET)

    if check:   # missed-day check: market open today but no trade started
        if trade is None and snapshot()[0] is not None:
            notify("SPY 0DTE bot: missed", f"{today}: no paper trade was started today. Check cron-job.org.")
        return

    if test:
        as_of, spy, calls = snapshot()
        if as_of is None:
            msg = "Feed works, but no SPY options expiring today (market closed or after hours)."
        else:
            k = min(calls, key=lambda s: abs(s - spy))
            msg = f"Feed works. Data as of {as_of:%H:%M} ET, SPY ~{spy:.2f}, {k:g} call bid {calls[k][0]} / ask {calls[k][1]}."
        print(msg); open("test_result.txt", "w").write(msg + "\n"); notify("SPY 0DTE bot: test", msg); return

    if started >= deadline:   # started after the trading day (e.g. a manual test run)
        print("Trading day is over; nothing to do."); return

    while datetime.now(ET) < deadline:
        try:
            as_of, spy, calls = snapshot()
        except Exception as e:
            print("fetch failed:", e); time.sleep(30); continue
        now = datetime.now(ET)
        if as_of is None:
            if now.hour * 60 + now.minute > 10 * 60 + 45:
                print("No SPY options trading today; market closed."); return
            time.sleep(30); continue
        hm = as_of.strftime("%H:%M")

        if trade is None:
            if hm < "10:00":
                time.sleep(30); continue
            if hm > "10:05":   # started too late to get a 10:00 entry; don't fake one
                row = dict(date=today, strike="", contracts=0, entry_time="", entry_ask="", entry_bid="",
                           exit_time="", exit_price="", exit_reason="skipped",
                           result=0, balance=round(balance, 2))
                save_trade(row)
                notify("SPY 0DTE bot: skipped", f"{today}: bot started late (data already at {hm} ET), no trade.")
                return
            k = min(calls, key=lambda s: abs(s - spy))
            bid, ask = calls[k]
            n = math.floor(min(STAKE, balance) / (ask * 100)) if ask > 0 else 0
            if n == 0:
                notify("SPY 0DTE bot: skipped", f"{today}: not enough balance (${balance:.2f}) for one contract.")
                save_trade(dict(date=today, strike=k, contracts=0, entry_time=hm, entry_ask=ask, entry_bid=bid,
                                exit_time="", exit_price="", exit_reason="skipped", result=0, balance=round(balance, 2)))
                return
            trade = dict(date=today, strike=k, contracts=n, entry_time=hm, entry_ask=ask, entry_bid=bid,
                         target=round(ask * TARGET, 2), stop=round(ask * STOP, 2))
            json.dump(trade, open(STATE, "w"))
            os.system("git add open_trade.json && git commit -qm 'Open paper trade' && git push -q")
            notify("SPY 0DTE bot: BUY",
                   f"{n} x SPY {k:g} call at ${ask:.2f} (bid {bid:.2f}), {hm} ET. Target ${trade['target']:.2f}, stop ${trade['stop']:.2f}.")
            continue

        bid, ask = calls.get(trade["strike"], (0, 0))
        if bid == 0 and ask == 0:   # missing quote, not a real price
            time.sleep(30); continue
        if (now - started).total_seconds() > 280 * 60:   # GitHub stops jobs at 6 hours: hand off to a fresh run
            os.system("gh workflow run paper.yml")
            print("Handed off to a new run; trade still open."); return
        reason = None
        if bid <= trade["stop"]:
            reason, price = "stop", bid
        elif bid >= trade["target"]:
            reason, price = "target", trade["target"]
        elif hm >= exit_at:
            reason, price = "3:55 exit" if exit_at == "15:55" else "early-close exit", bid
        if reason:
            n = trade["contracts"]
            result = round(n * 100 * (price - trade["entry_ask"]) - 2 * n * FEE, 2)
            balance += result
            save_trade(dict(date=today, strike=trade["strike"], contracts=n, entry_time=trade["entry_time"],
                            entry_ask=trade["entry_ask"], entry_bid=trade["entry_bid"], exit_time=hm,
                            exit_price=price, exit_reason=reason, result=result, balance=round(balance, 2)))
            os.remove(STATE)
            notify(f"SPY 0DTE bot: SELL ({reason})",
                   f"Sold at ${price:.2f}, {hm} ET. Result ${result:+.2f}. Balance ${balance:.2f}.\n{summary(load_trades())}")
            return
        time.sleep(30)

    notify("SPY 0DTE bot: problem", f"{today}: ran out of time without finishing (started {started:%H:%M} ET). Check the run.")
    sys.exit(1)


if __name__ == "__main__":
    main(test="--test" in sys.argv, check="--check" in sys.argv)
