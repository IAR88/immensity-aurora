from __future__ import annotations

import argparse
import asyncio
import json
import math
import os
import re
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Protocol
from urllib.parse import urlparse

OVERVIEW_URL = "https://www.avanza.se/overview/overview.html"
RESPONSE_WAIT_MS = 2_500


class ResponseLike(Protocol):
    url: str
    status: int
    headers: Mapping[str, str]

    async def json(self) -> object: ...


class PageLike(Protocol):
    url: str

    def on(
        self, event: str, listener: Callable[[ResponseLike], None]
    ) -> None: ...

    async def goto(self, url: str, *, wait_until: str) -> object: ...

    async def wait_for_timeout(self, timeout: float) -> None: ...


class PageUrlLike(Protocol):
    url: str


class BrowserContextLike(Protocol):
    async def new_page(self) -> PageLike: ...

    async def storage_state(self) -> dict[str, object]: ...


class BrowserLike(Protocol):
    async def new_context(
        self, *, storage_state: str | None = None
    ) -> BrowserContextLike: ...

    async def close(self) -> None: ...


class ChromiumLike(Protocol):
    async def launch(self, *, headless: bool) -> BrowserLike: ...


class PlaywrightLike(Protocol):
    chromium: ChromiumLike


@dataclass
class JsonResponseCapture:
    playwright_error: type[Exception]
    payloads: list[object] = field(default_factory=list)
    session_expired: bool = False
    _tasks: list[asyncio.Task[None]] = field(default_factory=list)

    def attach(self, page: PageLike) -> None:
        def on_response(response: ResponseLike) -> None:
            self._tasks.append(asyncio.create_task(self._capture(response)))

        page.on("response", on_response)

    async def drain(self) -> None:
        if self._tasks:
            await asyncio.gather(*self._tasks)
            self._tasks.clear()

    async def _capture(self, response: ResponseLike) -> None:
        parsed_url = urlparse(response.url)
        if not _is_avanza_host(parsed_url.hostname):
            return

        if response.status == 401 and "/api/" in parsed_url.path.lower():
            self.session_expired = True
            return

        if response.status >= 400:
            return

        content_type = response.headers.get("content-type", "").lower()
        if "json" not in content_type:
            return

        try:
            payload = await response.json()
        except self.playwright_error:
            return
        self.payloads.append(payload)


def _is_avanza_host(hostname: str | None) -> bool:
    if hostname is None:
        return False
    normalized = hostname.lower()
    return normalized == "avanza.se" or normalized.endswith(".avanza.se")


def _requires_login(page: PageUrlLike) -> bool:
    parsed_url = urlparse(page.url)
    path = parsed_url.path.lower()
    return not _is_avanza_host(parsed_url.hostname) or any(
        marker in path for marker in ("/login", "/logga-in", "/authenticate")
    )


def _normalized_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def _walk_mappings(value: object, depth: int = 0) -> Iterator[tuple[int, Mapping[str, object]]]:
    if isinstance(value, Mapping):
        yield depth, value
        for child in value.values():
            yield from _walk_mappings(child, depth + 1)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_mappings(child, depth + 1)


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        result = float(value)
        return result if math.isfinite(result) else None
    if isinstance(value, Mapping):
        for key in ("amount", "value", "raw", "total", "balance"):
            if key in value:
                result = _number(value[key])
                if result is not None:
                    return result
        return None
    if not isinstance(value, str):
        return None

    normalized = value.replace("\u00a0", "").replace(" ", "").strip()
    if "," in normalized:
        if "." in normalized and normalized.rfind(",") < normalized.rfind("."):
            normalized = normalized.replace(",", "")
        else:
            normalized = normalized.replace(".", "").replace(",", ".")
    try:
        result = float(normalized)
    except ValueError:
        return None
    return result if math.isfinite(result) else None


def _matching_value(
    mapping: Mapping[str, object], aliases: Sequence[str]
) -> tuple[str, object] | None:
    normalized_aliases = {_normalized_key(alias): alias for alias in aliases}
    for key, value in mapping.items():
        normalized_key = _normalized_key(key)
        if normalized_key in normalized_aliases:
            return normalized_aliases[normalized_key], value
    return None


def _find_metric(
    payloads: Sequence[object], aliases: Sequence[str]
) -> float | None:
    ranked: list[tuple[int, int, float]] = []
    alias_order = {_normalized_key(alias): index for index, alias in enumerate(aliases)}
    for payload in payloads:
        for depth, mapping in _walk_mappings(payload):
            for key, value in mapping.items():
                normalized_key = _normalized_key(key)
                priority = alias_order.get(normalized_key)
                if priority is None:
                    continue
                if normalized_key in {"totalvalue", "accountvalue"} and any(
                    _normalized_key(field_name)
                    in {"orderbook", "ticker", "tickersymbol", "symbol", "instrumenttype"}
                    for field_name in mapping
                ):
                    continue
                number = _number(value)
                if number is not None:
                    ranked.append((priority, depth, number))
    if not ranked:
        return None
    return min(ranked, key=lambda item: (item[0], item[1]))[2]


