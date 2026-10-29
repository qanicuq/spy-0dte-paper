# SPY 0DTE paper trader (Version B)

Paper money only. Never touches a real account.

**The plan, every weekday:**
1. At 10:00 AM ET, buy the SPY call closest to the current price that expires today. Spend up to $500 (whole contracts, never more than the paper balance). Start balance: $2,000.
2. Sell everything when the price reaches **+25%**, or if it falls to **-80%**.
3. If neither happens, sell at **3:55 PM** (12:55 PM on early-close days).

**Prices are real quotes** from Cboe's free delayed feed (about 15 minutes behind), checked every 30 seconds:
- Buys fill at the ask.
- The +25% target fills when the bid reaches it.
- Stops and 3:55 exits sell at the bid.
- $0.02 per contract fee on each buy and sell.

If the bot starts too late to get a 10:00 price, it skips the day instead of guessing.

**Where to look:** `trades.csv` has every trade and the running balance. Phone alerts come by ntfy for every buy, sell, skip and failure.

**Backtest to beat** (125 days, Apr 27 – Oct 22, 2026, model prices): 81.6% wins against a 76.2% break-even win rate. Real fills will be worse than the model, so watch the win rate against 76%.

**Running it:** cron-job.org starts the `SPY 0DTE paper trade` workflow at 10:10 AM ET on weekdays. A GitHub schedule at 11:30 AM EDT / 10:30 AM EST alerts if no trade was started on a market day. Run the workflow by hand with mode `test` to check the data feed and alerts.
