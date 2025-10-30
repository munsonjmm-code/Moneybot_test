import json
import time
import threading
from collections import deque
from dataclasses import dataclass, asdict
from typing import Deque, Dict, List
import os

from flask import Flask, render_template_string, request, jsonify

# ---- Optional dependency handling (websocket-client) ----
try:
    import websocket  # type: ignore
    WEBSOCKET_AVAILABLE = True
except Exception:
    websocket = None
    WEBSOCKET_AVAILABLE = False

# ---- Optional dependency handling (requests for bootstrap/fetch) ----
try:
    import requests  # type: ignore
    REQUESTS_AVAILABLE = True
except Exception:
    requests = None
    REQUESTS_AVAILABLE = False


app = Flask(__name__)
APP_NAME = "JML'S Money Maker"
APP_VERSION = "0.1.0"

# --------------------------- Config ---------------------------
PUBLIC_WS_URL = "wss://fapi.bitunix.com/public/"
DEFAULT_SYMBOL = "BTCUSDT"
DEFAULT_INTERVAL = "market_kline_1min"  # server pushes roughly every 500ms
MAX_CANDLES = 500
MAX_TRADES = 2000

# --------------------------- Data Models ---------------------------
@dataclass
class Candle:
    t: int   # server ts (ms)
    o: float
    h: float
    l: float
    c: float
    v: float  # base volume

 # Global state (per symbol)
candles: Dict[str, Deque[Candle]] = {}
trades: Dict[str, Deque[dict]] = {}
ws_status = {
    "connected": False,
    "last_msg_ts": None,
    "last_error": None,
    "thread_alive": False,
    "enabled": WEBSOCKET_AVAILABLE,
    "url": PUBLIC_WS_URL,
    "subscribed": {"kline": False, "trade": False},
    "symbol": DEFAULT_SYMBOL,
    "interval": DEFAULT_INTERVAL,
}

# Handle to current websocket connection (set by _ws_thread)
ws_current = None  # type: ignore

state_lock = threading.Lock()

# --------------------------- Paper Orders & Strategy Config ---------------------------
paper_orders: Dict[str, dict] = {}
order_lock = threading.Lock()

# --------------------------- Positions (paper) ---------------------------
positions: Dict[str, dict] = {}
position_lock = threading.Lock()

strategy_cfg = {
    "window": 20,
    "multiplier": 2.5,
    "lookback": 20,
}

def _now_ms() -> int:
    return int(time.time() * 1000)

# --- Position/candle helpers ---
def _last_close(symbol: str) -> float:
    _ensure_buffers(symbol)
    buf = candles.get(symbol)
    if not buf or len(buf) == 0:
        return 0.0
    return float(buf[-1].c)

def _gen_id(prefix: str) -> str:
    return f"{prefix}-{_now_ms()}"

# --------------------------- WebSocket Client ---------------------------
def _ensure_buffers(symbol: str):
    if symbol not in candles:
        candles[symbol] = deque(maxlen=MAX_CANDLES)
    if symbol not in trades:
        trades[symbol] = deque(maxlen=MAX_TRADES)

def _on_open(ws):
    """Subscribe to kline and trade channels when connection opens."""
    try:
        print("[WS] on_open: subscribing to", ws_status["symbol"], ws_status["interval"], "and trade")
        sub = {
            "op": "subscribe",
            "args": [
                {"symbol": ws_status["symbol"], "ch": ws_status["interval"]},
                {"symbol": ws_status["symbol"], "ch": "trade"},
            ],
        }
        ws.send(json.dumps(sub))
        print("[WS] subscribe sent:", sub)
        with state_lock:
            ws_status["connected"] = True
            ws_status["subscribed"]["kline"] = True
            ws_status["subscribed"]["trade"] = True
    except Exception as e:
        print("[WS][ERROR] on_open:", e)
        with state_lock:
            ws_status["last_error"] = f"on_open error: {e}"

def _on_message(ws, message):
    """Handle incoming messages from the WebSocket."""
    # lightweight debug: show channel and a small snippet
    try:
        peek = message[:120] + ("..." if len(message) > 120 else "")
        print("[WS] on_message len", len(message), "peek:", peek)
    except Exception:
        pass
    try:
        data = json.loads(message)
        ch = data.get("ch")
        sym = data.get("symbol", ws_status["symbol"])
        ts = data.get("ts")
        with state_lock:
            ws_status["last_msg_ts"] = ts or int(time.time() * 1000)
        _ensure_buffers(sym)

        if ch and ch.startswith("market_kline_"):
            d = data.get("data") or {}
            try:
                cndl = Candle(
                    t=ts or int(time.time() * 1000),
                    o=float(d.get("o", 0)),
                    h=float(d.get("h", 0)),
                    l=float(d.get("l", 0)),
                    c=float(d.get("c", 0)),
                    v=float(d.get("b", 0)),
                )
                candles[sym].append(cndl)
            except Exception as e:
                with state_lock:
                    ws_status["last_error"] = f"parse_kline error: {e}"

        elif ch == "trade":
            arr = data.get("data") or []
            for t in arr:
                trades[sym].append(
                    {
                        "t": t.get("t"),
                        "p": float(t.get("p", 0)),
                        "v": float(t.get("v", 0)),
                        "s": t.get("s"),
                    }
                )
    except Exception as e:
        print("[WS][ERROR] on_message:", e)
        with state_lock:
            ws_status["last_error"] = f"on_message error: {e}"

def _on_error(ws, error):
    """Store errors and mark connection as disconnected."""
    with state_lock:
        ws_status["last_error"] = str(error)
        ws_status["connected"] = False
    print("[WS][ERROR] socket error:", error)

def _on_close(ws, status_code, msg):
    """Handle cleanly closing the WebSocket connection."""
    with state_lock:
        ws_status["connected"] = False
        ws_status["subscribed"]["kline"] = False
        ws_status["subscribed"]["trade"] = False
    print(f"[WS] on_close status={status_code} msg={msg}")
    global ws_current
    ws_current = None

def _ping_loop(ws):
    """Send periodic ping frames expected by Bitunix."""
    while True:
        time.sleep(15)
        if not ws_status["connected"]:
            break
        try:
            ws.send(json.dumps({"op": "ping", "ping": int(time.time())}))
        except Exception:
            break

# --- Helper to close current websocket connection (forces reconnect) ---
def _ws_close():
    """Close the current WebSocket connection if open; background thread will reconnect."""
    global ws_current
    try:
        if ws_current is not None:
            ws_current.close()
    except Exception:
        pass

def _ws_thread():
    """Background thread to manage the WebSocket connection with auto-reconnect."""
    global ws_current
    if not WEBSOCKET_AVAILABLE:
        return
    while True:
        try:
            print("[WS] connecting to", PUBLIC_WS_URL, "for", ws_status["symbol"], ws_status["interval"])
            ws = websocket.WebSocketApp(
                PUBLIC_WS_URL,
                on_open=_on_open,
                on_message=_on_message,
                on_error=_on_error,
                on_close=_on_close,
            )
            # Save handle so API endpoints can request an immediate reconnect by closing it
            ws_current = ws
            ping_thread = threading.Thread(target=_ping_loop, args=(ws,), daemon=True)
            ping_thread.start()
            ws.run_forever()
            print("[WS] run_forever() returned; will attempt reconnect")
        except Exception as e:
            print("[WS][EXCEPTION] in _ws_thread:", e)
            with state_lock:
                ws_status["last_error"] = f"ws_thread exception: {e}"
            # Ensure handle cleared on failure
            ws_current = None
        time.sleep(3)  # backoff before reconnecting

# Start background thread on boot
if WEBSOCKET_AVAILABLE:
    t = threading.Thread(target=_ws_thread, daemon=True)
    t.start()
    with state_lock:
        ws_status["thread_alive"] = True