def _find_cash_positions(payloads: Sequence[object]) -> float | None:
    candidates: list[tuple[int, float]] = []
    for payload in payloads:
        for depth, mapping in _walk_mappings(payload):
            cash_rows = next(
                (
                    value
                    for key, value in mapping.items()
                    if _normalized_key(key) == "cashpositions"
                    and isinstance(value, list)
                ),
                None,
            )
            if not isinstance(cash_rows, list):
                continue
            amounts: list[float] = []
            for row in cash_rows:
                if not isinstance(row, Mapping):
                    continue
                matched = _matching_value(row, ("balance", "amount", "cashValue"))
                if matched is not None:
                    amount = _number(matched[1])
                    if amount is not None:
                        amounts.append(amount)
            if amounts:
                candidates.append((depth, sum(amounts)))
    return min(candidates, key=lambda item: item[0])[1] if candidates else None


def _field_from_maps(
    mappings: Sequence[Mapping[str, object]], aliases: Sequence[str]
) -> object | None:
    for mapping in mappings:
        match = _matching_value(mapping, aliases)
        if match is not None:
            return match[1]
    return None


def _parse_holding(row: Mapping[str, object]) -> dict[str, object] | None:
    metadata_maps: list[Mapping[str, object]] = []
    for key in ("instrument", "security", "orderbook"):
        nested = row.get(key)
        if isinstance(nested, Mapping):
            metadata_maps.append(nested)
    metadata_maps.append(row)

    name = _field_from_maps(
        metadata_maps, ("name", "instrumentName", "securityName", "displayName")
    )
    symbol = _field_from_maps(metadata_maps, ("tickerSymbol", "ticker", "symbol"))
    kind = _field_from_maps(
        metadata_maps, ("instrumentType", "assetType", "securityType", "type")
    )
    name_text = name.strip() if isinstance(name, str) else ""
    symbol_text = symbol.strip() if isinstance(symbol, str) else ""
    kind_text = _normalized_key(kind) if isinstance(kind, str) else ""
    is_stock = any(token in kind_text for token in ("stock", "share", "equity", "aktie"))
    if "fund" in kind_text or "bond" in kind_text:
        return None
    if not is_stock and not (not kind_text and symbol_text and name_text):
        return None

    quantity = _number(
        _field_from_maps(metadata_maps, ("quantity", "volume", "shares", "units"))
    )
    value = _number(
        _field_from_maps(
            (row, *metadata_maps),
            ("marketValue", "positionValue", "currentValue", "totalValue", "value"),
        )
    )
    if not name_text and not symbol_text:
        return None

    holding: dict[str, object] = {"name": name_text or symbol_text}
    if symbol_text:
        holding["symbol"] = symbol_text
    if quantity is not None:
        holding["quantity"] = quantity
    if value is not None:
        holding["value"] = value
    currency = _field_from_maps((row, *metadata_maps), ("currencyCode", "currency"))
    if isinstance(currency, str) and currency.strip():
        holding["currency"] = currency.strip()
    account = row.get("account")
    if isinstance(account, Mapping):
        account_name = _field_from_maps((account,), ("accountName", "name"))
        if isinstance(account_name, str) and account_name.strip():
            holding["account"] = account_name.strip()
    return holding


def _find_holdings(payloads: Sequence[object]) -> list[dict[str, object]]:
    list_keys = {"positions", "holdings", "withorderbook"}
    found: dict[tuple[object, ...], dict[str, object]] = {}
    for payload in payloads:
        for _, mapping in _walk_mappings(payload):
            for key, value in mapping.items():
                if _normalized_key(key) not in list_keys or not isinstance(value, list):
                    continue
                for row in value:
                    if not isinstance(row, Mapping):
                        continue
                    holding = _parse_holding(row)
                    if holding is None:
                        continue
                    dedupe_key = (
                        holding.get("account"),
                        holding.get("symbol"),
                        holding.get("name"),
                        holding.get("quantity"),
                        holding.get("value"),
                    )
                    found.setdefault(dedupe_key, holding)
    return list(found.values())


