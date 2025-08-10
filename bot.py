# Aurum Core — fully automatic Alpaca paper trader
# Uses config.py (keys baked in). Press Run and it goes.

import time, json, math
from typing import Dict, Any, List, Optional
import requests

from config import HEADERS, TRADING_BASE, DATA_BASE

# ========= TUNED DEFAULTS =========
TIMEFRAME = "1Min"
SCAN_INTERVAL_SEC = 30
MAX_SYMBOLS_PER_SCAN = 40
MAX_CONCURRENT_POS = 5
RISK_PCT_PER_TRADE = 0.01
TAKE_PROFIT_ATR_MULT = 2.0
STOP_ATR_MULT = 1.0
MIN_PRICE = 5.0
MAX_PRICE = 800.0
MIN_AVG_VOL = 300_000
DRY_RUN = False  # set True to simulate without sending orders

UNIVERSE = [
    "AAPL","MSFT","NVDA","TSLA","AMD","META","AMZN","GOOGL","GOOG","NFLX",
    "AVGO","SMCI","ASML","SHOP","UBER","CRM","ADBE","MU","INTC","COIN",
    "PLTR","SQ","ABNB","DELL","ON","KLAC","LRCX","PANW","NOW","ANET",
    "TTD","SNOW","MDB","BABA","PDD","NIO","LI","RIVN","CVNA","DDOG",
    "CRWD","NET","ZS","OKTA","ARM","SOFI","UAL","JPM","BAC","CAT"
]

# ========= HTTP helpers =========
def _req(method: str, url: str, **kw) -> Optional[requests.Response]:
    tries = 0
    while tries < 3:
        try:
            r = requests.request(method, url, headers=HEADERS, timeout=20, **kw)
            if r.status_code == 429:
                wait = 2 ** tries
                print(f"⏳ Rate limited (429). Backing off {wait}s…")
                time.sleep(wait)
                tries += 1
                continue
            r.raise_for_status()
            return r
        except requests.HTTPError as e:
            body = getattr(e.response, "text", "")
            print(f"HTTP {getattr(e.response,'status_code', '')} {method} {url} -> {e} | {body}")
            return None
        except Exception as e:
            print(f"{method} {url} error: {e}")
            return None
    return None

def get_json(url: str, params: Dict[str, Any] = None) -> Optional[Dict[str, Any]]:
    r = _req("GET", url, params=params or {})
    return r.json() if r is not None else None

