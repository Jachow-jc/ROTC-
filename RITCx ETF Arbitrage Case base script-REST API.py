"""
RITCx Algorithmic ETF Arbitrage Case - Team bot v1
Based on the Rotman BMO Finance Research and Trading Lab base script (REST API).

WHAT CHANGED FROM THE BASE SCRIPT (search for "EDIT" to find each one)
  EDIT 1  All the numbers you might change mid-round are in one SETTINGS block.
  EDIT 2  Tenders are no longer accepted blindly. Each one is priced against the
          current RITC order book (walk the book, subtract fees, add a safety buffer).
  EDIT 3  Accepted tender positions are unwound in small chunks every loop.
  EDIT 4  Position limits count RITC x2 (case rule) and are checked BEFORE trading,
          using the position you would have after the trade.
  EDIT 5  Every order result is checked and printed. A failed arb leg is reported.
  EDIT 6  The loop survives API errors instead of crashing, waits for the case to
          start, and stops cleanly with Ctrl+C.
  EDIT 7  Removed numpy (it was never used).

THINGS TO CONFIRM IN THE FIRST PRACTICE RUN (not stated in the case brief)
  - The real gross/net limits for this case (shown in the RIT Client). Put them in SETTINGS.
  - What a tender's "action" field means. This bot assumes "BUY" = you would buy RITC
    if you accept. The first tender it sees is printed raw so you can check.
  - Whether the API header is "X-API-key" or "X-API-Key" on your server.
"""

import requests
from time import sleep

API = "http://localhost:9999/v1"
API_KEY = "Rotman"                     # <-- your key from the RIT Client
HDRS = {"X-API-key": API_KEY}          # change to X-API-Key if your server needs it

# Tickers
CAD  = "CAD"    # currency instrument quoted in CAD
USD  = "USD"    # price of 1 USD in CAD (i.e., USD/CAD)
BULL = "BULL"   # stock in CAD
BEAR = "BEAR"   # stock in CAD
RITC = "RITC"   # ETF quoted in USD

# Case rules (from the case brief)
FEE_MKT = 0.02           # $/share, market orders
REBATE_LMT = 0.01        # $/share, filled limit orders (not used yet)
MAX_SIZE_EQUITY = 10000  # max shares per order for BULL/BEAR/RITC
MAX_SIZE_FX = 2500000    # max per order for CAD/USD

# ======================= EDIT 1: SETTINGS =======================
# Change these between heats or mid-round. Save the file and restart the script.
TRADING_ENABLED  = True    # False = stop sending arb orders
TENDERS_ENABLED  = True    # False = ignore all tenders
UNWIND_ENABLED   = True    # False = keep tender positions open

# Limits. PLACEHOLDERS: replace with the real numbers shown in the RIT Client.
# RITC counts x2 toward limits (case rule).
GROSS_LIMIT = 300000
NET_LIMIT   = 100000
LIMIT_BUFFER = 0.90        # only use 90% of the limit, to leave room

ORDER_QTY = 5000           # arb clip size per leg (max 10,000)

# Arb: 3 market orders ~ $0.06/sh in fees, plus a cushion for slippage.
ARB_THRESHOLD_CAD = 0.07

# Tenders: accept only if expected profit beats fees by at least this much per share.
TENDER_MIN_EDGE = 0.02     # USD per share, safety buffer on top of the fee
UNWIND_CHUNK    = 5000     # shares sold/bought per loop when unwinding a tender

LOOP_SLEEP = 0.5           # seconds between loops
# ================================================================

# --------- SESSION ----------
s = requests.Session()
s.headers.update(HDRS)

seen_tenders = set()       # tender ids we already decided on
printed_raw_tender = False


class ApiError(Exception):
    pass


# --------- HELPERS ----------
def get_json(path, params=None):
    # EDIT 6: one place that handles HTTP errors for every GET request
    r = s.get(f"{API}{path}", params=params)
    if r.status_code == 429:               # too many requests: back off a little
        sleep(0.2)
        raise ApiError("rate limited (429)")
    if not r.ok:
        raise ApiError(f"GET {path} failed: {r.status_code} {r.text[:120]}")
    return r.json()


def get_tick_status():
    # Gets simulation tick and status (e.g. ACTIVE, STOPPED)
    j = get_json("/case")
    return j["tick"], j["status"]


def get_book(ticker):
    # Full order book for a ticker. bids[0] is the best (highest) bid,
    # asks[0] is the best (lowest) ask, so [0] is always the top of the book.
    return get_json("/securities/book", params={"ticker": ticker})


