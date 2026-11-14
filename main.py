"""CBBTC trend-gated two-step ladder with a profit target, plus a rare washout-bounce entry.

Protocol agent-fun/bars-replay/v1 via vendor/makefun_bars.py. Standard library only, deterministic.
"""

import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "vendor"))
import makefun_bars  # noqa: E402

SYMBOL = "CBBTC"
WINDOW_BARS = 1440                   # batches in the window, seq 0..1439
START_UNITS = 1_000_000_000          # 1,000 USDC
MIN_NOTIONAL = 1_000_000             # 1 USDC
SMA_BARS = 240                       # trend gate: 4-hour simple moving average of closes
GATE_BAND = 0.0005                   # ladder entry needs close >= SMA * (1 + GATE_BAND)
RUNG_FRACTIONS = (0.5, 0.5)          # ladder rungs as fractions of equity at ladder start
RUNG_DROPS = (0.0, 0.006)            # rung i is added when close <= first_close * (1 - RUNG_DROPS[i])
TARGET_UNITS = 3_000_000             # bank the ladder once marked equity gain >= 3 USDC net of estimated exit cost
TARGET_MIN_UNITS = 1_000_000         # late-window ladder target: 1 USDC
DECAY_START_BAR = 600                # the ladder target starts to decay after this seq
DECAY_END_BAR = 1300                 # ... and reaches TARGET_MIN_UNITS at this seq
EXIT_COST_BPS = 9                    # estimated cost of selling the position, in bps of its value
LADDER_STOP = 0.02                   # give up the ladder when close <= first_close * (1 - LADDER_STOP)
LADDER_LAST_BAR = 1200               # no new ladder after this seq
DONE_UNITS = 600_000                 # when flat with equity >= start + 0.6 USDC: no new entries for the rest of the window
TWAP_IN = 5                          # ladder buys are cut into this many slices, one per bar
TWAP_OUT = 3                         # exits are cut into this many slices, one per bar
WASH_HIGH_BARS = 240                 # washout: rolling high window
WASH_LOW_BARS = 30                   # washout: rolling low window
WASH_DIP = 0.04                      # washout needs low <= high * (1 - WASH_DIP)
WASH_BOUNCE = 0.0025                 # ... and close >= low * (1 + WASH_BOUNCE)
WASH_CHASE = 0.0095                  # ... and close <= low * (1 + WASH_CHASE)
WASH_TP = 0.02                       # washout take profit vs first fill price
WASH_SL = 0.015                      # washout stop loss vs first fill price
WASH_TWAP_IN = 2                     # washout buys are cut into this many slices
WASH_MAX_ENTRIES = 1                 # at most this many washout entries per window
WASH_COOLDOWN = 30                   # bars after the last exit before a washout entry
WASH_LAST_BAR = 1320                 # no new washout after this seq
PROBE_BAR = 1380                     # if nothing has ever filled by this seq, buy 1 USDC so the entry has a trade
BUY_SLIPPAGE_BPS = 150               # max slippage on buys
SELL_SLIPPAGE_BPS = 300              # max slippage on sells
HISTORY_KEEP = 600                   # closes kept in memory

FLAT, LADDER, WASH, EXIT = "FLAT", "LADDER", "WASH", "EXIT"


def load_seed():
    """Seed (t, c) lists from data/seed.json; empty when missing or malformed."""
    try:
        with open(os.path.join(HERE, "data", "seed.json"), encoding="utf-8") as f:
            raw = json.load(f)
        ts = [int(x) for x in raw["t"]]
        cs = [int(x) for x in raw["c"]]
        if len(ts) != len(cs):
            return [], []
        return ts, cs
    except (OSError, ValueError, KeyError, TypeError):
        return [], []


SEED_T, SEED_C = load_seed()


def ladder_target(k):
    """Ladder profit target on scored bar k: TARGET_UNITS, decaying linearly to TARGET_MIN_UNITS."""
    if k <= DECAY_START_BAR:
        return TARGET_UNITS
    if k >= DECAY_END_BAR:
        return TARGET_MIN_UNITS
    return TARGET_UNITS + (TARGET_MIN_UNITS - TARGET_UNITS) * (k - DECAY_START_BAR) / (DECAY_END_BAR - DECAY_START_BAR)