def post_json(url: str, payload: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    r = _req("POST", url, data=json.dumps(payload))
    return r.json() if r is not None else None

# ========= Alpaca wrappers =========
def get_clock() -> Optional[Dict[str, Any]]:
    return get_json(f"{TRADING_BASE}/v2/clock")

def is_open() -> bool:
    c = get_clock()
    return bool(c and c.get("is_open", False))

def get_account() -> Optional[Dict[str, Any]]:
    return get_json(f"{TRADING_BASE}/v2/account")

def list_positions() -> List[Dict[str, Any]]:
    res = get_json(f"{TRADING_BASE}/v2/positions")
    return res if isinstance(res, list) else []

def get_bars(symbol: str, timeframe: str, limit: int = 120) -> Optional[List[Dict[str, Any]]]:
    res = get_json(f"{DATA_BASE}/v2/stocks/{symbol}/bars", {"timeframe": timeframe, "limit": limit})
    if not res or "bars" not in res: return None
    return res["bars"]

def get_snapshot(symbol: str) -> Optional[Dict[str, Any]]:
    return get_json(f"{DATA_BASE}/v2/stocks/{symbol}/snapshot")

def place_bracket_order(symbol: str, qty: int, stop_price: float, take_profit_price: float, tif: str = "day"):
    payload = {
        "symbol": symbol.upper(),
        "qty": qty,
        "side": "buy",
        "type": "market",
        "time_in_force": tif,
        "order_class": "bracket",
        "take_profit": {"limit_price": round(take_profit_price, 2)},
        "stop_loss": {"stop_price": round(stop_price, 2)},
    }
    return post_json(f"{TRADING_BASE}/v2/orders", payload)

# ========= Indicators =========
def sma(values: List[float], n: int) -> float:
    if len(values) < n: return float("nan")
    return sum(values[-n:]) / n

def highest(values: List[float], n: int) -> float:
    if len(values) < n: return float("nan")
    return max(values[-n:])

def atr(highs: List[float], lows: List[float], closes: List[float], n: int = 14) -> float:
    if len(closes) < n + 1: return float("nan")
    trs = []
    for i in range(1, len(closes)):
        h, l, pc = highs[i], lows[i], closes[i-1]
        tr = max(h - l, abs(h - pc), abs(l - pc))
        trs.append(tr)
    if len(trs) < n: return float("nan")
    return sum(trs[-n:]) / n

# ========= Strategy =========
def analyze_symbol(symbol: str) -> Optional[Dict[str, Any]]:
    bars = get_bars(symbol, TIMEFRAME, limit=120)
    if not bars or len(bars) < 40: return None

    closes = [b["c"] for b in bars]
    highs  = [b["h"] for b in bars]
    lows   = [b["l"] for b in bars]
    vols   = [b["v"] for b in bars]

    last = closes[-1]
    if not (MIN_PRICE <= last <= MAX_PRICE): return None

    avg_vol = sum(vols[-30:]) / min(30, len(vols))
    if avg_vol < MIN_AVG_VOL: return None

    hi20 = highest(highs[:-1], 20)
    vol_surge = vols[-1] > 1.5 * (sum(vols[-21:-1]) / 20 if len(vols) > 21 else avg_vol)
    is_breakout = (last > hi20) and vol_surge
    if not is_breakout: return None

    a = atr(highs, lows, closes, n=14)
    if a != a: return None

    stop = last - STOP_ATR_MULT * a
    take = last + TAKE_PROFIT_ATR_MULT * a

    snap = get_snapshot(symbol)
    if not snap: return None
    if snap.get("trading_status") in {"Halted", "T1"}: return None

    strength = (last - hi20) / max(0.01, a)
    return {
        "symbol": symbol,
        "entry": float(last),
        "stop": float(stop),
        "take": float(take),
        "atr": float(a),
        "avg_vol": float(avg_vol),
        "strength": float(strength),
    }

# ========= Risk =========
def position_size(buying_power: float, entry: float, stop: float, risk_pct: float) -> int:
    risk_dollars = buying_power * risk_pct
    per_share_risk = max(0.01, entry - stop)
    shares = math.floor(risk_dollars / per_share_risk)
    if shares * entry > buying_power:
        shares = math.floor(buying_power / entry)
    return max(0, shares)

# ========= Main =========
def scan_and_trade():
    acct = get_account()
    if not acct:
        print("❌ Cannot fetch account. Check keys/base URLs."); return
    if acct.get("trading_blocked"):
        print("🚫 Trading is blocked on this account."); return

    buying_power = float(acct.get("buying_power", 0.0))
    equity = float(acct.get("equity", 0.0))
    print(f"✅ Account OK | Buying Power ${buying_power:,.2f} | Equity ${equity:,.2f}")

    pos = list_positions()
    if len(pos) >= MAX_CONCURRENT_POS:
        print(f"ℹ️ Holding {len(pos)} (limit {MAX_CONCURRENT_POS}). Skipping new entries this cycle.")
        return

    symbols = UNIVERSE[:MAX_SYMBOLS_PER_SCAN]
    candidates: List[Dict[str, Any]] = []
    for s in symbols:
        idea = analyze_symbol(s)
        if idea: candidates.append(idea)

    if not candidates:
        print("🔎 No valid breakouts this cycle."); return

    candidates.sort(key=lambda d: d["strength"], reverse=True)

    for idea in candidates:
        qty = position_size(buying_power, idea["entry"], idea["stop"], RISK_PCT_PER_TRADE)
        if qty <= 0:
            print(f"⚠️ {idea['symbol']}: size=0 (insufficient buying power)."); continue

        print(f"🚀 {idea['symbol']} | entry≈{idea['entry']:.2f} stop={idea['stop']:.2f} take={idea['take']:.2f} qty={qty}")
        if DRY_RUN:
            print("🧪 DRY_RUN=True -> not sending order.")
        else:
            resp = place_bracket_order(idea["symbol"], qty, idea["stop"], idea["take"])
            print("🧾 Order response:", resp)
        break  # one trade per cycle

def main():
    print("🤖 Aurum Core — press Run and it goes (paper)")
    print(f"   timeframe {TIMEFRAME} | scan {SCAN_INTERVAL_SEC}s | risk {int(RISK_PCT_PER_TRADE*100)}%/trade")
    print(f"   stops {STOP_ATR_MULT}×ATR | targets {TAKE_PROFIT_ATR_MULT}×ATR | DRY_RUN={DRY_RUN}")
    clock = get_clock()
    if clock:
        print(f"🕒 Market open: {clock.get('is_open')} | next open: {clock.get('next_open')} | next close: {clock.get('next_close')}")
    while True:
        try:
            if is_open(): scan_and_trade()
            else: print("🛑 Market closed. (Market orders may reject after-hours.)")
            time.sleep(SCAN_INTERVAL_SEC)
        except KeyboardInterrupt:
            print("👋 Stopped by user."); break
        except Exception as e:
            print("🔥 Loop error:", e); time.sleep(5)

if __name__ == "__main__":
    main()