# --------------------------- Strategy Helpers ---------------------------
def compute_spikes(symbol: str, window: int = 20, multiplier: float = 2.5, limit: int = 50):
    """Compute volume spikes given a rolling window and threshold multiplier."""
    _ensure_buffers(symbol)
    buf = list(candles[symbol])[-(window + limit + 5):]
    if len(buf) < max(window, 5):
        return []

    spikes = []
    for i in range(window, len(buf)):
        prev = buf[i - window:i]
        avg_v = sum(c.v for c in prev) / float(len(prev))
        cur = buf[i]
        ratio = (cur.v / avg_v) if avg_v > 0 else 0
        if ratio >= multiplier:
            direction = "long" if cur.c >= cur.o else "short"
            spikes.append(
                {
                    "t": cur.t,
                    "o": cur.o,
                    "h": cur.h,
                    "l": cur.l,
                    "c": cur.c,
                    "v": cur.v,
                    "avg_v": avg_v,
                    "ratio": ratio,
                    "direction_hint": direction,
                }
            )
    return spikes[-limit:]

def breakout_signal(symbol: str, lookback: int = 20, window: int = 20, multiplier: float = 2.5):
    """Determine breakout signal based on volume spikes and price breakout from range."""
    _ensure_buffers(symbol)
    buf = list(candles[symbol])
    if len(buf) < max(lookback + 1, window + 1):
        return {"hasSignal": False, "reason": "insufficient candles"}

    spikes = compute_spikes(symbol, window=window, multiplier=multiplier, limit=5)
    if not spikes:
        return {"hasSignal": False, "reason": "no spikes"}

    cur = buf[-1]
    range_buf = buf[-lookback:]
    hh = max(c.h for c in range_buf[:-1])  # exclude current candle
    ll = min(c.l for c in range_buf[:-1])

    direction = None
    entry = None
    if cur.c > hh and spikes[-1]["ratio"] >= multiplier:
        direction = "long"
        entry = cur.c
    elif cur.c < ll and spikes[-1]["ratio"] >= multiplier:
        direction = "short"
        entry = cur.c

    if not direction:
        return {"hasSignal": False, "reason": "no breakout", "hh": hh, "ll": ll, "last_close": cur.c}

    # basic risk/reward: stop loss at recent range
    if direction == "long":
        sl = max(ll, cur.c - (cur.h - cur.l))
        risk = entry - sl
        tp = entry + 1.5 * risk
    else:
        sl = min(hh, cur.c + (cur.h - cur.l))
        risk = sl - entry
        tp = entry - 1.5 * risk

    return {
        "hasSignal": True,
        "symbol": symbol,
        "direction": direction,
        "entry": round(entry, 8),
        "sl": round(sl, 8),
        "tp": round(tp, 8),
        "lookback": lookback,
        "window": window,
        "spike_ratio": round(spikes[-1]["ratio"], 3),
        "hh": hh,
        "ll": ll,
        "confidence": 0.6 + min(0.3, (spikes[-1]["ratio"] - multiplier) * 0.1),
    }

