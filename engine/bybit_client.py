from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import threading
import time
import uuid
from decimal import Decimal, InvalidOperation, ROUND_DOWN
from typing import Any
from urllib.parse import urlencode

import requests

from auth.sessions_manager import bybit_api_domain_parts

logger = logging.getLogger(__name__)


class BybitAPIError(RuntimeError):
    """Raised when a Bybit v5 request fails or returns an invalid response."""


class BybitClient:
    """Small signed Bybit v5 REST client with precision and retry guardrails."""

    def __init__(
        self,
        api_key: str | None = None,
        api_secret: str | None = None,
        *,
        testnet: bool | None = None,
        domain: str | None = None,
        recv_window: int = 5_000,
        timeout: float = 10.0,
        max_retries: int = 3,
        min_request_interval: float = 0.1,
        session: requests.Session | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("BYBIT_API_KEY", "").strip()
        self.api_secret = (
            api_secret
            or os.getenv("BYBIT_API_SECRET", "").strip()
            or os.getenv("BYBIT_SECRET", "").strip()
        )
        if not self.api_key:
            raise ValueError("Missing required environment variable: BYBIT_API_KEY")
        if not self.api_secret:
            raise ValueError(
                "Missing required environment variable: BYBIT_API_SECRET"
            )
        if recv_window <= 0 or timeout <= 0 or min_request_interval < 0:
            raise ValueError("Bybit request timing configuration is invalid.")
        if max_retries < 0:
            raise ValueError("max_retries must be non-negative.")
        self.testnet = (
            self._env_bool("BYBIT_TESTNET", default=True)
            if testnet is None
            else testnet
        )
        self.allow_mainnet_trading = self._env_bool(
            "BYBIT_ALLOW_MAINNET_TRADING", default=False
        )
        configured_domain = domain or os.getenv("BYBIT_DOMAIN", "bybit.eu")
        domain_name, tld = bybit_api_domain_parts(configured_domain)
        host = f"api.{domain_name}.{tld}"
        if self.testnet:
            host = f"api-testnet.{domain_name}.{tld}"
        self.base_url = f"https://{host}"
        self.recv_window = recv_window
        self.timeout = timeout
        self.max_retries = max_retries
        self.min_request_interval = min_request_interval
        self._session = session or requests.Session()
        self._request_lock = threading.Lock()
        self._last_request_at = 0.0
        self._instrument_cache: dict[str, dict[str, Decimal]] = {}

    @staticmethod
    def _env_bool(name: str, *, default: bool) -> bool:
        value = os.getenv(name)
        if value is None:
            return default
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        raise ValueError(f"Invalid boolean value for {name}: expected true or false.")

    def _throttle(self) -> None:
        with self._request_lock:
            now = time.monotonic()
            delay = self.min_request_interval - (now - self._last_request_at)
            if delay > 0:
                time.sleep(delay)
            self._last_request_at = time.monotonic()

    def _signature(self, timestamp: str, payload: str) -> str:
        message = (
            f"{timestamp}{self.api_key}{self.recv_window}{payload}".encode("utf-8")
        )
        return hmac.new(
            self.api_secret.encode("utf-8"), message, hashlib.sha256
        ).hexdigest()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, object] | None = None,
        body: dict[str, object] | None = None,
        retry_safe: bool = True,
    ) -> dict[str, Any]:
        method = method.upper()
        query = urlencode(sorted((params or {}).items())) if method == "GET" else ""
        serialized_body = (
            json.dumps(body, separators=(",", ":"), ensure_ascii=False)
            if method != "GET" and body is not None
            else ""
        )
        payload = query if method == "GET" else serialized_body
        url = f"{self.base_url}{path}"
        if query:
            url = f"{url}?{query}"

        attempts = self.max_retries + 1 if retry_safe else 1
        for attempt in range(attempts):
            timestamp = str(int(time.time() * 1000))
            headers = {
                "X-BAPI-API-KEY": self.api_key,
                "X-BAPI-SIGN": self._signature(timestamp, payload),
                "X-BAPI-SIGN-TYPE": "2",
                "X-BAPI-TIMESTAMP": timestamp,
                "X-BAPI-RECV-WINDOW": str(self.recv_window),
                "Content-Type": "application/json",
            }
            self._throttle()
            try:
                response = self._session.request(
                    method,
                    url,
                    headers=headers,
                    data=serialized_body if method != "GET" else None,
                    timeout=self.timeout,
                )
            except requests.RequestException as error:
                if attempt + 1 >= attempts:
                    raise BybitAPIError(
                        f"Bybit {method} {path} network request failed; "
                        "write requests are not retried because acceptance may be ambiguous."
                    ) from error
                self._retry_delay(attempt)
                continue

            if response.status_code == 429 and attempt + 1 <= self.max_retries:
                self._retry_delay(attempt, response)
                continue
            if response.status_code >= 500 and retry_safe:
                if attempt + 1 < attempts:
                    self._retry_delay(attempt, response)
                    continue
            try:
                response.raise_for_status()
            except requests.HTTPError as error:
                raise BybitAPIError(
                    f"Bybit {method} {path} returned HTTP {response.status_code}."
                ) from error

            try:
                payload_json = response.json()
            except (ValueError, json.JSONDecodeError) as error:
                raise BybitAPIError(
                    f"Bybit {method} {path} returned invalid JSON."
                ) from error
            if not isinstance(payload_json, dict):
                raise BybitAPIError(
                    f"Bybit {method} {path} returned a malformed response."
                )
            ret_code = payload_json.get("retCode")
            if ret_code is None or str(ret_code) != "0":
                raise BybitAPIError(
                    f"Bybit {method} {path} failed "
                    f"({ret_code}): {payload_json.get('retMsg', 'unknown error')}"
                )
            return payload_json
        raise BybitAPIError(f"Bybit {method} {path} exhausted retry attempts.")

    def _retry_delay(
        self, attempt: int, response: requests.Response | None = None
    ) -> None:
        if response is not None:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = max(0.0, float(retry_after))
                except ValueError:
                    delay = min(0.5 * (2**attempt), 5.0)
            else:
                delay = min(0.5 * (2**attempt), 5.0)
        else:
            delay = min(0.5 * (2**attempt), 5.0)
        logger.warning("Retrying transient Bybit REST failure in %.2fs.", delay)
        time.sleep(delay)

    def _wallet_coin(self, coin: str) -> dict[str, object]:
        if not coin or not coin.isalnum():
            raise ValueError("coin must be a non-empty alphanumeric currency code.")
        response = self._request(
            "GET",
            "/v5/account/wallet-balance",
            params={"accountType": "UNIFIED", "coin": coin.upper()},
        )
        result = response.get("result")
        accounts = result.get("list", []) if isinstance(result, dict) else []
        if not isinstance(accounts, list) or not accounts:
            raise BybitAPIError("Bybit returned no unified wallet account.")
        account = accounts[0]
        if not isinstance(account, dict):
            raise BybitAPIError("Bybit returned a malformed wallet account.")
        coins = account.get("coin", [])
        if not isinstance(coins, list):
            raise BybitAPIError("Bybit wallet response has an invalid coin list.")
        found = next(
            (
                item
                for item in coins
                if isinstance(item, dict) and item.get("coin") == coin.upper()
            ),
            None,
        )
        if found is None:
            return {"coin": coin.upper(), "availableToWithdraw": "0"}
        return found

    def get_wallet_balance(self, coin: str = "USDT") -> float:
        """Return the available UNIFIED-account balance for a coin."""
        wallet_coin = self._wallet_coin(coin)
        available = wallet_coin.get("availableToWithdraw")
        if available not in (None, ""):
            balance = self._decimal(available, "available balance")
        else:
            wallet_balance = self._decimal(
                wallet_coin.get("walletBalance", "0"), "wallet balance"
            )
            locked = self._decimal(wallet_coin.get("locked", "0"), "locked balance")
            balance = wallet_balance - locked
        if balance < 0:
            raise BybitAPIError("Bybit returned a negative available balance.")
        return float(balance)

    def _instrument(self, symbol: str) -> dict[str, Decimal]:
        cached = self._instrument_cache.get(symbol)
        if cached is not None:
            return cached
        response = self._request(
            "GET",
            "/v5/market/instruments-info",
            params={"category": "spot", "symbol": symbol},
        )
        result = response.get("result")
        instruments = result.get("list", []) if isinstance(result, dict) else []
        if not isinstance(instruments, list) or len(instruments) != 1:
            raise BybitAPIError(f"Could not retrieve spot rules for {symbol}.")
        instrument = instruments[0]
        if not isinstance(instrument, dict):
            raise BybitAPIError(f"Bybit returned malformed spot rules for {symbol}.")
        lot_filter = instrument.get("lotSizeFilter", {})
        price_filter = instrument.get("priceFilter", {})
        try:
            rules = {
                "qty_step": self._decimal(lot_filter["qtyStep"], "quantity step"),
                "min_qty": self._decimal(lot_filter["minOrderQty"], "minimum quantity"),
                "tick_size": self._decimal(price_filter["tickSize"], "tick size"),
                "min_amount": self._decimal(
                    lot_filter.get("minOrderAmt", "0"), "minimum order amount"
                ),
            }
        except (KeyError, TypeError) as error:
            raise BybitAPIError(f"Incomplete spot rules for {symbol}.") from error
        if any(rules[key] <= 0 for key in ("qty_step", "min_qty", "tick_size")):
            raise BybitAPIError(f"Invalid spot precision rules for {symbol}.")
        self._instrument_cache[symbol] = rules
        return rules

    def execute_order(
        self,
        symbol: str,
        side: str,
        qty: float,
        order_type: str = "Market",
        *,
        price: float | None = None,
    ) -> dict[str, Any]:
        """Place one spot order after validating and rounding exchange filters."""
        if not self.testnet and not self.allow_mainnet_trading:
            raise RuntimeError(
                "Mainnet orders are disabled; set "
                "BYBIT_ALLOW_MAINNET_TRADING=true to opt in."
            )
        if not symbol or not symbol.isalnum():
            raise ValueError("symbol must be an alphanumeric Bybit symbol.")
        if side not in {"Buy", "Sell"}:
            raise ValueError("side must be 'Buy' or 'Sell'.")
        if order_type not in {"Market", "Limit"}:
            raise ValueError("order_type must be 'Market' or 'Limit'.")
        quantity = self._decimal(qty, "quantity")
        if quantity <= 0:
            raise ValueError("quantity must be positive.")
        rules = self._instrument(symbol)
        rounded_qty = self._round_down(quantity, rules["qty_step"])
        if rounded_qty < rules["min_qty"]:
            raise ValueError(f"Order quantity is below the {symbol} minimum.")

        body: dict[str, object] = {
            "category": "spot",
            "symbol": symbol,
            "side": side,
            "orderType": order_type,
            "qty": self._format_decimal(rounded_qty),
            "orderLinkId": f"ia{uuid.uuid4().hex[:30]}",
        }
        if order_type == "Market":
            body["marketUnit"] = "baseCoin"
        else:
            if price is None:
                raise ValueError("Limit orders require a price.")
            limit_price = self._decimal(price, "price")
            if limit_price <= 0:
                raise ValueError("price must be positive.")
            body["price"] = self._format_decimal(
                self._round_down(limit_price, rules["tick_size"])
            )
            body["timeInForce"] = "GTC"
        if (
            order_type == "Limit"
            and rules["min_amount"] > 0
            and rounded_qty * self._decimal(body["price"], "rounded price")
            < rules["min_amount"]
        ):
            raise ValueError(f"Order value is below the {symbol} minimum.")

        response = self._request(
            "POST",
            "/v5/order/create",
            body=body,
            retry_safe=False,
        )
        order_result = response.get("result")
        if not isinstance(order_result, dict):
            raise BybitAPIError("Bybit accepted order without a result object.")
        return response

    def close_position(self, symbol: str) -> dict[str, Any]:
        """Market-sell the available base-coin balance for a spot symbol."""
        if not symbol or not symbol.isalnum():
            raise ValueError("symbol must be an alphanumeric Bybit symbol.")
        quote_currencies = ("USDT", "USDC", "EUR", "BTC", "ETH", "USD")
        quote = next(
            (currency for currency in quote_currencies if symbol.endswith(currency)),
            None,
        )
        if quote is None or len(symbol) <= len(quote):
            raise ValueError(f"Cannot determine base currency for {symbol}.")
        base_coin = symbol[: -len(quote)]
        quantity = self.get_wallet_balance(base_coin)
        if quantity <= 0:
            raise BybitAPIError(f"No available {base_coin} balance to close.")
        return self.execute_order(symbol, "Sell", quantity, "Market")

    @staticmethod
    def _decimal(value: object, field: str) -> Decimal:
        if isinstance(value, bool):
            raise ValueError(f"{field} must be a finite number.")
        try:
            parsed = Decimal(str(value))
        except (InvalidOperation, ValueError, TypeError) as error:
            raise ValueError(f"{field} must be a finite number.") from error
        if not parsed.is_finite():
            raise ValueError(f"{field} must be finite.")
        return parsed

    @staticmethod
    def _round_down(value: Decimal, step: Decimal) -> Decimal:
        return (value / step).to_integral_value(rounding=ROUND_DOWN) * step

    @staticmethod
    def _format_decimal(value: Decimal) -> str:
        return format(value.normalize(), "f")