class Bot:
    def __init__(self):
        self.closes = None
        self.mode = FLAT
        self.rung = 0
        self.first_close = 0
        self.base_equity = 0
        self.buy_left = 0
        self.slice_units = 0
        self.sell_slices = 0
        self.ladder_done = False
        self.wash_entries = 0
        self.last_exit_seq = -100000
        self.wash_entry_price = None
        self.traded = False
        self.pending = {}

    def on_batch(self, state, batch):
        # bar count within the scored window, after any warm-up
        k = batch["seq"] - state.warmup_bars

        # 1. the CBBTC bar
        bar = None
        for b in batch["bars"]:
            if b["symbol"] == SYMBOL:
                bar = b

        # 2. fills of the previous batch's orders
        for f in batch["fills"]:
            if f["status"] == "filled":
                self.traded = True
                if f["side"] == "buy" and self.mode == WASH and self.wash_entry_price is None:
                    self.wash_entry_price = int(f["price"])
            elif f["status"] == "rejected" and f["side"] == "buy" and self.mode in (LADDER, WASH):
                self.buy_left += self.pending.get(f["client_id"], 0)
        self.pending = {}

        if bar is None:
            return []

        # 3. history and account
        c = int(bar["c"])
        if self.closes is None:
            t = batch["t"]
            self.closes = [x for tt, x in zip(SEED_T, SEED_C) if tt < t][-HISTORY_KEEP:]
        self.closes.append(c)
        if len(self.closes) > HISTORY_KEEP:
            del self.closes[:-HISTORY_KEEP]
        closes = self.closes
        cash = state.cash
        eq = state.equity
        held = state.positions.get(SYMBOL)
        qty = held.qty if held is not None else 0

        # orders during a warm-up are rejected
        if state.warming_up:
            return []

        # 4. orders on the last batch are rejected
        if k >= WINDOW_BARS - 1:
            return []

        # 5. state logic
        if self.mode == FLAT:
            if eq >= START_UNITS + DONE_UNITS:
                pass
            else:
                entered = False
                if (self.wash_entries < WASH_MAX_ENTRIES and k <= WASH_LAST_BAR
                        and k - self.last_exit_seq >= WASH_COOLDOWN and len(closes) >= WASH_HIGH_BARS):
                    hi = max(closes[-WASH_HIGH_BARS:])
                    lo = min(closes[-WASH_LOW_BARS:])
                    if lo <= hi * (1 - WASH_DIP) and c >= lo * (1 + WASH_BOUNCE) and c <= lo * (1 + WASH_CHASE):
                        self.mode = WASH
                        self.wash_entries += 1
                        self.base_equity = eq
                        self.wash_entry_price = None
                        self.buy_left = cash
                        self.slice_units = self.buy_left // WASH_TWAP_IN
                        entered = True
                if not entered and not self.ladder_done and k <= LADDER_LAST_BAR and len(closes) >= SMA_BARS:
                    sma = sum(closes[-SMA_BARS:]) / SMA_BARS
                    if c >= sma * (1 + GATE_BAND):
                        self.mode = LADDER
                        self.rung = 1
                        self.first_close = c
                        self.base_equity = eq
                        self.buy_left = int(eq * RUNG_FRACTIONS[0])
                        self.slice_units = self.buy_left // TWAP_IN
        elif self.mode == LADDER:
            pos_value = state.value(SYMBOL, qty)
            est = pos_value * EXIT_COST_BPS // 10000
            if qty > 0 and eq - est >= self.base_equity + ladder_target(k):
                self.mode = EXIT
                self.sell_slices = TWAP_OUT
                self.ladder_done = True
                self.buy_left = 0
            elif c <= self.first_close * (1 - LADDER_STOP):
                self.mode = EXIT
                self.sell_slices = TWAP_OUT
                self.ladder_done = True
                self.buy_left = 0
            elif self.rung < len(RUNG_FRACTIONS) and c <= self.first_close * (1 - RUNG_DROPS[self.rung]):
                self.buy_left += int(self.base_equity * RUNG_FRACTIONS[self.rung])
                self.slice_units = self.buy_left // TWAP_IN
                self.rung += 1
        elif self.mode == WASH:
            p = self.wash_entry_price
            if qty > 0 and p is not None and (c >= p * (1 + WASH_TP) or c <= p * (1 - WASH_SL)):
                self.mode = EXIT
                self.sell_slices = TWAP_OUT
                self.buy_left = 0

        # 6. end-of-window unwind
        if self.mode in (LADDER, WASH) and k >= WINDOW_BARS - 2 - TWAP_OUT:
            self.mode = EXIT
            self.buy_left = 0
            self.sell_slices = max(1, WINDOW_BARS - 2 - k)

        # 7. execution, at most one order
        orders = []
        if self.mode in (LADDER, WASH) and self.buy_left >= MIN_NOTIONAL:
            a = min(self.slice_units, self.buy_left, cash)
            if a >= MIN_NOTIONAL:
                cid = f"b{k}"
                orders.append({"side": "buy", "client_id": cid, "symbol": SYMBOL,
                               "notional_quote_units": a, "max_slippage_bps": BUY_SLIPPAGE_BPS})
                self.pending[cid] = a
                self.buy_left -= a
        elif self.mode == EXIT:
            if qty > 0:
                if self.sell_slices <= 1:
                    q = qty
                else:
                    q = qty // self.sell_slices
                    self.sell_slices -= 1
                orders.append({"side": "sell", "client_id": f"s{k}", "symbol": SYMBOL,
                               "qty": q, "max_slippage_bps": SELL_SLIPPAGE_BPS})
            else:
                self.mode = FLAT
                self.last_exit_seq = k

        # 8. probe so the entry has at least one trade
        if k == PROBE_BAR and not self.traded and not orders and qty == 0:
            orders.append({"side": "buy", "client_id": f"p{k}", "symbol": SYMBOL,
                           "notional_quote_units": MIN_NOTIONAL, "max_slippage_bps": BUY_SLIPPAGE_BPS})

        return orders


if __name__ == "__main__":
    makefun_bars.run(Bot().on_batch)