def best_bid_ask(ticker):
    book = get_book(ticker)
    bid = float(book["bids"][0]["price"]) if book["bids"] else 0.0
    ask = float(book["asks"][0]["price"]) if book["asks"] else 1e12
    return bid, ask


def avg_fill_price(levels, qty):
    """Walk the book: average price to fill `qty` shares against these levels.
    Returns None if the visible book is not deep enough."""
    remaining, cost = qty, 0.0
    for lvl in levels:
        available = float(lvl["quantity"]) - float(lvl.get("quantity_filled", 0))
        if available <= 0:
            continue
        take = min(remaining, available)
        cost += take * float(lvl["price"])
        remaining -= take
        if remaining <= 0:
            return cost / qty
    return None


def positions_map():
    # Current positions for every ticker
    out = {p["ticker"]: int(p.get("position", 0)) for p in get_json("/securities")}
    for k in (BULL, BEAR, RITC, USD, CAD):
        out.setdefault(k, 0)
    return out


def place_mkt(ticker, action, qty):
    # EDIT 5: returns True/False and prints any failure so you can see it
    qty = int(min(qty, MAX_SIZE_EQUITY))
    if qty <= 0:
        return False
    r = s.post(f"{API}/orders",
               params={"ticker": ticker, "type": "MARKET",
                       "quantity": qty, "action": action})
    if not r.ok:
        print(f"  ORDER FAILED {action} {qty} {ticker}: {r.status_code} {r.text[:120]}")
    return r.ok


# --------- EDIT 4: LIMITS ----------
def gross_net(pos):
    # Case rule: BULL and BEAR count x1, RITC counts x2
    gross = abs(pos[BULL]) + abs(pos[BEAR]) + 2 * abs(pos[RITC])
    net = pos[BULL] + pos[BEAR] + 2 * pos[RITC]
    return gross, net


def fits_limits(pos, changes):
    """True if the position AFTER `changes` (e.g. {RITC: +5000, BULL: -5000})
    stays inside the buffered limits."""
    after = dict(pos)
    for t, q in changes.items():
        after[t] = after.get(t, 0) + q
    gross, net = gross_net(after)
    return gross <= GROSS_LIMIT * LIMIT_BUFFER and abs(net) <= NET_LIMIT * LIMIT_BUFFER


# --------- EDIT 2: TENDERS ----------
def evaluate_and_handle_tenders(pos):
    global printed_raw_tender
    offers = get_json("/tenders")
    for t in offers:
        tid = t["tender_id"]
        if tid in seen_tenders:
            continue
        seen_tenders.add(tid)

        if not printed_raw_tender:
            print("RAW TENDER (check the field meanings once):", t)
            printed_raw_tender = True

        ticker = t.get("ticker", RITC)
        action = t["action"]                     # ASSUMED: "BUY" = we buy if we accept
        qty = int(t["quantity"])
        price = float(t["price"])

        book = get_book(ticker)
        if action == "BUY":
            # We buy at `price`, then sell into the bids to get out
            exit_avg = avg_fill_price(book["bids"], qty)
            edge = None if exit_avg is None else (exit_avg - FEE_MKT) - price
            change = {ticker: +qty}
        else:
            # We sell at `price`, then buy from the asks to get out
            exit_avg = avg_fill_price(book["asks"], qty)
            edge = None if exit_avg is None else price - (exit_avg + FEE_MKT)
            change = {ticker: -qty}

        if edge is None:
            print(f"TENDER {tid} DECLINED: book too thin to unwind {qty}")
            s.delete(f"{API}/tenders/{tid}")
            continue
        if not fits_limits(pos, change):
            print(f"TENDER {tid} DECLINED: would break position limits")
            s.delete(f"{API}/tenders/{tid}")
            continue

        if edge >= TENDER_MIN_EDGE:
            if t.get("is_fixed_bid", True):
                resp = s.post(f"{API}/tenders/{tid}")
            else:
                resp = s.post(f"{API}/tenders/{tid}", params={"price": price})
            print(f"TENDER {tid} ACCEPTED ({resp.ok}): {action} {qty} @ {price:.2f}, "
                  f"exit avg {exit_avg:.3f}, edge {edge:+.3f}/sh = ${edge * qty:,.0f}")
        else:
            s.delete(f"{API}/tenders/{tid}")
            print(f"TENDER {tid} DECLINED: {action} {qty} @ {price:.2f}, "
                  f"edge {edge:+.3f}/sh < {TENDER_MIN_EDGE}")