# --------------------------- Routes ---------------------------
PAGE = """
<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>JML'S Money Maker</title>
  <style>
    body { font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; margin: 24px; }
    h1 { margin: 0 0 12px; }
    .banner { background: #fff3cd; border: 1px solid #ffeeba; padding: 12px 14px; border-radius: 8px; margin: 12px 0 20px; }
    .banner strong { display: block; margin-bottom: 4px; }
    .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin-bottom: 8px; }
    label { display: flex; align-items: center; gap: 6px; }
    input { padding: 8px; border: 1px solid #ccc; border-radius: 6px; }
    button { padding: 10px 14px; border: 1px solid #222; border-radius: 8px; background: #f5f5f5; cursor: pointer; }
    button:active { transform: translateY(1px); }
    #status { margin-top: 10px; white-space: pre-wrap; }
    .muted { color: #555; font-size: 14px; }
    code { background: #f6f6f6; padding: 2px 4px; border-radius: 4px; }
    /* --- Top-right menu --- */
    .topbar { position: absolute; top: 16px; right: 16px; }
    .menu-button { padding: 8px 10px; border: 1px solid #222; border-radius: 8px; background: #f5f5f5; cursor: pointer; }
    .dropdown { position: absolute; right: 0; margin-top: 6px; background: #fff; border: 1px solid #ccc; border-radius: 8px; min-width: 180px; box-shadow: 0 8px 24px rgba(0,0,0,0.15); }
    .dropdown .item { display: block; width: 100%; text-align: left; background: transparent; border: 0; padding: 10px 12px; cursor: pointer; }
    .dropdown .item:hover { background: #f2f2f2; }
    .hidden { display: none; }

    /* --- Dark theme --- */
    body.dark { background: #0f1115; color: #e6e6e6; }
    body.dark .banner { background: #1b1f2a; border-color: #2a2f3a; }
    body.dark input, body.dark select { background: #1a1f2b; color: #e6e6e6; border-color: #333; }
    body.dark button, body.dark .menu-button { background: #1f232b; border-color: #3a3f4b; color: #e6e6e6; }
    body.dark code { background: #1a1d24; color: #d6d6d6; }
    body.dark .muted { color: #a9b0bb; }
    body { transition: background 0.2s ease, color 0.2s ease; }
  </style>
</head>
<body>

  <h1>JML'S Money Maker</h1>
  <div class="topbar">
    <button class="menu-button" onclick="toggleMenu()">☰ Menu</button>
    <div id="menuDropdown" class="dropdown hidden">
      <button class="item" onclick="toggleTheme()">🌙 Toggle Dark Mode</button>
      <button class="item" onclick="reconnectWS()">🔄 Reconnect WS</button>
      <button class="item" onclick="applyAndReconnect()">⚡ Apply &amp; Reconnect</button>
      <button class="item" onclick="resetBuffers()">🧹 Reset Buffers</button>
      <button class="item" onclick="resetOrders()">🗑️ Reset Orders</button>
      <button class="item" onclick="listEndpoints()">📋 List Endpoints</button>
      <button class="item" onclick="metrics()">📈 Metrics</button>
      <button class="item" onclick="listPositions()">📂 Positions</button>
    </div>
  </div>

  <div class="banner">
    <strong>Visible Note / Banner</strong>
    <div class="muted" id="bannerText">
      Live data: <span id="liveFlag">(checking...)</span> • Symbol: <span id="sym"></span> • Interval: <span id="intv"></span>
    </div>
  </div>

  <div class="row">
    <label>Symbol <input id="symbol" value="BTCUSDT" /></label>
    <label>Price <input id="price" placeholder="0.19004" /></label>
    <label>Qty <input id="qty" placeholder="1" /></label>
    <label>Side
      <select id="side">
        <option value="buy" selected>BUY</option>
        <option value="sell">SELL</option>
      </select>
    </label>
    <label>Cancel After (s) <input id="cancelAfter" value="15" /></label>
    <button onclick="placeLimit()">Place Limit (auto-cancel)</button>
    <button onclick="getSignal()">Detect Breakout</button>
    <button onclick="showSpikes()">Recent Spikes</button>
    <button onclick="applyAndReconnect()">Apply &amp; Reconnect WS</button>
    <button onclick="reconnectWS()">Reconnect Now</button>
    <button onclick="listEndpoints()">Endpoints</button>
    <button onclick="suggestPos()">Suggest</button>
    <button onclick="executeSuggestion()">Execute (1% / $1000)</button>
    <button onclick="listPositions()">Positions</button>
    <button onclick="runBacktest()">Backtest</button>
    <button onclick="bootstrapStatus()">Bootstrap Status</button>
    <button onclick="seedDemo()">Seed Demo Candles</button>
    <button onclick="metrics()">Metrics</button>
    <select id="preset">
      <option value="aggressive">Aggressive</option>
      <option value="balanced" selected>Balanced</option>
      <option value="conservative">Conservative</option>
    </select>
    <button onclick="applyPreset()">Apply Preset</button>
    <button onclick="runGrid()">Grid Search</button>
  </div>

  <p id="status" class="muted">Ready.</p>

  <script>
    // --- Theme and menu helpers ---
    function setTheme(theme) {
      const dark = theme === 'dark';
      document.body.classList.toggle('dark', dark);
      try { localStorage.setItem('theme', dark ? 'dark' : 'light'); } catch {}
    }
    function initTheme() {
      let t = 'light';
      try { t = localStorage.getItem('theme') || 'light'; } catch {}
      setTheme(t);
    }
    function toggleTheme() {
      const dark = document.body.classList.contains('dark');
      setTheme(dark ? 'light' : 'dark');
    }
    function toggleMenu() {
      const dd = document.getElementById('menuDropdown');
      dd.classList.toggle('hidden');
    }
    // Close dropdown when clicking outside
    document.addEventListener('click', (e) => {
      const dd = document.getElementById('menuDropdown');
      const btn = document.querySelector('.menu-button');
      if (!dd || !btn) return;
      if (!dd.contains(e.target) && !btn.contains(e.target)) {
        dd.classList.add('hidden');
      }
    });
    async function placeLimit() {
      const body = {
        symbol: document.getElementById('symbol').value,
        price: document.getElementById('price').value,
        qty: document.getElementById('qty').value,
        side: document.getElementById('side').value,
        cancel_after: parseInt(document.getElementById('cancelAfter').value || '15', 10)
      };
      try {
        const res = await fetch('/api/order/limit', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(body)
        });
        const json = await res.json();
        document.getElementById('status').textContent = json.message || JSON.stringify(json);
      } catch (e) {
        document.getElementById('status').textContent = 'Request failed: ' + e;
      }
    }

    async function getSignal() {
      const body = {
        symbol: document.getElementById('symbol').value,
        interval: 'market_kline_1min',
        window: 20,
        multiplier: 2.5,
        lookback: 20
      };
      const res = await fetch('/api/signal/breakout', {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify(body)
      });
      const json = await res.json();
      document.getElementById('status').textContent = JSON.stringify(json, null, 2);
    }

    async function showSpikes() {
      const symbol = document.getElementById('symbol').value;
      const res = await fetch(`/api/volume-spikes?symbol=${encodeURIComponent(symbol)}&window=20&multiplier=2.5&limit=10`);
      const json = await res.json();
      document.getElementById('status').textContent = JSON.stringify(json, null, 2);
    }

    async function applyAndReconnect() {
      const body = {
        symbol: document.getElementById('symbol').value,
        interval: 'market_kline_1min'
      };
      try {
        const res = await fetch('/api/ws/subscribe', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify(body)
        });
        const json = await res.json();
        document.getElementById('status').textContent = JSON.stringify(json, null, 2);
        refreshBanner();
      } catch (e) {
        document.getElementById('status').textContent = 'WS subscribe failed: ' + e;
      }
    }
    async function reconnectWS() {
      try {
        const res = await fetch('/api/ws/reconnect', { method: 'POST' });
        const json = await res.json();
        document.getElementById('status').textContent = JSON.stringify(json, null, 2);
        refreshBanner();
      } catch (e) {
        document.getElementById('status').textContent = 'WS reconnect failed: ' + e;
      }
    }

    async function resetBuffers() {
      try {
        const sym = document.getElementById('symbol').value || 'BTCUSDT';
        const res = await fetch('/api/buffers/reset', {
          method: 'POST',
          headers: {'Content-Type':'application/json'},
          body: JSON.stringify({symbol: sym})
        });
        const json = await res.json();
        document.getElementById('status').textContent = JSON.stringify(json, null, 2);
      } catch (e) {
        document.getElementById('status').textContent = 'Reset buffers failed: ' + e;
      }
    }
    async function resetOrders() {
      try {
        const res = await fetch('/api/orders/reset', { method: 'POST' });
        const json = await res.json();
        document.getElementById('status').textContent = JSON.stringify(json, null, 2);
      } catch (e) {
        document.getElementById('status').textContent = 'Reset orders failed: ' + e;
      }
    }

    async function listEndpoints() {
      try {
        const res = await fetch('/api/endpoints');
        const json = await res.json();
        document.getElementById('status').textContent = JSON.stringify(json, null, 2);
      } catch (e) {
        document.getElementById('status').textContent = 'Endpoints fetch failed: ' + e;
      }
    }

    async function suggestPos() {
      const body = { symbol: document.getElementById('symbol').value };
      const r = await fetch('/api/position/suggest', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
      const j = await r.json();
      document.getElementById('status').textContent = JSON.stringify(j, null, 2);
    }

    async function executeSuggestion() {
      const body = { symbol: document.getElementById('symbol').value, balance: 1000, risk_pct: 0.01, leverage: 5 };
      const r = await fetch('/api/position/execute', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
      const j = await r.json();
      document.getElementById('status').textContent = JSON.stringify(j, null, 2);
    }

    async function listPositions() {
      const r = await fetch('/api/positions?status=open');
      const j = await r.json();
      document.getElementById('status').textContent = JSON.stringify(j, null, 2);
    }

    async function runBacktest() {
      const body = { symbol: document.getElementById('symbol').value, resolve_bars: 10, max_trades: 150 };
      const r = await fetch('/api/backtest', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
      const j = await r.json();
      document.getElementById('status').textContent = JSON.stringify(j, null, 2);
    }

    async function bootstrapStatus() {
      const sym = document.getElementById('symbol').value;
      const r = await fetch(`/api/bootstrap/status?symbol=${encodeURIComponent(sym)}`);
      const j = await r.json();
      document.getElementById('status').textContent = JSON.stringify(j, null, 2);
    }

    // Seed ~60 synthetic candles so signals/backtests work immediately
    async function seedDemo() {
      const sym = document.getElementById('symbol').value;
      const baseTs = Date.now() - 60*60*1000; // last hour
      const N = 60;
      const arr = [];
      let price = 100;
      for (let i=0;i<N;i++) {
        const t = baseTs + i*60*1000;
        const drift = (Math.sin(i/6) + Math.random()*0.4 - 0.2) * 0.8;
        const o = price;
        const c = price + drift;
        const h = Math.max(o, c) + Math.random()*0.6;
        const l = Math.min(o, c) - Math.random()*0.6;
        const v = 10 + Math.max(0, (Math.sin(i/5)+1)*8) + Math.random()*3;
        arr.push({t: Math.floor(t), o: +o.toFixed(4), h: +h.toFixed(4), l: +l.toFixed(4), c: +c.toFixed(4), v: +v.toFixed(3)});
        price = c;
      }
      const body = { symbol: sym, replace: true, candles: arr };
      const r = await fetch('/api/bootstrap/candles', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
      const j = await r.json();
      document.getElementById('status').textContent = JSON.stringify(j, null, 2);
    }

    async function metrics() {
      const r = await fetch('/api/metrics');
      const j = await r.json();
      document.getElementById('status').textContent = JSON.stringify(j, null, 2);
    }
    async function applyPreset() {
      const name = document.getElementById('preset').value;
      const r = await fetch('/api/strategy/presets', {method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name})});
      const j = await r.json();
      document.getElementById('status').textContent = JSON.stringify(j, null, 2);
    }
    async function runGrid() {
      const body = {
        symbol: document.getElementById('symbol').value,
        window: {"start":5,"stop":20,"step":5},
        multiplier: [1.3,1.6,2.0,2.5],
        lookback: [10,20,30],
        resolve_bars: 10, max_trades: 200, tie_breaker: "sl_wins", fee_bps: 10, slippage: 0.0
      };
      const r = await fetch('/api/backtest/grid', { method:'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify(body) });
      const j = await r.json();
      document.getElementById('status').textContent = JSON.stringify(j, null, 2);
    }
    async function refreshBanner() {
      try {
        const res = await fetch('/api/health');
        const j = await res.json();
        document.getElementById('sym').textContent = j.symbol;
        document.getElementById('intv').textContent = j.interval;
        document.getElementById('liveFlag').textContent = j.connected && j.enabled ? 'LIVE' : 'OFFLINE';
      } catch {}
    }
    setInterval(refreshBanner, 3000);
    refreshBanner();
    initTheme();
  </script>
</body>
</html>
"""

