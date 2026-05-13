"""
V2 BitgetClient — Kopie aus V1, angepasst für apex-v2 Pfadstruktur.
Credential-Pfad: /root/apex-v2/config/.env
Keine V1-Imports.
"""

import os
import json
import time
import hmac
import random
import hashlib
import base64
import requests
from typing import Optional, Dict, List
from dataclasses import dataclass
from core.utils import log

BASE_URL     = "https://api.bitget.com"
PRODUCT_TYPE = "USDT-FUTURES"
MARGIN_COIN  = "USDT"

INTERVAL_MAP = {
    "1m": "1m", "3m": "3m", "5m": "5m", "15m": "15m", "30m": "30m",
    "1h": "1H", "2h": "2H", "4h": "4H", "6h": "6H", "12h": "12H",
    "1d": "1D",
}

CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "config")

SIZE_DECIMALS_FALLBACK = {"BTC": 4, "ETH": 2, "SOL": 1, "AVAX": 1, "XRP": 0,
                          "DOGE": 0, "ADA": 0, "SUI": 1, "AAVE": 2}


@dataclass
class OrderResult:
    success: bool
    order_id: Optional[str] = None
    filled_size: float = 0.0
    avg_price: float = 0.0
    error: Optional[str] = None


@dataclass
class Position:
    coin: str
    size: float
    entry_price: float
    unrealized_pnl: float
    leverage: float
    liquidation_price: float
    mark_price: float = 0.0    # aktueller Mark-Preis für BE-Check