# --------- EDIT 3: UNWIND ----------
def unwind_tender_position(pos):
    """Arb trades keep RITC matched by BULL+BEAR (+q RITC, -q BULL, -q BEAR).
    Anything beyond that match came from tenders and should be unwound."""
    hedged_ritc = -(pos[BULL] + pos[BEAR]) / 2
    excess = int(pos[RITC] - hedged_ritc)
    if abs(excess) < 100:
        return
    qty = min(abs(excess), UNWIND_CHUNK)
    action = "SELL" if excess > 0 else "BUY"
    if place_mkt(RITC, action, qty):
        print(f"  UNWIND {action} {qty} RITC (excess was {excess:+,})")


# --------- CORE LOGIC ----------
def step_once():
    pos = positions_map()

    if TENDERS_ENABLED:
        evaluate_and_handle_tenders(pos)
    if UNWIND_ENABLED:
        unwind_tender_position(pos)

    # Get executable prices
    bull_bid, bull_ask = best_bid_ask(BULL)
    bear_bid, bear_ask = best_bid_ask(BEAR)
    ritc_bid_usd, ritc_ask_usd = best_bid_ask(RITC)
    usd_bid, usd_ask = best_bid_ask(USD)   # USD quoted in CAD (USD/CAD)

    # Convert RITC to CAD using the USD book
    ritc_bid_cad = ritc_bid_usd + usd_bid
    ritc_ask_cad = ritc_ask_usd * usd_ask

    # Basket executable values in CAD
    basket_sell_value = bull_bid + bear_bid      # what we get if we SELL basket now
    basket_buy_cost   = bull_ask + bear_ask      # what we pay if we BUY basket now

    # Direction 1: basket rich vs ETF -> SELL basket, BUY RITC
    edge1 = basket_sell_value - ritc_ask_cad
    # Direction 2: ETF rich vs basket -> SELL RITC, BUY basket
    edge2 = ritc_bid_cad - basket_buy_cost

    traded = False
    q = min(ORDER_QTY, MAX_SIZE_EQUITY)
    pos = positions_map()   # refresh after tenders/unwind

    if TRADING_ENABLED and edge1 >= ARB_THRESHOLD_CAD and fits_limits(pos, {BULL: -q, BEAR: -q, RITC: +q}):
        legs = [place_mkt(BULL, "SELL", q), place_mkt(BEAR, "SELL", q), place_mkt(RITC, "BUY", q)]
        if not all(legs):
            print("  WARNING: an arb leg failed, position may be unhedged:", legs)
        traded = True

    elif TRADING_ENABLED and edge2 >= ARB_THRESHOLD_CAD and fits_limits(pos, {BULL: +q, BEAR: +q, RITC: -q}):
        legs = [place_mkt(BULL, "BUY", q), place_mkt(BEAR, "BUY", q), place_mkt(RITC, "SELL", q)]
        if not all(legs):
            print("  WARNING: an arb leg failed, position may be unhedged:", legs)
        traded = True

    return traded, edge1, edge2, ritc_ask_cad, pos


def main():
    # EDIT 6: wait for the case to start instead of exiting immediately
    print("Waiting for the case to become ACTIVE... (Ctrl+C to quit)")
    while True:
        try:
            tick, status = get_tick_status()
            if status == "ACTIVE":
                break
        except (ApiError, requests.RequestException) as e:
            print("Cannot reach RIT yet:", e)
        sleep(1)

    print("Case is ACTIVE. Trading.")
    try:
        while status == "ACTIVE":
            try:
                traded, e1, e2, ritc_ask_cad, pos = step_once()
                gross, net = gross_net(pos)
                print(f"tick={tick:>3} e1={e1:+.4f} e2={e2:+.4f} "
                      f"RITC={pos[RITC]:+,} BULL={pos[BULL]:+,} BEAR={pos[BEAR]:+,} "
                      f"gross={gross:,} net={net:+,}{'  TRADED' if traded else ''}")
            except (ApiError, requests.RequestException, KeyError) as e:
                # One bad response should not kill the bot
                print("  loop error, skipping this tick:", e)
            sleep(LOOP_SLEEP)
            tick, status = get_tick_status()
    except KeyboardInterrupt:
        print("\nStopped by you (Ctrl+C). No new orders will be sent.")
    print("Case no longer active. Bot finished.")


if __name__ == "__main__":
    main()