def extract_portfolio(payloads: Sequence[object]) -> dict[str, object] | None:
    total_value = _find_metric(
        payloads,
        (
            "totalPortfolioValue",
            "portfolioValue",
            "totalMarketValue",
            "portfolioMarketValue",
            "totalValue",
            "accountValue",
            "totalBalance",
        ),
    )
    cash = _find_metric(
        payloads,
        ("cashBalance", "cashAmount", "cashValue", "availableCash", "uninvestedCash", "cash"),
    )
    if cash is None:
        cash = _find_cash_positions(payloads)
    holdings = _find_holdings(payloads)
    if total_value is None and cash is None and not holdings:
        return None
    return {
        "fetched_at": datetime.now(UTC).isoformat(),
        "source": "Avanza",
        "total_value": total_value,
        "cash": cash,
        "holdings": holdings,
    }


def _storage_state_is_usable(path: Path) -> bool:
    if not path.is_file():
        return False
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return False
    return (
        isinstance(state, dict)
        and isinstance(state.get("cookies"), list)
        and isinstance(state.get("origins"), list)
    )


def _write_private_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            json.dump(value, temporary_file, ensure_ascii=False, indent=2, allow_nan=False)
            temporary_file.write("\n")
        os.chmod(temporary_path, 0o600)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None and temporary_path.exists():
            temporary_path.unlink()


async def _wait_for_responses(page: PageLike, capture: JsonResponseCapture) -> None:
    await page.wait_for_timeout(RESPONSE_WAIT_MS)
    await capture.drain()


async def _collect_authenticated_page(
    page: PageLike,
    context: BrowserContextLike,
    capture: JsonResponseCapture,
    session_file: Path,
    output_file: Path,
) -> bool:
    capture.attach(page)
    await page.goto(OVERVIEW_URL, wait_until="domcontentloaded")
    await _wait_for_responses(page, capture)
    if _requires_login(page) or capture.session_expired:
        return False

    portfolio = extract_portfolio(capture.payloads)
    if portfolio is None:
        raise RuntimeError(
            "Avanza loaded without an expired-session signal, but no portfolio "
            "JSON data was recognized. The site's response schema may have changed."
        )

    _write_private_json(session_file, await context.storage_state())
    _write_private_json(output_file, portfolio)
    return True


async def _attempt_saved_session(
    playwright: PlaywrightLike,
    session_file: Path,
    output_file: Path,
    playwright_error: type[Exception],
) -> bool:
    browser = await playwright.chromium.launch(headless=True)
    try:
        context = await browser.new_context(storage_state=str(session_file))
        page = await context.new_page()
        capture = JsonResponseCapture(playwright_error)
        return await _collect_authenticated_page(
            page, context, capture, session_file, output_file
        )
    finally:
        await browser.close()


async def _manual_login(
    playwright: PlaywrightLike,
    session_file: Path,
    output_file: Path,
    playwright_error: type[Exception],
) -> None:
    browser = await playwright.chromium.launch(headless=False)
    try:
        context = await browser.new_context()
        page = await context.new_page()
        await page.goto(OVERVIEW_URL, wait_until="domcontentloaded")
        print(
            "Complete the BankID login in the browser. When Avanza has returned "
            "to the overview page, press Enter here."
        )
        try:
            await asyncio.to_thread(input)
        except EOFError as error:
            raise RuntimeError(
                "Interactive login needs a terminal. Run this script from a terminal "
                "and complete BankID in the opened browser."
            ) from error
        if _requires_login(page):
            raise RuntimeError(
                "Avanza still shows a login page. Complete BankID login before "
                "pressing Enter."
            )

        _write_private_json(session_file, await context.storage_state())
        capture = JsonResponseCapture(playwright_error)
        succeeded = await _collect_authenticated_page(
            page, context, capture, session_file, output_file
        )
        if not succeeded:
            raise RuntimeError(
                "The new session was not accepted by Avanza. Complete login again "
                "and rerun the script."
            )
    finally:
        await browser.close()


async def run(session_file: Path, output_file: Path) -> None:
    try:
        from playwright.async_api import Error as PlaywrightError
        from playwright.async_api import async_playwright
    except ImportError as error:
        raise RuntimeError(
            "Playwright is required. Install project dependencies with `uv sync`, "
            "then install Chromium with `uv run playwright install chromium`."
        ) from error

    async with async_playwright() as playwright:
        session_is_usable = _storage_state_is_usable(session_file)
        if session_is_usable:
            session_valid = await _attempt_saved_session(
                playwright, session_file, output_file, PlaywrightError
            )
            if session_valid:
                return
            print("Saved Avanza session is expired; opening one login browser.")
        await _manual_login(
            playwright, session_file, output_file, PlaywrightError
        )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch Avanza portfolio data using a locally saved browser session."
    )
    parser.add_argument(
        "--session-file", type=Path, default=Path("avanza_session.json")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("avanza_portfolio.json")
    )
    args = parser.parse_args()
    asyncio.run(run(args.session_file, args.output))


if __name__ == "__main__":
    main()
