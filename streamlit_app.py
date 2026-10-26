import os
import time
from pathlib import Path

import pandas as pd
import requests
import streamlit as st
from dotenv import load_dotenv
from pybit.exceptions import FailedRequestError
from pybit.unified_trading import HTTP
from streamlit.errors import StreamlitSecretNotFoundError

from engine_live import (
    AvanzaPortfolioSnapshot,
    HjarnanLiveEngine,
    PREDICTIVE_PARAMETER_NAMES,
    is_bybit_ip_restriction,
    load_avanza_portfolio,
    mock_predictive_parameters,
)
from auth.sessions_manager import bybit_api_domain_parts

load_dotenv()

# --- PAGE CONFIGURATION & MOBILE COMPACT CSS ---
st.set_page_config(
    page_title="IMMENSITY AURORA",
    page_icon="⚡",
    layout="wide",
)

st.markdown("""
    <style>
    .stApp {
        background: radial-gradient(circle at 20% 0%, #effdf7 0, #edf3ff 42%, #f7f0ff 100%);
        color: #263446;
    }
    h1, h2, h3 {
        color: #354d75 !important;
    }
    [data-testid="stMetric"], [data-testid="stVerticalBlockBorderWrapper"] {
        background: rgba(255, 255, 255, 0.62);
        border: 1px solid rgba(255, 255, 255, 0.82);
        border-radius: 16px;
        box-shadow: 0 8px 28px rgba(79, 93, 133, 0.10);
        backdrop-filter: blur(14px);
    }
    .stMetric {
        padding: 14px;
    }
    .stButton>button {
        background: linear-gradient(100deg, #76b7a3, #8f9bd8);
        color: white;
        border-radius: 12px;
        border: none;
        width: 100%;
    }
    .stButton>button:hover {
        background: linear-gradient(100deg, #5c9b88, #7785c8);
    }
    div.block-container {
        padding-top: 2rem;
        padding-bottom: 2rem;
        max-width: 1200px;
    }
    </style>
""", unsafe_allow_html=True)

# --- FETCH SECRETS ---
try:
    secrets = st.secrets
except StreamlitSecretNotFoundError:
    secrets = {}

def parse_bool(value, name):
    if isinstance(value, bool):
        return value
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean.")

try:
    mock_value = os.getenv("MOCK_MODE", secrets.get("MOCK_MODE", False))
    MOCK_MODE = parse_bool(mock_value, "MOCK_MODE")
    testnet_value = os.getenv(
        "BYBIT_TESTNET", secrets.get("BYBIT_TESTNET", True)
    )
    BYBIT_TESTNET = parse_bool(testnet_value, "BYBIT_TESTNET")
    BYBIT_DOMAIN = os.getenv("BYBIT_DOMAIN", secrets.get("BYBIT_DOMAIN", "bybit.com"))
    try:
        BYBIT_DOMAIN_NAME, BYBIT_DOMAIN_TLD = bybit_api_domain_parts(BYBIT_DOMAIN)
    except ValueError as error:
        st.error(str(error))
        st.stop()
except ValueError as error:
    st.error(str(error))
    st.stop()

BYBIT_API_KEY = secrets.get("BYBIT_API_KEY") or os.getenv("BYBIT_API_KEY", "")
BYBIT_SECRET = (
    secrets.get("BYBIT_SECRET")
    or secrets.get("BYBIT_API_SECRET")
    or os.getenv("BYBIT_SECRET")
    or os.getenv("BYBIT_API_SECRET", "")
)
if not MOCK_MODE and (not BYBIT_API_KEY or not BYBIT_SECRET):
    st.error(
        "Bybit credentials are not configured. Add BYBIT_API_KEY and "
        "BYBIT_SECRET (or BYBIT_API_SECRET) to Streamlit secrets or the "
        "environment, or enable MOCK_MODE=true for offline use."
    )
    st.stop()

# --- BYBIT CLIENT SETUP ---
@st.cache_resource
def get_bybit_session(api_key, api_secret, testnet, domain, tld):
    return HTTP(
        testnet=testnet,
        domain=domain,
        tld=tld,
        api_key=api_key,
        api_secret=api_secret,
    )