@app.route("/")
def index():
  return render_template_string(PAGE)

@app.route("/api/health")
def api_health():
  with state_lock:
    return jsonify(
      connected=ws_status["connected"],
      last_msg_ts=ws_status["last_msg_ts"],
      last_error=ws_status["last_error"],
      enabled=ws_status["enabled"],
      url=ws_status["url"],
      subscribed=ws_status["subscribed"],
      symbol=ws_status["symbol"],
      interval=ws_status["interval"],
      thread_alive=ws_status["thread_alive"],
    )

@app.route("/api/ping")
def api_ping():
  return jsonify(ok=True, app=APP_NAME, version=APP_VERSION, ts=_now_ms())

@app.route("/api/candles")
def api_candles():
  symbol = request.args.get("symbol", DEFAULT_SYMBOL)
  limit = int(request.args.get("limit", "100"))
  _ensure_buffers(symbol)
  out = [asdict(c) for c in list(candles[symbol])[-limit:]]
  return jsonify(symbol=symbol, candles=out)


# --- Recent trades endpoint ---
@app.route("/api/trades")
def api_trades():
  symbol = request.args.get("symbol", DEFAULT_SYMBOL)
  limit = int(request.args.get("limit", "100"))
  _ensure_buffers(symbol)
  out = list(trades[symbol])[-limit:]
  return jsonify(symbol=symbol, trades=out)

@app.route("/api/volume-spikes")
def api_volume_spikes():
  symbol = request.args.get("symbol", DEFAULT_SYMBOL)
  window = int(request.args.get("window", "20"))
  multiplier = float(request.args.get("multiplier", "2.5"))
  limit = int(request.args.get("limit", "20"))
  sp = compute_spikes(symbol, window=window, multiplier=multiplier, limit=limit)
  return jsonify(symbol=symbol, window=window, multiplier=multiplier, spikes=sp)


# --- Endpoints index ---
@app.route("/api/endpoints")
def api_endpoints():
  # Updated endpoints list: add /api/ws/subscribe and /api/ws/reconnect, new descriptions, and remove duplicate reconnect if present
  endpoints=[
    {"method":"GET","path":"/api/health","desc":"Websocket/health status"},
    {"method":"GET","path":"/api/ping","desc":"Simple liveness check with version"},
    {"method":"GET","path":"/api/candles?symbol=SYM&amp;limit=N","desc":"Recent candles"},
    {"method":"GET","path":"/api/trades?symbol=SYM&amp;limit=N","desc":"Recent trades"},
    {"method":"GET","path":"/api/volume-spikes?symbol=SYM&amp;window=W&amp;multiplier=M&amp;limit=N","desc":"Volume spike scan"},
    {"method":"GET","path":"/api/signal/levels?symbol=SYM&amp;lookback=L","desc":"Recent range HH/LL and last close"},
    {"method":"POST","path":"/api/signal/breakout","desc":"Breakout decision from spikes + range"},
    {"method":"POST","path":"/api/signal/summary","desc":"Combined snapshot for spikes + breakout"},
    {"method":"POST","path":"/api/bootstrap/candles","desc":"Seed candles from provided list or external URL"},
    {"method":"GET","path":"/api/bootstrap/status","desc":"Counts of candles/trades for a symbol"},
    {"method":"POST","path":"/api/position/suggest","desc":"Suggest position (direction, entry, SL, TP)"},
    {"method":"POST","path":"/api/position/execute","desc":"Run suggest and open paper position with risk sizing"},
    {"method":"POST","path":"/api/position/open","desc":"Open paper position"},
    {"method":"POST","path":"/api/position/close","desc":"Close paper position (market)"},
    {"method":"GET","path":"/api/positions?status=open","desc":"List paper positions with PnL (unrealized for open)"},
    {"method":"POST","path":"/api/position/simulate","desc":"Size position by risk % (qty, notional, RR)"},
    {"method":"POST","path":"/api/backtest","desc":"Backtest breakout+spike logic over recent candles"},
    {"method":"POST","path":"/api/backtest/grid","desc":"Parameter sweep over (window,multiplier,lookback)"},
    {"method":"GET","path":"/api/metrics","desc":"Portfolio/positions analytics (PnL, expectancy, drawdown, streaks)"},
    {"method":"GET","path":"/api/strategy/presets","desc":"List built-in strategy presets"},
    {"method":"POST","path":"/api/strategy/presets","desc":"Apply a preset to strategy config"},
    {"method":"GET","path":"/api/config","desc":"Get default strategy params"},
    {"method":"POST","path":"/api/config","desc":"Set default strategy params"},
    {"method":"POST","path":"/api/ws/update","desc":"Update symbol/interval for WS (applies on next reconnect)"},
    {"method":"POST","path":"/api/ws/subscribe","desc":"Update WS symbol/interval and reconnect now"},
    {"method":"POST","path":"/api/ws/reconnect","desc":"Force-close socket to trigger reconnect"},
    {"method":"POST","path":"/api/status/clear_error","desc":"Clear last error"},
    {"method":"POST","path":"/api/order/market","desc":"Simulated market order (records filled)"},
    {"method":"POST","path":"/api/order/limit","desc":"Simulated limit order with side &amp; auto-cancel"},
    {"method":"GET","path":"/api/orders?status=open","desc":"List paper orders (optional status filter)"},
    {"method":"POST","path":"/api/order/cancel","desc":"Cancel paper order by id (if open)"},
    {"method":"POST","path":"/api/orders/reset","desc":"Clear paper orders"},
    {"method":"POST","path":"/api/buffers/reset","desc":"Clear candle/trade buffers for a symbol"},
    {"method":"POST","path":"/api/backtest/report?format=csv|json","desc":"Backtest + return CSV or JSON"}
  ]
  # Remove any duplicate /api/ws/reconnect entries (keep only the one with new description)
  seen = set()
  endpoints_clean = []
  for ep in endpoints:
    key = (ep["method"], ep["path"])
    if key not in seen:
      endpoints_clean.append(ep)
      seen.add(key)
  return jsonify(endpoints=endpoints_clean)
# --- Bootstrap routes (seed candles) ---
@app.route("/api/bootstrap/status")
def api_bootstrap_status():
  symbol = request.args.get("symbol", DEFAULT_SYMBOL)
  _ensure_buffers(symbol)
  return jsonify(ok=True, symbol=symbol, candles=len(candles[symbol]), trades=len(trades[symbol]), requests_available=REQUESTS_AVAILABLE)

