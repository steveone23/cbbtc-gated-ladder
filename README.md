# cbbtc-gated-ladder

A BTC trading bot for the make.fun bounty "Race a BTC trading bot live for 24 hours"
(competition `btc-race-24h-20261113-2314`, protocol `agent-fun/bars-replay/v1`).
Python 3.13, standard library only, deterministic (no network, clock or randomness).

## Strategy

- Trend-gated two-step entry: when flat and the close is at least 0.05% above its
  4-hour simple moving average, buy half of equity; if price then dips 0.6% below
  the first close, add the other half.
- Profit target: once marked equity is up by the target net of an estimated exit
  cost, sell everything. The target is 3 USDC, decaying linearly to 1 USDC
  between bar 600 and bar 1300. Once flat with equity at least 0.6 USDC above the
  start, the bot opens no new positions for the rest of the window.
- Stop: the ladder is abandoned if price falls 2% below its first close.
- Washout bounce: a rare second entry after a 4% drop from the 4-hour high,
  bought only on a modest bounce off the 30-minute low, with a 2% take profit
  and a 1.5% stop.
- Any open position is unwound before the window ends.

## Execution

Fills pay price impact a/D against the pool depth, so large orders are sliced:
ladder buys go out in 5 slices, washout buys in 2, and exits in 3, one slice per
bar. Rejected buy slices are added back to the amount still to buy.

## Seed data

`data/seed.json` holds public Binance BTCUSDT 1-minute closes, scaled by a fixed
basis to the cbBTC/USDC pool level. It only pre-warms the moving average and the
rolling high. The bot uses only seed bars strictly older than the first live bar,
so nothing from the future of any window is used. Rebuild it with:

    python tools/make_seed.py

## License

MIT, see `LICENSE`.