# --- HELPER: FETCH REAL USDT BALANCE ---
@st.cache_data(ttl=10)
def get_live_usdt_balances(api_key, api_secret, testnet, simulated_mode=False):
    if MOCK_MODE or simulated_mode:
        available = float(os.getenv("MOCK_BYBIT_FREE_USDT", "200"))
        return {"equity": available, "available": available}
    session = get_bybit_session(
        api_key, api_secret, testnet, BYBIT_DOMAIN_NAME, BYBIT_DOMAIN_TLD
    )
    wallet = session.get_wallet_balance(accountType="UNIFIED", coin="USDT")
    if wallet.get("retCode") != 0:
        raise RuntimeError(
            f"Bybit balance request failed: {wallet.get('retMsg', 'unknown error')}"
        )

    accounts = wallet.get("result", {}).get("list", [])
    if not accounts:
        raise RuntimeError("Bybit returned no unified wallet account")

    coins = accounts[0].get("coin", [])
    usdt = next((coin for coin in coins if coin.get("coin") == "USDT"), None)
    if usdt is None:
        return {"equity": 0.0, "available": 0.0}
    equity = float(usdt.get("equity") or 0.0)
    available_value = usdt.get("availableToWithdraw")
    if available_value not in (None, ""):
        available = float(available_value)
    else:
        available = float(usdt.get("walletBalance") or 0.0) - float(
            usdt.get("locked") or 0.0
        )
    return {"equity": equity, "available": max(available, 0.0)}


@st.cache_data(ttl=10)
def get_live_spot_holdings(api_key, api_secret, testnet, simulated_mode=False):
    if MOCK_MODE or simulated_mode:
        return [{"asset": "USDT", "equity": float(os.getenv("MOCK_BYBIT_FREE_USDT", "200"))}]
    session = get_bybit_session(
        api_key, api_secret, testnet, BYBIT_DOMAIN_NAME, BYBIT_DOMAIN_TLD
    )
    wallet = session.get_wallet_balance(accountType="UNIFIED")
    if wallet.get("retCode") != 0:
        raise RuntimeError(
            f"Bybit holdings request failed: {wallet.get('retMsg', 'unknown error')}"
        )
    accounts = wallet.get("result", {}).get("list", [])
    if not accounts:
        raise RuntimeError("Bybit returned no unified wallet account")
    holdings = []
    for coin in accounts[0].get("coin", []):
        equity = float(coin.get("equity", 0))
        if equity > 0:
            holdings.append({"asset": coin.get("coin", "unknown"), "equity": equity})
    return holdings

# --- HEADER ---
st.title("⚡ IMMENSITY AURORA")
st.caption("ENGINE | MULTI-SERVICE DASHBOARD | Soft Aurora Light")
if MOCK_MODE:
    st.info("Offline mock mode is active. Mock balances and signals are not live or tradeable.")

AVANZA_POSITIONS_FILE = Path(
    os.getenv("AVANZA_POSITIONS_FILE", "avanza_positions.json")
)


def read_avanza_snapshot():
    try:
        return load_avanza_portfolio(AVANZA_POSITIONS_FILE)
    except (OSError, ValueError, TypeError) as error:
        st.warning(f"Could not read saved Avanza positions: {error}")
        return AvanzaPortfolioSnapshot((), (), False)


avanza_snapshot = read_avanza_snapshot()