@app.route("/api/bootstrap/candles", methods=["POST"])
def api_bootstrap_candles():
  """
  Seed the in-memory candle buffer in two ways:
  - Body contains {"candles":[{"t":..,"o":..,"h":..,"l":..,"c":..,"v":..}, ...], "symbol":"...", "replace":false}
  - OR body contains {"url":"https://...", "symbol":"...", "json_path":"data"} to fetch from an external URL (if requests available).
  Any parse errors are skipped.
  """
  data = request.get_json(silent=True) or {}
  symbol = data.get("symbol", DEFAULT_SYMBOL)
  replace = bool(data.get("replace", False))
  _ensure_buffers(symbol)

  inserted = 0
  used_url = None

  if data.get("candles"):
    rows = data.get("candles") or []
    seq = []
    for r in rows:
      try:
        seq.append(Candle(
          t=int(r.get("t")),
          o=float(r.get("o")), h=float(r.get("h")), l=float(r.get("l")),
          c=float(r.get("c")), v=float(r.get("v"))
        ))
      except Exception:
        continue
    if replace:
      candles[symbol].clear()
    for cndl in seq:
      candles[symbol].append(cndl)
    inserted = len(seq)

  elif data.get("url"):
    if not REQUESTS_AVAILABLE:
      return jsonify(ok=False, reason="requests not available"), 400
    url = str(data.get("url"))
    used_url = url
    try:
      resp = requests.get(url, timeout=10)
      resp.raise_for_status()
      payload = resp.json()
      json_path = data.get("json_path")  # e.g., "data.list"
      node = payload
      if json_path:
        for key in str(json_path).split("."):
          if key == "":
            continue
          node = node.get(key, {})
      rows = node if isinstance(node, list) else []
      seq = []
      for r in rows:
        # Try multiple common field names
        t = r.get("t") or r.get("ts") or r.get("time")
        o = r.get("o") or r.get("open")
        h = r.get("h") or r.get("high")
        l = r.get("l") or r.get("low")
        c = r.get("c") or r.get("close")
        v = r.get("v") or r.get("volume") or r.get("b")
        if None in (t,o,h,l,c,v):
          continue
        try:
          seq.append(Candle(t=int(t), o=float(o), h=float(h), l=float(l), c=float(c), v=float(v)))
        except Exception:
          continue
      if replace:
        candles[symbol].clear()
      for cndl in seq:
        candles[symbol].append(cndl)
      inserted = len(seq)
    except Exception as e:
      return jsonify(ok=False, reason=f"fetch failed: {e}")
  else:
    return jsonify(ok=False, reason="provide candles[] or url"), 400

  return jsonify(ok=True, symbol=symbol, inserted=inserted, url=used_url, total=len(candles[symbol]))
# --- Position sizing utility ---
@app.route("/api/position/simulate", methods=["POST"])
def api_position_simulate():
  data = request.get_json(silent=True) or {}
  entry = float(data.get("entry", 0))
  sl = float(data.get("sl", 0))
  balance = float(data.get("balance", 0))
  risk_pct = float(data.get("risk_pct", 0.01))
  leverage = float(data.get("leverage", 1))
  if entry <= 0 or sl <= 0 or balance <= 0 or risk_pct <= 0:
    return jsonify(ok=False, reason="entry, sl, balance, risk_pct required and > 0"), 400
  risk_amt = balance * risk_pct
  per_unit_risk = abs(entry - sl)
  if per_unit_risk == 0:
    return jsonify(ok=False, reason="entry and sl cannot be equal"), 400
  qty = risk_amt / per_unit_risk
  notional = qty * entry
  rr15 = 1.5  # matches breakout TP sizing
  tp = entry + rr15 * (entry - sl) if entry > sl else entry - rr15 * (sl - entry)
  return jsonify(ok=True, qty=qty, notional=notional, tp=tp, risk_amount=risk_amt, per_unit_risk=per_unit_risk, leverage=leverage)

# --- Positions (paper) ---
@app.route("/api/position/open", methods=["POST"])
def api_position_open():
  data = request.get_json(silent=True) or {}
  symbol = data.get("symbol", DEFAULT_SYMBOL)
  side = str(data.get("side", "long")).lower()
  entry = float(data.get("entry"))
  qty = float(data.get("qty", 0))
  sl = float(data.get("sl"))
  tp = float(data.get("tp"))
  leverage = float(data.get("leverage", 1))
  if not all([symbol, entry, qty, sl, tp]):
    return jsonify(ok=False, reason="symbol, entry, qty, sl, tp required"), 400
  pid = _gen_id("POS")
  pos = {
    "id": pid, "symbol": symbol, "side": "long" if side != "short" else "short",
    "entry": entry, "qty": qty, "sl": sl, "tp": tp, "leverage": leverage,
    "status": "open", "created_at": _now_ms(), "updated_at": _now_ms(),
    "exit": None, "closed_at": None, "realized_pnl": 0.0
  }
  with position_lock:
    positions[pid] = pos
  return jsonify(ok=True, position=pos)

@app.route("/api/position/close", methods=["POST"])
def api_position_close():
  data = request.get_json(silent=True) or {}
  pid = data.get("position_id")
  price = data.get("price")
  if not pid:
    return jsonify(ok=False, reason="position_id required"), 400
  with position_lock:
    p = positions.get(pid)
    if not p:
      return jsonify(ok=False, reason="position not found"), 404
    if p["status"] != "open":
      return jsonify(ok=False, reason=f"position is {p['status']}"), 409
    mkt = float(price) if price is not None else _last_close(p["symbol"])
    if mkt <= 0:
      return jsonify(ok=False, reason="no price available to close"), 400
    # Realized PnL (simple, no fees)
    if p["side"] == "long":
      pnl = (mkt - p["entry"]) * p["qty"]
    else:
      pnl = (p["entry"] - mkt) * p["qty"]
    p.update({"status":"closed","exit":mkt,"closed_at":_now_ms(),"updated_at":_now_ms(),"realized_pnl":pnl})
  return jsonify(ok=True, position=p)

@app.route("/api/positions")
def api_positions():
  status = request.args.get("status")
  sym = request.args.get("symbol")
  with position_lock:
    vals = list(positions.values())
  if status:
    vals = [p for p in vals if p.get("status") == status]
  if sym:
    vals = [p for p in vals if p.get("symbol") == sym]
  # attach unrealized PnL for open positions
  out = []
  for p in vals:
    pcopy = dict(p)
    if pcopy["status"] == "open":
      last = _last_close(pcopy["symbol"])
      if last > 0:
        if pcopy["side"] == "long":
          upnl = (last - pcopy["entry"]) * pcopy["qty"]
        else:
          upnl = (pcopy["entry"] - last) * pcopy["qty"]
      else:
        upnl = None
      pcopy["unrealized_pnl"] = upnl
      pcopy["mark"] = last
    out.append(pcopy)
  return jsonify(ok=True, positions=out, count=len(out))

# --- Execute wrapper: run suggest, size, and open ---
@app.route("/api/position/execute", methods=["POST"])
def api_position_execute():
  """
  Run /api/position/suggest; if ok, size the trade by balance & risk_pct, then open a paper position.
  Body: {"symbol":"BTCUSDT","balance":1000,"risk_pct":0.01,"leverage":5, overrides...}
  """
  data = request.get_json(silent=True) or {}
  symbol = data.get("symbol", DEFAULT_SYMBOL)
  balance = float(data.get("balance", 0))
  risk_pct = float(data.get("risk_pct", 0.01))
  leverage = float(data.get("leverage", 1))
  # overrides
  window = int(data.get("window", strategy_cfg["window"]))
  multiplier = float(data.get("multiplier", strategy_cfg["multiplier"]))
  lookback = int(data.get("lookback", strategy_cfg["lookback"]))

  # get suggestion
  sig = breakout_signal(symbol, lookback=lookback, window=window, multiplier=multiplier)
  if not sig.get("hasSignal"):
    return jsonify(ok=False, reason=sig.get("reason","no signal"), suggestion=sig), 409

  entry, sl, tp = float(sig["entry"]), float(sig["sl"]), float(sig["tp"])
  # sizing
  sim = {"entry": entry, "sl": sl, "balance": balance, "risk_pct": risk_pct, "leverage": leverage}
  # reuse same math
  risk_amt = balance * risk_pct
  per_unit_risk = abs(entry - sl)
  if per_unit_risk <= 0:
    return jsonify(ok=False, reason="invalid entry/sl for sizing", suggestion=sig), 400
  qty = risk_amt / per_unit_risk
  side = "long" if sig["direction"] == "long" else "short"

  # open position
  pid = _gen_id("POS")
  pos = {
    "id": pid, "symbol": symbol, "side": side, "entry": entry, "qty": qty,
    "sl": sl, "tp": tp, "leverage": leverage, "status": "open",
    "created_at": _now_ms(), "updated_at": _now_ms(),
    "exit": None, "closed_at": None, "realized_pnl": 0.0
  }
  with position_lock:
    positions[pid] = pos

  return jsonify(ok=True, opened=pos, suggestion=sig, sizing={"qty": qty, "risk_amount": risk_amt})
