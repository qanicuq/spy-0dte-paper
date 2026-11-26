"""Paper trades Version B: buy the nearest-to-the-money SPY 0DTE call at 10:00 AM ET,
sell all at +25% or stop out at -80%, otherwise sell at 3:55 PM. Paper money only.

Prices are live quotes from the user's tastytrade account (read-only sign-in: it can never trade).
Fills: buy at the ask; target is a limit sell that fills when the bid reaches it;
stop and 3:55 exits sell at the bid. Whole contracts only.
"""
import csv, json, math, os, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")
TT_API = "https://api.tastyworks.com"
START_BALANCE, STAKE, FEE = 2000.0, 500.0, 0.02   # FEE is per contract, each side
TARGET, STOP = 1.25, 0.20
MAX_FAILS = 10                                     # quote failures in a row (about 5 minutes) before giving up
EARLY_CLOSE = {"2026-11-27", "2026-12-24"}         # exit at 12:55 PM on these days
TRADES, STATE = "trades.csv", "open_trade.json"
FIELDS = ["date", "strike", "contracts", "entry_time", "entry_ask", "entry_bid",
          "exit_time", "exit_price", "exit_reason", "result", "balance"]
_token, _token_at = None, 0.0


def notify(title, msg):
    topic = os.environ.get("NTFY_TOPIC")
    if not topic:
        print("(no NTFY_TOPIC set, alert not sent)"); return
    req = urllib.request.Request("https://ntfy.sh/" + topic, data=msg.encode(), headers={"Title": title})
    urllib.request.urlopen(req, timeout=20)


def quote(params):
    """(bid, ask, last) for one symbol from tastytrade, or None if tastytrade has no quote for it."""
    global _token, _token_at
    head = {"User-Agent": "spy-0dte-paper/1.0", "Content-Type": "application/json"}
    if _token is None or time.time() - _token_at > 600:   # access tokens last 15 minutes; renew after 10
        body = json.dumps({"grant_type": "refresh_token", "refresh_token": os.environ["TT_REFRESH_TOKEN"],
                           "client_id": os.environ["TT_CLIENT_ID"], "client_secret": os.environ["TT_CLIENT_SECRET"],
                           "scope": "read"}).encode()   # read only: can never trade
        req = urllib.request.Request(TT_API + "/oauth/token", data=body, headers=head)
        _token, _token_at = json.load(urllib.request.urlopen(req, timeout=20))["access_token"], time.time()
    req = urllib.request.Request(TT_API + "/market-data/by-type?" + urllib.parse.urlencode(params),
                                 headers={**head, "Authorization": "Bearer " + _token})
    try:
        items = json.load(urllib.request.urlopen(req, timeout=20)).get("data", {}).get("items", [])
    except urllib.error.HTTPError as e:
        if e.code in (400, 404):   # unknown symbol, e.g. no option expiring today
            return None
        raise
    if not items:
        return None
    r = items[0]
    return float(r.get("bid") or 0), float(r.get("ask") or 0), float(r.get("last") or 0)


def snapshot(strikes=None):
    """Return (as_of ET datetime, SPY price, {strike: (bid, ask)}) for SPY calls expiring today.
    strikes=None looks at the 7 whole-dollar strikes nearest SPY. as_of is None when no
    SPY option expiring today has a quote (market closed)."""
    s = quote({"equity": "SPY"})
    if s is None:
        raise RuntimeError("tastytrade sent no SPY quote")
    bid, ask, last = s
    spy = (bid + ask) / 2 if bid > 0 and ask > 0 else last
    if strikes is None:
        strikes = range(round(spy) - 3, round(spy) + 4)
    day = datetime.now(ET).strftime("%y%m%d")
    calls = {}
    for k in strikes:
        q = quote({"equity-option": f"SPY   {day}C{round(float(k) * 1000):08d}"})   # OCC symbol
        if q:
            calls[float(k)] = q[:2]
    if not calls:
        return None, spy, {}
    return datetime.now(ET), spy, calls


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

    fails = 0
    while datetime.now(ET) < deadline:
        try:
            as_of, spy, calls = snapshot([trade["strike"]] if trade else None)
            fails = 0
        except Exception as e:
            fails += 1
            print(f"fetch failed ({fails} in a row):", e)
            if fails >= MAX_FAILS:
                notify("SPY 0DTE bot: problem",
                       f"{today}: tastytrade quotes failed {fails} times in a row ({e}). "
                       + ("A paper trade is still open. " if trade else "") + "Stopped; check the run.")
                sys.exit(1)
            time.sleep(30); continue
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
