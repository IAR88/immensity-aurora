# 🌌 IMMENSITY AURORA

Algorithmic decision-support trading and macro intelligence framework for portfolio tracking and market analysis.

[![Open in Streamlit](https://static.streamlit.io/badges/streamlit_badge_black_white.svg)](https://immensity-aurora.streamlit.app/)

---


Prerequisite: install `uv` if you don't already have it.

```
$ curl -LsSf https://astral.sh/uv/install.sh | sh
```

1. Sync the dependencies

   ```
   $ uv sync
   ```

2. Run the app

   ```
   $ uv run streamlit run streamlit_app.py
   ```

## Trading status

The dashboard prospects are illustrative examples, not live P1-P15 signals.
The dashboard does not place orders. `engine_live.py` accepts only injected
`LiveTradeSignal` objects; it does not generate signals. The engine requires
historical labeled `CalibrationObservation` data to fit its isotonic
probability calibrator, normalized CVD/momentum/volatility features, positive
independently verified EV, verified orderbook depth/CVD, and all P1-P15
criteria before a trade can be considered. Use the testnet environment by
default (`BYBIT_TESTNET=true`). Mainnet orders additionally require the
explicit `BYBIT_ALLOW_MAINNET_TRADING=true` opt-in. Keep API keys restricted
until the signal source, calibration data, and execution workflow have been
independently reviewed.

`MOCK_MODE=true` starts local mock Bybit/Avanza sessions without credentials.
The mock adapters provide sample balances and empty histories and explicitly
refuse order submission. `mock_predictive_parameters()` provides normalized
fixture values for UI and test use; these values are not verified signals and
cannot be used as live trades. Live proposals must carry all 15 named
predictive-parameter values, a fully verified P1–P15 matrix, directional
orderbook imbalance, and CVD confirmation. Execution requires calibrated
win probability of at least 80%. Fractional Kelly uses available free cash;
at >=90% probability and confluence it uses full Kelly (never more than free
cash), without a fixed transaction-dollar cap.

The Streamlit dashboard always uses the Bybit API and requires `BYBIT_API_KEY`
and `BYBIT_SECRET` (or `BYBIT_API_SECRET`) as environment variables in Render.
Configure `BYBIT_TESTNET` there to select testnet or mainnet; the dashboard does not
switch to mock balances when an API request fails. Set `BYBIT_DOMAIN` to the
supported Bybit account domain (`bybit.com` or `bybit.eu`) when needed. A 403
response is reported as an access restriction; check the selected domain, the
API key's IP allowlist, and regional API availability with Bybit. Changing the
endpoint or using a proxy does not bypass regional restrictions.

The engine prefers 6–10 accepted limit-order submissions per UTC day, enforces
a maximum of 10, and never forces a trade merely to meet the lower target.
Probability features are normalized to `[0, 1]` (CVD and momentum are
direction-adjusted; volatility is a risk score). The engine scores them
0.4/0.4/0.2, then fits isotonic regression to labeled historical outcomes.
Both sides of the orderbook must show at least 1,000 USDT depth by default.

The daily -2% drawdown limit is measured against the configured
`BYBIT_DAILY_START_EQUITY_USDT` or the first equity observed by this process
for that day. If the engine starts after the trading day begins, provide the
actual start-of-day equity to cover losses from before startup. Avanza
monitoring and rotation analysis are read-only; candidates are logged and never
submitted as stock orders. The live engine reads saved positions and cash from
`avanza_positions.json`; if the file is missing, it logs a warning and treats
the portfolio as empty. Stock holdings are not liquid trading funds. Trade
sizing uses Bybit's available balance only; Avanza cash is reported separately
because no currency conversion or transfer integration is configured.
Breakeven/trailing stop calculations are available through the live loop's
paired `protective_stop_provider` and `protective_stop_updater` callbacks; both
must be connected to a verified market/position source and supported Bybit
order-update operation before those adjustments can be applied to live orders.

Add `BYBIT_API_KEY` and `BYBIT_SECRET` to the Render environment to display the
Bybit USDT equity and available balance. Telegram credentials are optional and can be
provided as
`TELEGRAM_TOKEN` (or `TELEGRAM_BOT_TOKEN`) and `TELEGRAM_CHAT_ID` environment
variables for the engine. Configure `NTFY_TOPIC` to enable Ntfy notifications
for portfolio synchronization and signal changes. Use a private topic and set
`NTFY_ACCESS_TOKEN` when required by the server. The smoke test
`ntfy_notification_smoke_test.py` sends only to the configured topic.
Cross-currency totals are not calculated without a verified FX rate.

The live engine listens to Bybit's authenticated private execution stream on a
separate WebSocket connection. Closed spot lots that can be matched to entry
fills observed by this process are saved as JSONL at
`TRADE_ANALYSIS_PATH` (default `data/trade_outcomes.jsonl`) and reported through
Ntfy or structured logs. Each outcome includes its triggering P1-P15 IDs,
fill prices, quantity, fees-adjusted USDT PnL, and a historical signal
performance summary. The analysis is informational: it does not change signal
parameters or submit orders. A trade entered before the listener starts cannot
be assigned an entry price or signal by this process and is not reported as a
matched closed trade.

## Swedish tax export (K4)

`tax_exporter.py` creates a CSV of completed disposals from Bybit spot
executions and Avanza transactions. The history start date must include every
acquisition needed to calculate the cost basis; use `--report-from` to retain
earlier purchases for matching while exporting only the desired tax year.
Replace the example start date with the earliest date needed to cover the
holdings sold in the report.
Avanza uses Genomsnittsmetoden by default; use
`--avanza-cost-method schablon` only for securities eligible for the
20-percent Schablonmetoden. Bybit crypto disposals use FIFO.

```sh
python tax_exporter.py \
  --from-date 2020-01-01 \
  --to-date 2025-12-31 \
  --report-from 2025-01-01 \
  --output K4.csv \
  --fx-rates fx-rates.csv
```

The FX file must contain `date,currency,fx_rate_sek`; the exporter uses the
latest rate on or before each transaction date. SEK transactions use a rate of
1. Foreign-currency transactions fail rather than assume an exchange rate.
Configure `BYBIT_TESTNET=false` for a real export (testnet is refused unless
`--allow-testnet` is explicitly passed). The CSV is a K4-oriented working
report, not an official Skatteverket import file; review transaction fees,
corporate actions, prior holdings, currency rates, and tax classification
before filing.

Use `--pdf-output K4.pdf` for a printable K4 working report, and
`--dac8-output DAC8.csv --dac8-format csv` (or `json`) for the explicitly named
DAC8/CARF Transaction Ledger Export. The ledger contains UTC timestamp, asset
type, transaction ID, gross proceeds, acquisition cost, realized gain/loss,
and counterparty type. It is a structured working ledger, not a certification
of DAC8/CARF compliance or an official filing; confirm current jurisdictional
reporting requirements before submission.

## Avanza API session check

`avanza_scraper.py` uses the existing `avanza-api` session handler to
authenticate and save account positions to the private `avanza_positions.json`
file without launching a browser. The live engine uses this file for Avanza
monitoring, keeps liquid cash separate by currency from stock holdings, and
falls back to an empty snapshot until the file is created.
Set `MOCK_MODE=false` and provide `AVANZA_USERNAME`, `AVANZA_PASSWORD`, and
`AVANZA_TOTP_SECRET` in the environment (or `.env`) to use the live Avanza API.
With `MOCK_MODE=true`, it uses the local mock session.

```sh
uv run python avanza_scraper.py
```

Use `--output PATH` to choose a different output file.

## Avanza portfolio snapshot

`avanza_portfolio.py` reads Avanza's JSON responses while loading the overview
page and writes portfolio value, cash, and recognized stock holdings to
`avanza_portfolio.json`.

```sh
uv sync
uv run playwright install chromium
uv run python avanza_portfolio.py
```

The first run (and runs after the saved browser session expires) opens one
headed browser for you to complete BankID login. Press Enter in the terminal
after Avanza returns to the overview page. Subsequent runs reuse
`avanza_session.json` headlessly. The session file contains authentication
cookies/local storage and both generated JSON files are ignored by Git; protect
them as sensitive personal data. This script does not handle BankID credentials
or automate authentication. Avanza may change its page or response format; if
the captured JSON no longer includes recognizable portfolio fields, the script
reports that instead of writing an empty snapshot.