# --- LIVE METRICS FRAGMENT ---
@st.fragment(run_every=60)
def render_live_metrics():
    ip_restricted = st.session_state.get("bybit_ip_restricted", False)
    retry_at = st.session_state.get("bybit_balance_retry_at", 0.0)
    balance_error = st.session_state.get("bybit_balance_error")
    bybit_equity = st.session_state.get("bybit_balance")
    bybit_available = st.session_state.get("bybit_available")
    spot_holdings = st.session_state.get("bybit_spot_holdings", [])
    holdings_error = st.session_state.get("bybit_holdings_error")
    portfolio = read_avanza_snapshot()

    if ip_restricted:
        balances = get_live_usdt_balances(
            BYBIT_API_KEY, BYBIT_SECRET, BYBIT_TESTNET, simulated_mode=True
        )
        bybit_equity = balances["equity"]
        bybit_available = balances["available"]
        spot_holdings = get_live_spot_holdings(
            BYBIT_API_KEY, BYBIT_SECRET, BYBIT_TESTNET, simulated_mode=True
        )
        balance_error = None
        holdings_error = None
    elif time.time() >= retry_at:
        try:
            balances = get_live_usdt_balances(
                BYBIT_API_KEY,
                BYBIT_SECRET,
                BYBIT_TESTNET,
            )
            bybit_equity = balances["equity"]
            bybit_available = balances["available"]
            st.session_state["bybit_balance"] = bybit_equity
            st.session_state["bybit_available"] = bybit_available
            st.session_state["bybit_balance_error"] = None
            st.session_state["bybit_balance_retry_at"] = 0.0
            balance_error = None
        except (FailedRequestError, requests.exceptions.HTTPError) as error:
            if is_bybit_ip_restriction(error):
                st.session_state["bybit_ip_restricted"] = True
                ip_restricted = True
                balances = get_live_usdt_balances(
                    BYBIT_API_KEY,
                    BYBIT_SECRET,
                    BYBIT_TESTNET,
                    simulated_mode=True,
                )
                bybit_equity = balances["equity"]
                bybit_available = balances["available"]
                spot_holdings = get_live_spot_holdings(
                    BYBIT_API_KEY,
                    BYBIT_SECRET,
                    BYBIT_TESTNET,
                    simulated_mode=True,
                )
                st.session_state["bybit_balance"] = bybit_equity
                st.session_state["bybit_available"] = bybit_available
                st.session_state["bybit_spot_holdings"] = spot_holdings
                st.session_state["bybit_balance_error"] = None
                st.session_state["bybit_holdings_error"] = None
                st.session_state["bybit_balance_retry_at"] = 0.0
                balance_error = None
                holdings_error = None
            else:
                retry_delay = 600 if getattr(error, "status_code", None) == 403 else 60
                st.session_state["bybit_balance_error"] = (
                    f"Bybit request failed (HTTP {getattr(error, 'status_code', 'unknown')})."
                )
                st.session_state["bybit_balance_retry_at"] = time.time() + retry_delay
                balance_error = st.session_state["bybit_balance_error"]
        except Exception as error:
            st.session_state["bybit_balance_error"] = (
                f"Could not retrieve the Bybit balance: {error}. "
                "The dashboard will retry in 1 minute."
            )
            st.session_state["bybit_balance_retry_at"] = time.time() + 60
            balance_error = st.session_state["bybit_balance_error"]

    if not ip_restricted:
        try:
            spot_holdings = get_live_spot_holdings(
                BYBIT_API_KEY, BYBIT_SECRET, BYBIT_TESTNET
            )
            st.session_state["bybit_spot_holdings"] = spot_holdings
            st.session_state["bybit_holdings_error"] = None
            holdings_error = None
        except (FailedRequestError, requests.exceptions.HTTPError) as error:
            if is_bybit_ip_restriction(error):
                st.session_state["bybit_ip_restricted"] = True
                ip_restricted = True
                balances = get_live_usdt_balances(
                    BYBIT_API_KEY,
                    BYBIT_SECRET,
                    BYBIT_TESTNET,
                    simulated_mode=True,
                )
                bybit_equity = balances["equity"]
                bybit_available = balances["available"]
                spot_holdings = get_live_spot_holdings(
                    BYBIT_API_KEY,
                    BYBIT_SECRET,
                    BYBIT_TESTNET,
                    simulated_mode=True,
                )
                st.session_state["bybit_balance"] = bybit_equity
                st.session_state["bybit_available"] = bybit_available
                st.session_state["bybit_spot_holdings"] = spot_holdings
                st.session_state["bybit_balance_error"] = None
                st.session_state["bybit_holdings_error"] = None
                st.session_state["bybit_balance_retry_at"] = 0.0
                balance_error = None
                holdings_error = None
            else:
                holdings_error = f"Could not retrieve open spot holdings: {error}"
                st.session_state["bybit_holdings_error"] = holdings_error
        except Exception as error:
            holdings_error = f"Could not retrieve open spot holdings: {error}"
            st.session_state["bybit_holdings_error"] = holdings_error
    if ip_restricted:
        st.warning("Codespaces IP Restricted — Running in Simulated Mode")

    row1_col1, row1_col2 = st.columns(2)
    with row1_col1:
        st.metric(label="Profit (USDT)", value="Not tracked")
    with row1_col2:
        st.metric(label="Profit (SEK)", value="Not tracked")

    row2_col1, row2_col2, row2_col3 = st.columns(3)
    with row2_col1:
        balance_value = (
            f"{bybit_equity:.2f} USDT" if bybit_equity is not None else "Unavailable"
        )
        st.metric(label="Bybit USDT equity", value=balance_value)
    with row2_col2:
        available_value = (
            f"{bybit_available:.2f} USDT"
            if bybit_available is not None
            else "Unavailable"
        )
        st.metric("Bybit USDT available for orders", available_value)
    avanza_cash_by_currency = {}
    for cash in portfolio.cash_positions:
        avanza_cash_by_currency[cash.currency] = (
            avanza_cash_by_currency.get(cash.currency, 0.0) + cash.amount
        )
    with row2_col3:
        cash_value = (
            f"{avanza_cash_by_currency['SEK']:.2f} SEK"
            if "SEK" in avanza_cash_by_currency
            else "Unavailable"
        )
        st.metric("Avanza liquid cash", cash_value)
    st.metric("Open spot holdings", len(spot_holdings))
    if spot_holdings:
        st.dataframe(
            pd.DataFrame(spot_holdings),
            width="stretch",
            hide_index=True,
        )
    st.caption("Spot wallet assets are shown; derivative positions are not queried.")
    st.metric("Avanza stock positions (not tradeable cash)", len(portfolio.positions))
    if portfolio.positions:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Symbol": position.symbol,
                        "Quantity": position.quantity,
                        "Market value": position.market_value,
                        "Currency": position.currency,
                    }
                    for position in portfolio.positions
                ]
            ),
            width="stretch",
            hide_index=True,
        )
    if avanza_cash_by_currency:
        st.caption(
            "Avanza cash by currency: "
            + ", ".join(
                f"{currency} {amount:.2f}"
                for currency, amount in sorted(avanza_cash_by_currency.items())
            )
        )
    if not portfolio.available:
        st.warning("Saved Avanza positions are unavailable; using empty holdings and cash.")
    if balance_error:
        st.warning(balance_error)
    if holdings_error:
        st.warning(holdings_error)
    st.caption(
        "Order sizing uses Bybit available balance only. Avanza cash and stock "
        "holdings are reported separately; no FX or transfer integration is configured."
    )