# --- Maintenance endpoints ---
@app.route("/api/orders/reset", methods=["POST"])
def api_orders_reset():
  with order_lock:
    paper_orders.clear()
  return jsonify(ok=True, message="orders cleared")

@app.route("/api/buffers/reset", methods=["POST"])
def api_buffers_reset():
  data = request.get_json(silent=True) or {}
  symbol = data.get("symbol", DEFAULT_SYMBOL)
  _ensure_buffers(symbol)
  candles[symbol].clear()
  trades[symbol].clear()
  return jsonify(ok=True, symbol=symbol, message="buffers cleared")

@app.route("/api/ws/reconnect", methods=["POST"])
def api_ws_reconnect():
  """Request an immediate reconnect by closing the active socket if present."""
  with state_lock:
    ws_status["last_error"] = "manual reconnect requested"
  _ws_close()
  return jsonify(ok=True, message="Reconnect requested. Closing current WS to force reconnect.")

# --- Update symbol/interval and force immediate resubscribe ---
@app.route("/api/ws/subscribe", methods=["POST"])
def api_ws_subscribe():
  """
  Update symbol/interval and immediately reconnect WS to apply changes.
  Body: {"symbol":"BTCUSDT","interval":"market_kline_1min"}
  """
  data = request.get_json(silent=True) or {}
  symbol = data.get("symbol", DEFAULT_SYMBOL)
  interval = data.get("interval", DEFAULT_INTERVAL)
  with state_lock:
    ws_status["symbol"] = symbol
    ws_status["interval"] = interval
    ws_status["last_error"] = "subscribe requested; applying on reconnect"
  _ws_close()
  return jsonify(ok=True, message="Updated WS subscription and closing current socket to apply.", symbol=symbol, interval=interval)

@app.route("/api/signal/breakout", methods=["POST"])
def api_signal_breakout():
  data = request.get_json(silent=True) or {}
  symbol = data.get("symbol", DEFAULT_SYMBOL)
  window = int(data.get("window", strategy_cfg["window"]))
  multiplier = float(data.get("multiplier", strategy_cfg["multiplier"]))
  lookback = int(data.get("lookback", strategy_cfg["lookback"]))
  sig = breakout_signal(symbol, lookback=lookback, window=window, multiplier=multiplier)
  return jsonify(sig)


# --- Levels endpoint: expose recent range levels ---
@app.route("/api/signal/levels")
def api_signal_levels():
  symbol = request.args.get("symbol", DEFAULT_SYMBOL)
  lookback = int(request.args.get("lookback", strategy_cfg["lookback"]))
  _ensure_buffers(symbol)
  buf = list(candles[symbol])
  if len(buf) < lookback + 1:
    return jsonify(ok=False, reason="insufficient candles", symbol=symbol, lookback=lookback)
  rng = buf[-lookback:]
  hh = max(c.h for c in rng[:-1])  # exclude current candle
  ll = min(c.l for c in rng[:-1])
  cur = buf[-1]
  return jsonify(ok=True, symbol=symbol, lookback=lookback, hh=hh, ll=ll, last_close=cur.c)


# --- Combined summary route ---
@app.route("/api/signal/summary", methods=["POST"])
def api_signal_summary():
  data = request.get_json(silent=True) or {}
  symbol = data.get("symbol", DEFAULT_SYMBOL)
  window = int(data.get("window", 20))
  multiplier = float(data.get("multiplier", 2.5))
  lookback = int(data.get("lookback", 20))
  sp = compute_spikes(symbol, window=window, multiplier=multiplier, limit=10)
  sig = breakout_signal(symbol, lookback=lookback, window=window, multiplier=multiplier)
  return jsonify(symbol=symbol, spikes=sp, breakout=sig)


# --- Position suggestion route ---
@app.route("/api/position/suggest", methods=["POST"])
def api_position_suggest():
  data = request.get_json(silent=True) or {}
  symbol = data.get("symbol", DEFAULT_SYMBOL)
  window = int(data.get("window", strategy_cfg["window"]))
  multiplier = float(data.get("multiplier", strategy_cfg["multiplier"]))
  lookback = int(data.get("lookback", strategy_cfg["lookback"]))

  # Reuse breakout_signal to compute a candidate suggestion
  sig = breakout_signal(symbol, lookback=lookback, window=window, multiplier=multiplier)
  if not sig.get("hasSignal"):
    # Return levels even if no immediate signal
    _ensure_buffers(symbol)
    buf = list(candles[symbol])
    if len(buf) < lookback + 1:
      return jsonify(ok=False, reason="insufficient candles for suggestion", symbol=symbol)
    rng = buf[-lookback:]
    hh = max(c.h for c in rng[:-1])
    ll = min(c.l for c in rng[:-1])
    return jsonify(ok=False, reason=sig.get("reason","no signal"), symbol=symbol, hh=hh, ll=ll, last_close=buf[-1].c)

  return jsonify(ok=True, symbol=symbol, suggestion={
    "direction": sig["direction"],
    "entry": sig["entry"],
    "sl": sig["sl"],
    "tp": sig["tp"],
    "confidence": sig.get("confidence", 0.6),
    "window": window,
    "multiplier": multiplier,
    "lookback": lookback
  })


