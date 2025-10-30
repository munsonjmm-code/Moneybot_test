#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Bitunix simple client (clean v2)
- Uses documented /api/v1/futures/trade/place_order
- Adds trading-pairs lookup for minTradeVolume, etc.
- place-percent now calls the new endpoint under the hood

Env vars required (export in your shell first):
  export BITUNIX_API_KEY="..."
  export BITUNIX_SECRET_KEY="..."
"""

import os, time, json, hashlib, secrets, argparse
from typing import Dict, Optional
import requests

BASE = "https://fapi.bitunix.com"

# ----------------------------- signing helpers -----------------------------
def sha256_hex(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()

def canonical_qp(params: Dict[str, str]) -> str:
    """Concat as key+value in ASCII key order (no '=' or '&')."""
    if not params:
        return ""
    items = sorted((k, str(v)) for k, v in params.items())
    return "".join(f"{k}{v}" for k, v in items)

def make_signature(api_key: str, secret_key: str,
                   params: Dict[str, str], body_obj: Optional[Dict]) -> Dict[str, str]:
    """
    digest = SHA256(nonce + timestamp + api-key + queryParams + body)
    sign   = SHA256(digest + secretKey)
    body is compact JSON (no spaces).
    """
    qp_str   = canonical_qp(params)
    body_str = "" if body_obj is None else json.dumps(body_obj, separators=(",", ":"))
    nonce    = secrets.token_hex(16)
    ts       = str(int(time.time() * 1000))
    digest   = sha256_hex(f"{nonce}{ts}{api_key}{qp_str}{body_str}")
    sign     = sha256_hex(f"{digest}{secret_key}")
    return {
        "api-key": api_key,
        "sign": sign,
        "nonce": nonce,
        "timestamp": ts,
        "Content-Type": "application/json",
        "language": "en-US",
    }

# ------------------------------- HTTP helpers ------------------------------
def do_get(path: str, api_key: str, secret_key: str,
           params: Dict[str, str], debug: bool = False):
    url = f"{BASE}{path}"
    headers = make_signature(api_key, secret_key, params, None)
    if debug:
        print(f"[GET🔐] {url}")
        print("qp_str:", canonical_qp(params))
        print("sign  :", headers["sign"])
    r = requests.get(url, params=params, headers=headers, timeout=20)
    if debug:
        print("STATUS:", r.status_code)
        print("PARAMS:", json.dumps(params))
        print("RESP  :", r.text)
    return r

def do_post(path: str, api_key: str, secret_key: str,
            params: Dict[str, str], body: Dict, debug: bool = False):
    url = f"{BASE}{path}"
    headers = make_signature(api_key, secret_key, params, body)
    if debug:
        print(f"[POST🔐] {url}")
        print("qp_str:", canonical_qp(params))
        print("body  :", json.dumps(body, separators=(",", ":")))
        print("sign  :", headers["sign"])
    r = requests.post(url, params=params, headers=headers,
                      data=json.dumps(body, separators=(",", ":")), timeout=20)
    if debug:
        print("STATUS:", r.status_code)
        print("RESP  :", r.text)
    return r

# ---------------------------- new helpers -----------------------------------
def check_order_status(api_key: str, secret_key: str, symbol: str, margin_coin: str, order_id: str, debug: bool = False):
    params = {"marginCoin": margin_coin, "symbol": symbol}
    if debug:
        print(f"check_order_status params: marginCoin={margin_coin}, symbol={symbol}")
    r = do_get("/api/futures/v1/trade/open_orders", api_key, secret_key, params, debug)
    try:
        try:
            j = r.json()
        except Exception as e:
            if debug:
                print(f"Failed to parse JSON from open_orders response: {e}")
                print("Raw response text:", r.text)
            return None
        data = j.get("data") or []
        if j.get("code") != 0 or not data:
            if debug:
                print(f"Nonzero code or empty data in open_orders response: {j}")
            return {"code": j.get("code", -1), "msg": j.get("msg", "Unknown error"), "data": data}
        if debug:
            print(f"Searching open_orders for orderId={order_id}")
        found_order = None
        for p in data:
            if p.get("orderId") == order_id:
                found_order = p
                break
        if found_order:
            if debug:
                print(f"Found order {order_id} in open orders.")
            return {"code": 0, "data": found_order}
        else:
            if debug:
                print(f"Order {order_id} not found in open orders.")
            return {"code": 404, "msg": "Order not found in open orders"}
    except Exception as e:
        if debug:
            print(f"Failed to parse JSON or search open orders: {e}")
            print("Raw response text:", getattr(r, 'text', '(no text)'))
        return None

def cancel_order(api_key: str, secret_key: str, margin_coin: str, order_id: str, debug: bool = False):
    params = {"marginCoin": margin_coin}
    body = {"orderId": order_id}
    r = do_post("/api/v1/futures/trade/cancel", api_key, secret_key, params, body, debug)
    try:
        return r.json()
    except Exception:
        if debug:
            print("Failed to parse JSON from cancel order response")
        return None

# --------------------------------- calls -----------------------------------
def get_account(api_key: str, secret_key: str, margin_coin: str = "USDT", debug: bool = False):
    params = {"marginCoin": margin_coin}
    return do_get("/api/v1/futures/account", api_key, secret_key, params, debug)

def get_trading_pairs(symbols: Optional[str], debug: bool = False):
    url = f"{BASE}/api/v1/futures/market/trading_pairs"
    params = {}
    if symbols:
        params["symbols"] = symbols
    r = requests.get(url, params=params, timeout=20)
    if debug:
        print(f"[GET🌐] {url}")
        print("STATUS:", r.status_code)
        print("RESP  :", r.text)
    return r

def place_order_v2(api_key: str, secret_key: str, symbol: str, side: str,
                   order_type: str, qty: float, price: Optional[float],
                   trade_side: Optional[str], margin_coin: str = "USDT",
                   debug: bool = False):
    """
    POST /api/v1/futures/trade/place_order
    Body: symbol, qty, side (BUY/SELL), orderType (LIMIT/MARKET), price (LIMIT only)
    In HEDGE mode, tradeSide is required: OPEN / CLOSE_LONG / CLOSE_SHORT
    """
    params = {"marginCoin": margin_coin, "symbol": symbol}
    body: Dict[str, object] = {
        "symbol": symbol,
        "qty": float(qty),
        "side": side.upper(),
        "orderType": order_type.upper(),
    }
    if order_type.upper() == "LIMIT":
        if price is None:
            raise ValueError("price is required for LIMIT orders")
        body["price"] = float(price)
    if trade_side:
        body["tradeSide"] = trade_side.upper()

    return do_post("/api/v1/futures/trade/place_order",
                   api_key, secret_key, params, body, debug)

# ---------------------------- CLI: sub-commands -----------------------------
def main() -> None:
    parser = argparse.ArgumentParser(description="Bitunix simple client")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_acct = sub.add_parser("account", help="Show futures account (balance, mode, etc.)")
    p_acct.add_argument("--margin-coin", default="USDT")
    p_acct.add_argument("--debug", action="store_true")

    p_pairs = sub.add_parser("trading-pairs", help="Get trading pair constraints")
    p_pairs.add_argument("--symbols", default=None,
                         help="Comma-separated symbols, e.g. DOGEUSDT,BTCUSDT")
    p_pairs.add_argument("--debug", action="store_true")

    p_place2 = sub.add_parser("place-v2", help="Place via /api/v1/futures/trade/place_order")
    p_place2.add_argument("--symbol", required=True)
    p_place2.add_argument("--side", choices=["BUY", "SELL"], required=True)
    p_place2.add_argument("--order-type", choices=["limit", "market"], required=True)
    p_place2.add_argument("--qty", type=float, required=True, help="Order quantity in base asset (qty)")
    p_place2.add_argument("--price", type=float, help="Required for limit orders")
    p_place2.add_argument("--trade-side", choices=["OPEN", "CLOSE_LONG", "CLOSE_SHORT"],
                          default="OPEN", help="Required in HEDGE mode")
    p_place2.add_argument("--margin-coin", default="USDT")
    p_place2.add_argument("--timeout", type=float, default=15.0, help="Cancel order if not filled after N seconds")
    p_place2.add_argument("--debug", action="store_true")

    p_pct = sub.add_parser("place-percent",
        help="Size by percent of balance; dry-run unless --submit is given")
    p_pct.add_argument("--symbol", required=True)
    p_pct.add_argument("--side", choices=["BUY", "SELL"], required=True)
    p_pct.add_argument("--order-type", choices=["limit", "market"], required=True)
    p_pct.add_argument("--price", type=float, help="Price for limit orders (required if order-type=limit)")
    p_pct.add_argument("--percent", type=float, required=True,
                       help="e.g., 10 means 10 percent of available balance")
    p_pct.add_argument("--anchor-price", type=float, required=True,
                       help="Anchor price to convert $ to qty")
    p_pct.add_argument("--trade-side", choices=["OPEN", "CLOSE_LONG", "CLOSE_SHORT"],
                       default="OPEN")
    p_pct.add_argument("--margin-coin", default="USDT")
    p_pct.add_argument("--timeout", type=float, default=15.0, help="Cancel order if not filled after N seconds")
    p_pct.add_argument("--submit", action="store_true", help="Actually send the order")
    p_pct.add_argument("--debug", action="store_true")

    args = parser.parse_args()

    # keys
    api_key = os.getenv("BITUNIX_API_KEY", "").strip()
    secret  = os.getenv("BITUNIX_SECRET_KEY", "").strip()
    if args.cmd in {"account", "place-v2", "place-percent"}:
        if not api_key or not secret:
            raise SystemExit("Missing BITUNIX_API_KEY / BITUNIX_SECRET_KEY in environment")

    if args.cmd == "account":
        r = get_account(api_key, secret, args.margin_coin, args.debug)
        print(r.text)
        return

    if args.cmd == "trading-pairs":
        r = get_trading_pairs(args.symbols, args.debug)
        print(r.text)
        return

    if args.cmd == "place-v2":
        r = place_order_v2(api_key, secret,
                           symbol=args.symbol,
                           side=args.side,
                           order_type=args.order_type,
                           qty=args.qty,
                           price=args.price,
                           trade_side=args.trade_side,
                           margin_coin=args.margin_coin,
                           debug=args.debug)
        print(r.text)

        # handle timeout/cancel for limit orders
        if args.timeout > 0 and args.order_type.lower() == "limit":
            try:
                j = r.json()
                order_id = j.get("data", {}).get("orderId")
                if not order_id:
                    if args.debug:
                        print("No orderId returned, cannot check order status or cancel")
                else:
                    if args.debug:
                        print(f"Waiting {args.timeout} seconds before checking order status...")
                    time.sleep(args.timeout)
                    status_resp = check_order_status(api_key, secret, args.margin_coin, order_id, args.debug)
                    if status_resp and status_resp.get("code") == 0:
                        order_data = status_resp.get("data", {})
                        status = order_data.get("status")
                        if args.debug:
                            print(f"Order status after wait: {status}")
                        # Assuming status codes: 2 = filled/closed, others are open or partially filled
                        if status not in [2, "2"]:
                            if args.debug:
                                print(f"Order {order_id} not filled after {args.timeout} seconds, cancelling...")
                            cancel_resp = cancel_order(api_key, secret, args.margin_coin, order_id, args.debug)
                            print(f"Order cancelled: {cancel_resp}")
                        else:
                            if args.debug:
                                print(f"Order {order_id} filled within timeout.")
                    else:
                        if args.debug:
                            print("Failed to get order status or order not found.")
            except Exception as e:
                if args.debug:
                    print(f"Exception during order status check/cancel: {e}")
        return

    if args.cmd == "place-percent":
        # 1) get available balance
        acct = get_account(api_key, secret, args.margin_coin, args.debug)
        j = acct.json()
        if not j or j.get("code") != 0:
            print("Could not fetch account:", acct.text)
            raise SystemExit(1)
        avail = float(j["data"].get("available", 0.0))

        # 2) convert percent of balance (quote) -> qty (base)
        dollars = avail * (args.percent / 100.0)

        # Fetch trading pair info for minTradeVolume
        try:
            tp = get_trading_pairs(args.symbol, args.debug)
            tp_json = tp.json()
            if tp_json.get("code") == 0 and tp_json.get("data"):
                pair_info = next((p for p in tp_json["data"] if p.get("symbol") == args.symbol), None)
                if pair_info:
                    min_qty = float(pair_info.get("minTradeVolume", 0.0))
                    print(f"Exchange minTradeVolume for {args.symbol}: {min_qty}")
                    if dollars / args.anchor_price < min_qty:
                        print(f"Adjusted qty from {dollars / args.anchor_price:.6f} to minTradeVolume {min_qty}")
                        dollars = min_qty * args.anchor_price
        except Exception as e:
            print(f"Warning: could not fetch trading pair info: {e}")

        if args.anchor_price <= 0:
            raise SystemExit("--anchor-price must be > 0")
        qty = dollars / args.anchor_price
        print(f"Calculated qty ~ {qty:.6f} (from {args.percent}% of ${avail:.6f} at anchor {args.anchor_price})")

        if not args.submit:
            print("Dry-run only. Add --submit to actually send the order.")
            return

        # 3) real order
        r = place_order_v2(api_key, secret,
                           symbol=args.symbol,
                           side=args.side,
                           order_type=args.order_type,
                           qty=qty,
                           price=args.price,
                           trade_side=args.trade_side,
                           margin_coin=args.margin_coin,
                           debug=args.debug)
        print(r.text)

        # handle timeout/cancel for limit orders
        if args.timeout > 0 and args.order_type.lower() == "limit":
            try:
                j = r.json()
                order_id = j.get("data", {}).get("orderId")
                if not order_id:
                    if args.debug:
                        print("No orderId returned, cannot check order status or cancel")
                else:
                    if args.debug:
                        print(f"Waiting {args.timeout} seconds before checking order status...")
                    time.sleep(args.timeout)
                    status_resp = check_order_status(api_key, secret, args.margin_coin, order_id, args.debug)
                    if status_resp and status_resp.get("code") == 0:
                        order_data = status_resp.get("data", {})
                        status = order_data.get("status")
                        if args.debug:
                            print(f"Order status after wait: {status}")
                        # Assuming status codes: 2 = filled/closed, others are open or partially filled
                        if status not in [2, "2"]:
                            if args.debug:
                                print(f"Order {order_id} not filled after {args.timeout} seconds, cancelling...")
                            cancel_resp = cancel_order(api_key, secret, args.margin_coin, order_id, args.debug)
                            print(f"Order cancelled: {cancel_resp}")
                        else:
                            if args.debug:
                                print(f"Order {order_id} filled within timeout.")
                    else:
                        if args.debug:
                            print("Failed to get order status or order not found.")
            except Exception as e:
                if args.debug:
                    print(f"Exception during order status check/cancel: {e}")
        return

if __name__ == "__main__":
    main()