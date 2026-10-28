from __future__ import annotations

import os
from typing import TYPE_CHECKING

from dotenv import load_dotenv

if TYPE_CHECKING:
    from avanza import Avanza
    from pybit.unified_trading import HTTP


def bybit_api_domain_parts(domain: str) -> tuple[str, str]:
    normalized = domain.strip().lower()
    supported_domains = {
        "bybit.com": ("bybit", "com"),
        "bybit.eu": ("bybit", "eu"),
    }
    try:
        return supported_domains[normalized]
    except KeyError as error:
        raise ValueError(
            "BYBIT_DOMAIN must be one of the explicitly supported domains: "
            "bybit.com or bybit.eu."
        ) from error


class MockBybitSession:
    testnet = True

    def __init__(self, free_usdt: float) -> None:
        self.free_usdt = free_usdt

    def get_wallet_balance(
        self, *, accountType: str, coin: str = ""
    ) -> dict[str, object]:
        if accountType != "UNIFIED":
            raise ValueError("Mock Bybit supports only the UNIFIED account.")
        if coin in {"", "USDT"}:
            balance = str(self.free_usdt)
            coin_rows = [
                {
                    "coin": "USDT",
                    "equity": balance,
                    "availableToWithdraw": balance,
                    "walletBalance": balance,
                    "locked": "0",
                }
            ]
        else:
            coin_rows = []
        return {
            "retCode": 0,
            "result": {
                "list": [{"totalEquity": str(self.free_usdt), "coin": coin_rows}]
            },
        }

    def get_executions(self, **_: object) -> dict[str, object]:
        return {
            "retCode": 0,
            "result": {"list": [], "nextPageCursor": ""},
        }

    def get_accounts_positions(self) -> dict[str, list[object]]:
        return {"withOrderbook": [], "withoutOrderbook": [], "cashPositions": []}

    def place_order(self, **_: object) -> None:
        raise RuntimeError("Mock mode cannot submit orders.")


class MockAvanzaSession:
    def get_accounts_positions(self) -> dict[str, list[object]]:
        return {"withOrderbook": [], "withoutOrderbook": [], "cashPositions": []}

    def get_transactions_details(self, **_: object) -> dict[str, list[object]]:
        return {"transactions": []}

    def get_positions(self) -> list[object]:
        return []


class SessionsManager:
    def __init__(self) -> None:
        load_dotenv()
        self.mock_mode = self._env_bool("MOCK_MODE", default=False)
        self.avanza_session: Avanza | MockAvanzaSession | None = None
        self.bybit_session: HTTP | MockBybitSession | None = None
        self.bybit_testnet: bool | None = None

    def init_avanza_session(self) -> None:
        if self.mock_mode:
            self.avanza_session = MockAvanzaSession()
            return

        from avanza import Avanza

        username = self._required_env("AVANZA_USERNAME")
        password = self._required_env("AVANZA_PASSWORD")
        totp_secret = self._required_env("AVANZA_TOTP_SECRET")

        session = Avanza(
            {
                "username": username,
                "password": password,
                "totpSecret": totp_secret,
            }
        )
        self.avanza_session = session

    def get_avanza_session(self) -> Avanza | MockAvanzaSession:
        if self.avanza_session is None:
            self.init_avanza_session()
        if self.avanza_session is None:
            raise RuntimeError(
                "Avanza session initialization did not produce a session."
            )
        return self.avanza_session

    def get_bybit_credentials(self) -> dict[str, str | bool]:
        if self.mock_mode:
            return {"testnet": True, "api_key": "", "api_secret": ""}
        return {
            "testnet": self._env_bool("BYBIT_TESTNET", default=True),
            "api_key": self._required_env("BYBIT_API_KEY"),
            "api_secret": os.getenv("BYBIT_SECRET")
            or self._required_env("BYBIT_API_SECRET"),
        }

    def get_bybit_session(self) -> HTTP | MockBybitSession:
        if self.bybit_session is None:
            if self.mock_mode:
                free_usdt = float(os.getenv("MOCK_BYBIT_FREE_USDT", "200"))
                if not 0 <= free_usdt < float("inf"):
                    raise ValueError("MOCK_BYBIT_FREE_USDT must be finite and non-negative.")
                self.bybit_testnet = True
                self.bybit_session = MockBybitSession(free_usdt)
                return self.bybit_session

            from pybit.unified_trading import HTTP

            credentials = self.get_bybit_credentials()
            testnet = credentials["testnet"]
            api_key = credentials["api_key"]
            api_secret = credentials["api_secret"]
            if (
                not isinstance(testnet, bool)
                or not isinstance(api_key, str)
                or not isinstance(api_secret, str)
            ):
                raise ValueError("Invalid Bybit credentials or testnet setting.")
            self.bybit_testnet = testnet
            domain, tld = bybit_api_domain_parts(
                os.getenv("BYBIT_DOMAIN", "bybit.com")
            )
            self.bybit_session = HTTP(
                testnet=testnet,
                domain=domain,
                tld=tld,
                api_key=api_key,
                api_secret=api_secret,
            )
        return self.bybit_session

    @staticmethod
    def _required_env(name: str) -> str:
        value = os.getenv(name)
        if not value:
            raise ValueError(f"Missing required environment variable: {name}")
        return value

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