# --- Simple backtest route ---
@app.route("/api/backtest", methods=["POST"])
def api_backtest():
  """
  Backtest the current breakout+spike logic over the in-memory candles.
  For each candle i, we:
    - compute HH/LL over prior lookback (exclude current),
    - check last spike within window at i,
    - if breakout and spike ratio >= multiplier, enter at close_i,
    - resolve over the next N bars (default 10): first touch of TP or SL wins.
  """
  data = request.get_json(silent=True) or {}
  symbol = data.get("symbol", DEFAULT_SYMBOL)
  window = int(data.get("window", strategy_cfg["window"]))
  multiplier = float(data.get("multiplier", strategy_cfg["multiplier"]))
  lookback = int(data.get("lookback", strategy_cfg["lookback"]))
  resolve_bars = int(data.get("resolve_bars", 10))  # how many candles to look ahead
  max_trades = int(data.get("max_trades", 200))     # cap number of trades evaluated (most recent)
  tie_breaker = str(data.get("tie_breaker", "sl_wins")).lower()  # "sl_wins" | "tp_wins"
  fee_bps = float(data.get("fee_bps", 0.0))  # round-trip fee in basis points, e.g., 10 = 0.10%
  slippage = float(data.get("slippage", 0.0))  # absolute price slippage per fill

  _ensure_buffers(symbol)
  buf = list(candles[symbol])
  n = len(buf)
  if n < max(lookback + 1, window + 1, resolve_bars + 2):
    return jsonify(ok=False, reason="insufficient candles", available=n)

  # helper: compute spike on a slice ending at idx i (inclusive)
  def last_spike_ratio(up_to_idx: int):
    start = max(0, up_to_idx - (window))
    prev_start = max(0, up_to_idx - (window * 2))
    prev = buf[prev_start:up_to_idx - window + 1] if (up_to_idx - window) - prev_start + 1 > 0 else []
    if len(prev) < window:
      prev = buf[start:up_to_idx]  # fallback to immediate window
    if len(prev) == 0:
      return 0.0
    avg_v = sum(c.v for c in prev) / float(len(prev))
    cur = buf[up_to_idx]
    return (cur.v / avg_v) if avg_v > 0 else 0.0

  trades = []
  # iterate over recent region only
  i_start = max(lookback + 1, window + 1)
  i_end = n - resolve_bars - 1
  if i_end <= i_start:
    return jsonify(ok=False, reason="not enough forward bars to resolve")

  indices = list(range(i_start, i_end))
  if len(indices) > max_trades:
    indices = indices[-max_trades:]

  for i in indices:
    prior = buf[i - lookback:i]
    if len(prior) < lookback:
      continue
    hh = max(c.h for c in prior[:-1])
    ll = min(c.l for c in prior[:-1])
    cur = buf[i]
    ratio = last_spike_ratio(i)

    direction = None
    if cur.c > hh and ratio >= multiplier:
      direction = "long"
    elif cur.c < ll and ratio >= multiplier:
      direction = "short"

    if not direction:
      continue

    entry = cur.c
    # ATR-lite using candle range
    rng = cur.h - cur.l
    if direction == "long":
      sl = max(ll, entry - rng)
      risk = entry - sl
      tp = entry + 1.5 * risk
    else:
      sl = min(hh, entry + rng)
      risk = sl - entry
      tp = entry - 1.5 * risk

    outcome = None
    bars_used = 0
    # walk forward resolve_bars candles to see which is hit first
    for j in range(i + 1, min(n, i + 1 + resolve_bars)):
      bars_used += 1
      hi = buf[j].h
      lo = buf[j].l
      if direction == "long":
        hit_tp = hi >= tp
        hit_sl = lo <= sl
      else:
        hit_tp = lo <= tp
        hit_sl = hi >= sl
      if hit_tp and hit_sl:
        if tie_breaker == "tp_wins":
          outcome = "win"
        else:
          outcome = "loss"
        break
      if hit_tp:
        outcome = "win"
        break
      if hit_sl:
        outcome = "loss"
        break
    if outcome is None:
      outcome = "open"

    # Adjust entry/exit for slippage and fees
    exec_entry = entry + (slippage if direction == "long" else -slippage)
    if outcome == "win":
      exec_exit = tp - (slippage if direction == "long" else -slippage)
    elif outcome == "loss":
      exec_exit = sl - (slippage if direction == "long" else -slippage)
    else:
      exec_exit = buf[i + bars_used].c if bars_used > 0 else entry
    notional_entry = exec_entry
    notional_exit = exec_exit
    fee_mult = (1.0 - fee_bps / 10000.0)
    if direction == "long":
      unit_pnl = (notional_exit - notional_entry) * fee_mult
    else:
      unit_pnl = (notional_entry - notional_exit) * fee_mult

    trades.append({
      "i": i,
      "t": cur.t,
      "direction": direction,
      "entry": round(entry, 8),
      "sl": round(sl, 8),
      "tp": round(tp, 8),
      "spike_ratio": round(ratio, 3),
      "hh": hh,
      "ll": ll,
      "bars_to_resolve": bars_used,
      "outcome": outcome,
      "exec_entry": round(exec_entry, 8),
      "exec_exit": round(exec_exit, 8),
      "unit_pnl": round(unit_pnl, 8),
      "tie_breaker": tie_breaker,
      "fee_bps": fee_bps,
      "slippage": slippage,
    })

  # aggregate stats
  wins = sum(1 for t in trades if t["outcome"] == "win")
  losses = sum(1 for t in trades if t["outcome"] == "loss")
  opens = sum(1 for t in trades if t["outcome"] == "open")
  total = len(trades)
  win_rate = (wins / total) if total else 0.0
  avg_win = (sum(t["unit_pnl"] for t in trades if t["outcome"]=="win") / wins) if wins else 0.0
  avg_loss = (sum(t["unit_pnl"] for t in trades if t["outcome"]=="loss") / losses) if losses else 0.0
  expectancy = (wins/total)*avg_win + (losses/total)*avg_loss if total else 0.0

  return jsonify(ok=True, symbol=symbol, params={
      "window": window, "multiplier": multiplier, "lookback": lookback,
      "resolve_bars": resolve_bars, "max_trades": max_trades
    },
    summary={"total": total, "wins": wins, "losses": losses, "open": opens, "win_rate": round(win_rate, 4),
             "avg_win": round(avg_win, 8), "avg_loss": round(avg_loss, 8), "expectancy": round(expectancy, 8)},
    trades=trades
  )

# --- Parameter sweep over grid of (window, multiplier, lookback) ---
@app.route("/api/backtest/grid", methods=["POST"])
def api_backtest_grid():
  """
  Run a grid search over window, multiplier, lookback.
  Body can contain explicit arrays or ranges:
    {"symbol":"BTCUSDT",
     "window":[5,10,20], "multiplier":[1.3,1.6,2.0], "lookback":[10,20],
     "resolve_bars":10, "max_trades":200, "tie_breaker":"sl_wins", "fee_bps":10, "slippage":0.0}
  Or ranges:
    {"window":{"start":5,"stop":20,"step":5}, ...}
  Returns top 10 by expectancy then win_rate.
  """
  data = request.get_json(silent=True) or {}
  symbol = data.get("symbol", DEFAULT_SYMBOL)
  def _expand(v, default):
    if isinstance(v, dict):
      start, stop, step = int(v.get("start", 0)), int(v.get("stop", 0)), int(v.get("step", 1))
      return list(range(start, stop+1, max(1,step)))
    if isinstance(v, list):
      return v
    return default
  windows = _expand(data.get("window"), [strategy_cfg["window"]])
  mults = _expand(data.get("multiplier"), [strategy_cfg["multiplier"]])
  looks = _expand(data.get("lookback"), [strategy_cfg["lookback"]])
  resolve_bars = int(data.get("resolve_bars", 10))
  max_trades = int(data.get("max_trades", 200))
  tie_breaker = str(data.get("tie_breaker", "sl_wins")).lower()
  fee_bps = float(data.get("fee_bps", 0.0))
  slippage = float(data.get("slippage", 0.0))

  results = []
  for w in windows:
    for m in mults:
      for l in looks:
        req = {"symbol": symbol, "window": int(w), "multiplier": float(m), "lookback": int(l),
               "resolve_bars": resolve_bars, "max_trades": max_trades,
               "tie_breaker": tie_breaker, "fee_bps": fee_bps, "slippage": slippage}
        with app.test_request_context(json=req):
          res = api_backtest()
          js = res.get_json()
        if not js or not js.get("ok"):
          continue
        summ = js["summary"]
        results.append({
          "window": int(w), "multiplier": float(m), "lookback": int(l),
          "total": summ["total"], "wins": summ["wins"], "losses": summ["losses"],
          "win_rate": summ["win_rate"], "expectancy": summ.get("expectancy", 0.0),
          "avg_win": summ.get("avg_win", 0.0), "avg_loss": summ.get("avg_loss", 0.0)
        })
  # sort by expectancy then win_rate then total trades
  results.sort(key=lambda r: (r["expectancy"], r["win_rate"], r["total"]), reverse=True)
  top = results[:10]
  return jsonify(ok=True, symbol=symbol, tried=len(results), top=top)
# --- Portfolio/positions analytics (PnL, expectancy, drawdown, streaks) ---
@app.route("/api/metrics")
def api_metrics():
  # Build equity curve from closed positions by created_at order
  with position_lock:
    closed = sorted([p for p in positions.values() if p.get("status")=="closed"], key=lambda x: x["closed_at"] or x["created_at"])
  eq = [0.0]
  max_eq = 0.0
  dd = 0.0
  max_dd = 0.0
  wins = losses = 0
  win_streak = loss_streak = 0
  max_win_streak = max_loss_streak = 0
  pnl_list = []
  for p in closed:
    pnl = float(p.get("realized_pnl", 0.0))
    pnl_list.append(pnl)
    eq.append(eq[-1] + pnl)
    max_eq = max(max_eq, eq[-1])
    dd = max_eq - eq[-1]
    max_dd = max(max_dd, dd)
    if pnl > 0:
      wins += 1
      win_streak += 1
      loss_streak = 0
      max_win_streak = max(max_win_streak, win_streak)
    elif pnl < 0:
      losses += 1
      loss_streak += 1
      win_streak = 0
      max_loss_streak = max(max_loss_streak, loss_streak)
    else:
      # flat pnl
      win_streak = 0
      loss_streak = 0
  total = len(closed)
  avg_win = (sum(p for p in pnl_list if p > 0)/wins) if wins else 0.0
  avg_loss = (sum(p for p in pnl_list if p < 0)/losses) if losses else 0.0
  win_rate = (wins/total) if total else 0.0
  expectancy = (wins/total)*avg_win + (losses/total)*avg_loss if total else 0.0
  return jsonify(ok=True, summary={
    "trades": total,
    "wins": wins, "losses": losses, "win_rate": round(win_rate, 4),
    "avg_win": round(avg_win, 8), "avg_loss": round(avg_loss, 8),
    "expectancy": round(expectancy, 8),
    "max_drawdown": round(max_dd, 8),
    "max_win_streak": max_win_streak, "max_loss_streak": max_loss_streak,
    "final_equity": round(eq[-1], 8)
  })