class BitgetClient:
    _MIN_INTERVAL   = 0.2
    _last_request_time: float = 0.0
    _contract_cache: dict = {}   # {symbol: {"min_size": float, "size_precision": int}}

    def __init__(self, dry_run: bool = True):
        self.dry_run    = dry_run
        self.api_key    = None
        self.secret_key = None
        self.passphrase = None
        self._load_credentials()

    def _load_credentials(self):
        env_file = os.path.join(CONFIG_DIR, ".env")
        if not os.path.exists(env_file):
            return
        with open(env_file) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                if key == "BITGET_API_KEY":
                    self.api_key = value
                elif key == "BITGET_SECRET_KEY":
                    self.secret_key = value
                elif key == "BITGET_PASSPHRASE":
                    self.passphrase = value

    @property
    def is_ready(self) -> bool:
        return all([self.api_key, self.secret_key, self.passphrase])

    def _symbol(self, coin: str) -> str:
        return f"{coin.upper()}USDT"

    def _sign(self, timestamp: str, method: str, path: str, body: str = "") -> str:
        prehash = timestamp + method.upper() + path + body
        return base64.b64encode(
            hmac.new(self.secret_key.encode(), prehash.encode(), hashlib.sha256).digest()
        ).decode()

    def _auth_headers(self, method: str, path: str, body: str = "") -> Dict:
        ts = str(int(time.time() * 1000))
        h = {
            "ACCESS-KEY":       self.api_key,
            "ACCESS-SIGN":      self._sign(ts, method, path, body),
            "ACCESS-TIMESTAMP": ts,
            "ACCESS-PASSPHRASE": self.passphrase,
            "locale":           "en-US",
        }
        if method.upper() == "POST":
            h["Content-Type"] = "application/json"
        return h

    def _request_with_retry(self, method: str, url: str, **kwargs) -> requests.Response:
        now = time.monotonic()
        wait = BitgetClient._MIN_INTERVAL - (now - BitgetClient._last_request_time)
        if wait > 0:
            time.sleep(wait)
        BitgetClient._last_request_time = time.monotonic()

        for attempt, base_delay in enumerate([5, 15, 30, 60], 1):
            resp = requests.request(method, url, **kwargs)
            if resp.status_code != 429:
                return resp
            jitter = random.uniform(0, 5)
            time.sleep(base_delay + jitter)
        raise RuntimeError("Bitget API Rate Limit nach 4 Versuchen nicht aufgelöst")

    def _get(self, path: str, params: Dict = None, auth: bool = False):
        query = ""
        if params:
            query = "?" + "&".join(f"{k}={v}" for k, v in params.items())
        full_path = path + query
        headers = self._auth_headers("GET", full_path) if auth else {"locale": "en-US"}
        resp = self._request_with_retry("GET", BASE_URL + full_path, headers=headers, timeout=10)
        if not resp.ok:
            raise Exception(f"HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if data.get("code") != "00000":
            raise Exception(f"Bitget [{data.get('code')}]: {data.get('msg')}")
        return data.get("data")

    def _post(self, path: str, body: Dict):
        if not self.is_ready:
            raise Exception("API-Credentials fehlen")
        body_str = json.dumps(body)
        headers = self._auth_headers("POST", path, body_str)
        resp = self._request_with_retry("POST", BASE_URL + path, data=body_str, headers=headers, timeout=10)
        if not resp.ok:
            raise Exception(f"HTTP {resp.status_code}: {resp.text[:300]}")
        data = resp.json()
        if data.get("code") != "00000":
            raise Exception(f"Bitget [{data.get('code')}]: {data.get('msg')}")
        return data.get("data")

    # ── Marktdaten ────────────────────────────────────────────────────────────

    def get_candles(self, coin: str, interval: str = "15m", limit: int = 100,
                    start_time: int = None, end_time: int = None) -> List[Dict]:
        """OHLCV Kerzen, älteste zuerst. start_time/end_time: ms-Timestamps."""
        bg_interval = INTERVAL_MAP.get(interval, interval)
        params = {
            "symbol":      self._symbol(coin),
            "productType": PRODUCT_TYPE,
            "granularity": bg_interval,
            "limit":       str(limit),
        }
        if start_time is not None:
            params["startTime"] = str(int(start_time))
        if end_time is not None:
            params["endTime"] = str(int(end_time))

        data = self._get("/api/v2/mix/market/candles", params)
        candles = []
        for c in (data if isinstance(data, list) else []):
            candles.append({
                "time":   int(c[0]),
                "open":   float(c[1]),
                "high":   float(c[2]),
                "low":    float(c[3]),
                "close":  float(c[4]),
                "volume": float(c[5]),
            })
        candles.sort(key=lambda x: x["time"])
        return candles

    def get_price(self, coin: str) -> float:
        data = self._get("/api/v2/mix/market/ticker", {
            "symbol": self._symbol(coin), "productType": PRODUCT_TYPE,
        })
        items = data if isinstance(data, list) else [data]
        if not items:
            return 0.0
        item = items[0]
        return float(item.get("markPrice") or item.get("lastPr") or 0)

    def get_positions(self) -> List[Position]:
        if not self.is_ready:
            return []
        try:
            data = self._get("/api/v2/mix/position/all-position", {
                "productType": PRODUCT_TYPE, "marginCoin": MARGIN_COIN,
            }, auth=True)
            positions = []
            for pos in (data if isinstance(data, list) else []):
                size = float(pos.get("total", 0))
                if size == 0:
                    continue
                hold_side   = pos.get("holdSide", "long")
                signed_size = size if hold_side == "long" else -size
                symbol = pos.get("symbol", "")
                coin   = symbol.replace("USDT", "")
                positions.append(Position(
                    coin=coin, size=signed_size,
                    entry_price=float(pos.get("openPriceAvg", 0)),
                    unrealized_pnl=float(pos.get("unrealizedPL", 0)),
                    leverage=float(pos.get("leverage", 1)),
                    liquidation_price=float(pos.get("liquidationPrice") or 0),
                    mark_price=float(pos.get("markPrice") or pos.get("openPriceAvg", 0)),
                ))
            return positions
        except Exception as e:
            print(f"Positions-Fehler: {e}")
            return []

    def get_balance(self) -> float:
        if not self.is_ready:
            return 0.0
        try:
            data = self._get("/api/v2/mix/account/accounts", {
                "productType": PRODUCT_TYPE,
            }, auth=True)
            for acc in (data if isinstance(data, list) else []):
                if acc.get("marginCoin") == "USDT":
                    val = acc.get("equity") or acc.get("usdtEquity") or acc.get("available", 0)
                    return float(val)
        except Exception as e:
            print(f"Balance-Fehler: {e}")
        return 0.0

    def place_market_order(self, coin: str, is_buy: bool, size: float,
                           reduce_only: bool = False,
                           stop_loss: Optional[float] = None,
                           take_profit: Optional[float] = None,
                           client_order_id: Optional[str] = None) -> OrderResult:
        side       = "buy" if is_buy else "sell"
        trade_side = "close" if reduce_only else "open"

        if self.dry_run:
            price = self.get_price(coin)
            return OrderResult(
                success=True,
                order_id=client_order_id or f"DRY-{int(time.time())}",
                filled_size=size, avg_price=price,
            )

        from config.settings import PRICE_DECIMALS, SIZE_DECIMALS, MARGIN_MODE
        p_dec = PRICE_DECIMALS.get(coin, 4)
        s_dec = SIZE_DECIMALS.get(coin, 2)

        body = {
            "symbol": self._symbol(coin), "productType": PRODUCT_TYPE,
            "marginMode": MARGIN_MODE, "marginCoin": MARGIN_COIN,
            "size": f"{round(size, s_dec):.{s_dec}f}",
            "side": side, "tradeSide": trade_side,
            "orderType": "market", "force": "ioc",
        }
        # Deterministische clOrdId VOR dem API-Call setzen (Idempotenz)
        if client_order_id:
            body["clientOid"] = client_order_id
        if stop_loss:
            body["presetStopLossPrice"] = f"{round(stop_loss, p_dec):.{p_dec}f}"
            body["presetStopLossTriggerType"] = "mark_price"
        if take_profit:
            body["presetStopSurplusPrice"] = f"{round(take_profit, p_dec):.{p_dec}f}"
            body["presetStopSurplusTriggerType"] = "mark_price"
        try:
            result = self._post("/api/v2/mix/order/place-order", body)
            order_id = result.get("orderId", "") if isinstance(result, dict) else ""
            # Fallback: wenn Exchange keine orderId zurückgibt, clOrdId nutzen
            if not order_id and client_order_id:
                order_id = client_order_id
            time.sleep(1.0)
            fill_price = self.get_price(coin)
            return OrderResult(success=True, order_id=order_id, filled_size=size, avg_price=fill_price)
        except Exception as e:
            return OrderResult(success=False, error=str(e))

    def place_stop_loss(self, coin: str, trigger_price: float, size: float,
                        hold_side: str = "long") -> OrderResult:
        if self.dry_run:
            return OrderResult(success=True, avg_price=trigger_price)
        from config.settings import PRICE_DECIMALS, SIZE_DECIMALS
        p_dec = PRICE_DECIMALS.get(coin, 4)
        s_dec = SIZE_DECIMALS.get(coin, 2)
        try:
            self._post("/api/v2/mix/order/place-tpsl-order", {
                "symbol": self._symbol(coin), "productType": PRODUCT_TYPE,
                "marginCoin": MARGIN_COIN, "planType": "loss_plan",
                "triggerPrice": f"{round(trigger_price, p_dec):.{p_dec}f}",
                "triggerType": "mark_price", "executePrice": "0",
                "holdSide": hold_side,
                "size": f"{round(size, s_dec):.{s_dec}f}",
            })
            return OrderResult(success=True, avg_price=trigger_price)
        except Exception as e:
            return OrderResult(success=False, error=str(e))

    def place_take_profit(self, coin: str, trigger_price: float, size: float,
                          hold_side: str = "long") -> OrderResult:
        if self.dry_run:
            return OrderResult(success=True, avg_price=trigger_price)
        from config.settings import PRICE_DECIMALS, SIZE_DECIMALS
        p_dec = PRICE_DECIMALS.get(coin, 4)
        s_dec = SIZE_DECIMALS.get(coin, 2)
        try:
            self._post("/api/v2/mix/order/place-tpsl-order", {
                "symbol": self._symbol(coin), "productType": PRODUCT_TYPE,
                "marginCoin": MARGIN_COIN, "planType": "profit_plan",
                "triggerPrice": f"{round(trigger_price, p_dec):.{p_dec}f}",
                "triggerType": "mark_price", "executePrice": "0",
                "holdSide": hold_side,
                "size": f"{round(size, s_dec):.{s_dec}f}",
            })
            return OrderResult(success=True, avg_price=trigger_price)
        except Exception as e:
            return OrderResult(success=False, error=str(e))

    def get_contract_info(self, coin: str) -> dict:
        """
        Lädt Kontrakt-Limits für ein Asset (kein Auth nötig).
        Ergebnis wird für die Laufzeit gecacht — kein wiederholter API-Call.
        Rückgabe: {"min_size": float, "size_precision": int}
        """
        symbol = self._symbol(coin)
        if symbol in BitgetClient._contract_cache:
            return BitgetClient._contract_cache[symbol]

        fallback = {"min_size": 0.0, "size_precision": SIZE_DECIMALS_FALLBACK.get(coin, 2)}
        try:
            data = self._get("/api/v2/mix/market/contracts", {
                "productType": PRODUCT_TYPE,
                "symbol": symbol,
            })
            contracts = data if isinstance(data, list) else []
            for c in contracts:
                if c.get("symbol") == symbol:
                    min_size = float(c.get("minTradeNum", 0) or 0)
                    # volumePlace = Anzahl Nachkommastellen für Size
                    precision = int(c.get("volumePlace", 2) or 2)
                    result = {"min_size": min_size, "size_precision": precision}
                    BitgetClient._contract_cache[symbol] = result
                    return result
        except Exception as e:
            log(f"[BITGET] get_contract_info {coin} fehlgeschlagen: {e}")

        BitgetClient._contract_cache[symbol] = fallback
        return fallback

    def set_leverage(self, coin: str, leverage: int, hold_side: str = "long") -> bool:
        """
        Setzt den Hebel für ein Symbol (isolated margin, je Seite einzeln).
        Im Dry-Run immer True. Gibt True zurück wenn erfolgreich.
        """
        if self.dry_run:
            return True
        try:
            self._post("/api/v2/mix/account/set-leverage", {
                "symbol":      self._symbol(coin),
                "productType": PRODUCT_TYPE,
                "marginCoin":  MARGIN_COIN,
                "leverage":    str(leverage),
                "holdSide":    hold_side,
            })
            return True
        except Exception as e:
            log(f"[BITGET] set_leverage {coin}×{leverage} fehlgeschlagen: {e}")
            return False

    def modify_sl(self, coin: str, new_sl: float, size: float, hold_side: str = "long") -> bool:
        """
        Verschiebt den Stop-Loss auf einen neuen Preis.
        Strategie: bestehende loss_plan-Orders canceln, neue SL-Order platzieren.
        Dry-Run → True ohne API-Call.
        """
        if self.dry_run:
            return True
        try:
            # Nur loss_plan-Orders fetchen und canceln (TP bleibt unberührt)
            data = self._get("/api/v2/mix/order/orders-plan-pending", {
                "productType": PRODUCT_TYPE, "symbol": self._symbol(coin),
                "planType": "loss_plan", "limit": "20",
            }, auth=True)
            orders = (data.get("entrustedList", []) if isinstance(data, dict)
                      else (data if isinstance(data, list) else []))
            if orders:
                order_id_list = [{"orderId": o["orderId"], "clientOid": o.get("clientOid", "")}
                                 for o in orders if o.get("orderId")]
                if order_id_list:
                    self._post("/api/v2/mix/order/cancel-plan-order", {
                        "symbol": self._symbol(coin), "productType": PRODUCT_TYPE,
                        "marginCoin": MARGIN_COIN, "orderIdList": order_id_list,
                    })
            # Neuen SL setzen
            result = self.place_stop_loss(coin, new_sl, size, hold_side)
            return result.success
        except Exception as e:
            log(f"[BITGET] modify_sl {coin} fehlgeschlagen: {e}")
            return False

    def place_partial_close(self, coin: str, size: float, hold_side: str = "long") -> OrderResult:
        """Schließt einen Teil einer Position (reduce_only market order)."""
        is_buy = (hold_side == "short")   # short schließen = buy; long schließen = sell
        return self.place_market_order(coin, is_buy=is_buy, size=size, reduce_only=True)

    def cancel_tpsl_orders(self, coin: str) -> bool:
        if self.dry_run:
            return True
        try:
            data = self._get("/api/v2/mix/order/orders-plan-pending", {
                "productType": PRODUCT_TYPE, "symbol": self._symbol(coin),
                "planType": "profit_loss", "limit": "20",
            }, auth=True)
            orders = (data.get("entrustedList", []) if isinstance(data, dict)
                      else (data if isinstance(data, list) else []))
            if not orders:
                return True
            order_id_list = [{"orderId": o["orderId"], "clientOid": o.get("clientOid", "")}
                             for o in orders if o.get("orderId")]
            self._post("/api/v2/mix/order/cancel-plan-order", {
                "symbol": self._symbol(coin), "productType": PRODUCT_TYPE,
                "marginCoin": MARGIN_COIN, "orderIdList": order_id_list,
            })
            return True
        except Exception:
            return True