render_live_metrics()

st.divider()

# --- TABS LAYOUT ---
tab1, tab2, tab3, tab4 = st.tabs([
    "💼 Real Trades",
    "🌱 Prospects / Seeds",
    "📊 Signal Summary",
    "🧪 Test Trades"
])

with tab1:
    st.info("No live position feed is connected; open positions are unavailable.")
    st.metric("Open positions", "Not connected")

with tab2:
    st.subheader("🌱 Identified Prospects & Learning")
    st.warning(
        "The prospects below are illustrative examples, not verified external P1-P15 signals. "
        "No signal provider or Avanza news/execution API is configured."
    )

    prospects = [
        {
            "id": "p1",
            "asset": "BTC/USDT",
            "chance": "88%",
            "urgency": "🔴 High (Within 5 min)",
            "reason": "Breakout confirmed with volume spike.",
            "matrix_p": "P1 (github_commit_velocity) & P13 (cross_domain_correlation)",
            "saturation": "Level 1: Early Seed (P15)",
            "variable_type": "📈 Trend / News",
            "source_title": "FED cuts rates unexpectedly – Crypto market reacts strongly",
            "source_url": "https://coindesk.com",
        },
        {
            "id": "p2",
            "asset": "ETH/USDT",
            "chance": "74%",
            "urgency": "🟡 Medium (Within 30 min)",
            "reason": "RSI divergence and L2 network upgrade signals.",
            "matrix_p": "P2 (arxiv_preprint_rate) & P3 (tech_media_mention)",
            "saturation": "Level 1: Early Seed (P15)",
            "variable_type": "🔬 Research / Update",
            "source_title": "Ethereum Layer 2 scaling shows 40% lower fees in new survey",
            "source_url": "https://ethereum.org",
        },
        {
            "id": "p3",
            "asset": "SOL/USDT",
            "chance": "62%",
            "urgency": "🟢 Low (Within 2 hours)",
            "reason": "Consolidation near support with institutional volume.",
            "matrix_p": "P7 (viral_velocity_score) & P12 (commodity_flow_velocity)",
            "saturation": "Level 2: Maturing Trend (P15)",
            "variable_type": "📊 Survey / Report",
            "source_title": "Quarterly Report: Institutional Flow in Altcoins Q3 2026",
            "source_url": "https://bloomberg.com",
        }
    ]

    for p in prospects:
        with st.container():
            c1, c2, c3 = st.columns([2, 2, 1])
            c1.markdown(f"**{p['asset']}** ({p['chance']})")
            c2.markdown(f"{p['urgency']}")

            st.markdown(f"**Why:** {p['reason']}")
            st.markdown(f"**P1-P15 Matrix:** `{p['matrix_p']}`")
            st.markdown(f"**Saturation:** `{p['saturation']}`")
            st.markdown(f"**Variable:** `{p['variable_type']}`")
            st.markdown(f"🔗 **Source:** [{p['source_title']}]({p['source_url']})")

            st.caption("Illustrative example only — no allocation or order is generated.")
        st.divider()