@app.route("/api/ws/update", methods=["POST"])
def api_ws_update():
  data = request.get_json(silent=True) or {}
  symbol = data.get("symbol", DEFAULT_SYMBOL)
  interval = data.get("interval", DEFAULT_INTERVAL)
  with state_lock:
    ws_status["symbol"] = symbol
    ws_status["interval"] = interval
  return jsonify(ok=True, message="WS config updated. Will apply on next reconnect.", symbol=symbol, interval=interval)

@app.route("/api/status/clear_error", methods=["POST"])
def api_clear_error():
  with state_lock:
    ws_status["last_error"] = None
  return jsonify(ok=True, message="Last error cleared.")

# --- Simulated orders (paper) ---

@app.route("/api/order/market", methods=["POST"])
def api_order_market():
  data = request.get_json(silent=True) or {}
  symbol = data.get("symbol", DEFAULT_SYMBOL)
  side = str(data.get("side", "buy")).lower()
  qty = float(data.get("qty", 1))
  order_id = f"SIM-MKT-{_now_ms()}"
  order = {
    "id": order_id,
    "type": "market",
    "symbol": symbol,
    "side": "buy" if side != "sell" else "sell",
    "qty": qty,
    "price": None,
    "status": "filled",  # simulate immediate fill
    "created_at": _now_ms(),
    "updated_at": _now_ms(),
    "cancel_after": None
  }
  with order_lock:
    paper_orders[order_id] = order
  msg = f"[PAPER] Market {order['side'].upper()} {qty} {symbol} placed and filled."
  return jsonify(ok=True, order_id=order_id, order=order, message=msg)


@app.route("/api/order/limit", methods=["POST"])
def api_order_limit():
  data = request.get_json(silent=True) or {}
  symbol = data.get("symbol", DEFAULT_SYMBOL)
  price = float(data.get("price", 0))
  qty = float(data.get("qty", 1))
  side = str(data.get("side", "buy")).lower()
  cancel_after = int(data.get("cancel_after", 15))

  order_id = f"SIM-LMT-{_now_ms()}"
  order = {
    "id": order_id,
    "type": "limit",
    "symbol": symbol,
    "side": "buy" if side != "sell" else "sell",
    "qty": qty,
    "price": price,
    "status": "open",
    "created_at": _now_ms(),
    "updated_at": _now_ms(),
    "cancel_after": cancel_after
  }
  with order_lock:
    paper_orders[order_id] = order

  def cancel_later(oid, secs):
    time.sleep(max(1, secs))
    with order_lock:
      o = paper_orders.get(oid)
      if o and o.get("status") == "open":
        o["status"] = "canceled"
        o["updated_at"] = _now_ms()
    print(f"[AUTO-CANCEL] Order {oid} cancelled (paper).")

  threading.Thread(target=cancel_later, args=(order_id, cancel_after), daemon=True).start()
  side_txt = "BUY" if side != "sell" else "SELL"
  msg = f"[PAPER] Limit {side_txt} {qty} {symbol} @ {price}. Auto-cancel in {cancel_after}s."
  return jsonify(ok=True, order_id=order_id, order=order, message=msg)


# --- Order management endpoints ---
@app.route("/api/orders")
def api_orders():
  status = request.args.get("status")  # optional: open/filled/canceled
  with order_lock:
    values = list(paper_orders.values())
  if status:
    values = [o for o in values if o.get("status") == status]
  return jsonify(ok=True, orders=values, count=len(values))

@app.route("/api/order/cancel", methods=["POST"])
def api_order_cancel():
  data = request.get_json(silent=True) or {}
  oid = data.get("order_id")
  if not oid:
    return jsonify(ok=False, message="order_id required"), 400
  with order_lock:
    o = paper_orders.get(oid)
    if not o:
      return jsonify(ok=False, message="order not found"), 404
    if o.get("status") != "open":
      return jsonify(ok=False, message=f"order status is {o.get('status')} (not open)"), 409
    o["status"] = "canceled"
    o["updated_at"] = _now_ms()
  return jsonify(ok=True, message=f"Order {oid} canceled.", order=paper_orders.get(oid))


# --- Config endpoints for strategy params ---
@app.route("/api/config", methods=["GET"])
def api_config_get():
  return jsonify(ok=True, strategy=strategy_cfg)

@app.route("/api/config", methods=["POST"])
def api_config_set():
  data = request.get_json(silent=True) or {}
  # Only update known keys
  for k in ("window", "multiplier", "lookback"):
    if k in data:
      try:
        if k == "multiplier":
          strategy_cfg[k] = float(data[k])
        else:
          strategy_cfg[k] = int(data[k])
      except Exception:
        pass
  return jsonify(ok=True, strategy=strategy_cfg)

# --- Backtest report endpoint (CSV/JSON) ---
@app.route("/api/backtest/report", methods=["POST"])
def api_backtest_report():
  fmt = request.args.get("format", "json")
  # Run backtest using posted body
  res = api_backtest()
  # api_backtest returns a Response; get JSON if possible
  if hasattr(res, "json"):
    data = res.json
  else:
    # When called internally, Flask may return a tuple; handle common cases
    try:
      data = res.get_json()
    except Exception:
      return res
  if not data or not data.get("ok"):
    return res
  if fmt == "csv":
    import io, csv
    sio = io.StringIO()
    w = csv.DictWriter(sio, fieldnames=list(data["trades"][0].keys()) if data["trades"] else ["i"])
    w.writeheader()
    for t in data["trades"]:
      w.writerow(t)
    csv_bytes = sio.getvalue()
    return app.response_class(csv_bytes, mimetype="text/csv")
  return res

if __name__ == "__main__":
  print(f"Websocket available: {WEBSOCKET_AVAILABLE}. Connecting to {PUBLIC_WS_URL} for {DEFAULT_SYMBOL}/{DEFAULT_INTERVAL}...")
  HOST = os.getenv("HOST", "127.0.0.1")
  PORT = int(os.getenv("PORT", "5000"))
  app.run(host=HOST, port=PORT, debug=True)
# --- Strategy presets endpoints ---
PRESETS = {
  "aggressive": {"window": 5, "multiplier": 1.3, "lookback": 10},
  "balanced": {"window": 10, "multiplier": 1.6, "lookback": 20},
  "conservative": {"window": 20, "multiplier": 2.0, "lookback": 30},
}

@app.route("/api/strategy/presets", methods=["GET"])
def api_strategy_presets_get():
  return jsonify(ok=True, presets=PRESETS, current=strategy_cfg)

@app.route("/api/strategy/presets", methods=["POST"])
def api_strategy_presets_post():
  data = request.get_json(silent=True) or {}
  name = str(data.get("name","")).lower()
  if name not in PRESETS:
    return jsonify(ok=False, reason=f"unknown preset '{name}'", available=list(PRESETS.keys())), 400
  cfg = PRESETS[name]
  for k,v in cfg.items():
    strategy_cfg[k] = v
  return jsonify(ok=True, applied=name, strategy=strategy_cfg)