with tab3:
    st.subheader("P1–P15 Predictive Seeding Matrix")
    if MOCK_MODE or st.session_state.get("bybit_ip_restricted", False):
        parameters = mock_predictive_parameters()
        st.caption("Offline fixture values (normalized demonstration data; not verified).")
    else:
        parameters = st.session_state.get("external_predictive_parameters")
        if not isinstance(parameters, dict):
            parameters = {}
        st.info("Waiting for externally verified P1–P15 data. No fallback values are treated as verified.")

    for code, parameter_name in PREDICTIVE_PARAMETER_NAMES.items():
        value = parameters.get(parameter_name)
        label = f"{code} · {parameter_name}"
        if value is None:
            st.metric(label, "Unavailable")
        else:
            numeric_value = float(value)
            if parameter_name == "trend_saturation_level":
                st.metric(label, f"Level {int(numeric_value)}")
                st.progress((int(numeric_value) - 1) / 2)
            else:
                st.metric(label, f"{numeric_value:.0%}")
                st.progress(max(0.0, min(1.0, numeric_value)))

    st.divider()
    st.subheader("Avanza Start-account rotation · advisory only")
    targets = st.text_input("Target symbols (comma-separated)", "")
    news_scores_input = st.text_area(
        "News impact scores (SYMBOL:0–1, comma-separated)",
        help="Enter scores from your verified news analysis; no news provider is connected.",
    )
    news_scores = {}
    try:
        for item in news_scores_input.split(","):
            if item.strip():
                symbol, score = item.split(":", maxsplit=1)
                news_scores[symbol.strip()] = float(score)
        if any(not 0 <= score <= 1 for score in news_scores.values()):
            raise ValueError("News impact scores must be between 0 and 1.")
    except ValueError as error:
        st.error(f"Invalid news impact scores: {error}")
        news_scores = {}
    candidates = HjarnanLiveEngine.analyze_stock_rotation(
        avanza_snapshot.positions,
        [symbol.strip() for symbol in targets.split(",") if symbol.strip()],
        news_impact_scores=news_scores,
    )
    avanza_sek_cash = sum(
        cash.amount for cash in avanza_snapshot.cash_positions if cash.currency == "SEK"
    )
    if avanza_sek_cash < 50_000:
        st.caption(
            "Avanza liquid cash is below 50,000 SEK; recommendations remain "
            "advisory and do not use stock holdings as trading capital."
        )
    if not candidates:
        st.info("Enter target symbols to compare against saved Avanza holdings.")
    else:
        st.dataframe(
            pd.DataFrame(
                [
                    {
                        "Action": candidate.action.upper(),
                        "Symbol": candidate.symbol,
                        "News impact": candidate.news_impact_score,
                        "Recommendation": candidate.reason,
                    }
                    for candidate in candidates
                ]
            ),
            width="stretch",
            hide_index=True,
        )
    st.caption("Recommendations are visual alerts only; no Avanza order is sent.")

with tab4:
    st.warning("These rows are static examples, not a live trade history.")
    data = {
        "Timestamp": [
            "2026-10-14 13:19:02", "2026-10-14 12:39:22", "2026-10-14 11:59:24",
            "2026-10-14 11:25:37", "2026-10-14 10:45:53", "2026-10-14 10:03:32"
        ],
        "AssetClass": ["Crypto / Stocks"] * 6,
        "Asset": ["BTC/USDT", "ETH/USDT", "SOL/USDT", "ASML/EUR", "NVDA/USD", "TSLA/USD"],
        "SeedLevel": ["Level 1: Early Seed (P15)"] * 6,
        "MatrixSignal": ["P1 + P13", "P2 + P3", "P7 + P12", "P4 + P6", "P1 + P5", "P8 + P9"]
    }
    st.dataframe(pd.DataFrame(data), width="stretch", hide_index=True)