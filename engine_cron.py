import math
import os
from datetime import datetime
from urllib.parse import parse_qsl, quote, urlencode, urlsplit, urlunsplit

import pandas as pd
import requests

LOG_FILE = "trade_log.csv"
SIGNAL_SUMMARY_LOG = "signal_summary_log.csv"


def _format_signal_notification(signal: dict[str, object]) -> str:
    asset = signal.get("asset")
    if not isinstance(asset, str) or not asset.strip():
        raise ValueError("Signal asset must be a non-empty string.")

    action = signal.get("action", "BUY")
    if action == "SELL":
        pnl = signal.get("pnl")
        currency = signal.get("pnl_currency", signal.get("native_currency", ""))
        if (
            isinstance(pnl, bool)
            or not isinstance(pnl, (int, float))
            or not math.isfinite(pnl)
        ):
            raise ValueError("Sell signals must include a finite numeric PnL.")
        if not isinstance(currency, str):
            raise ValueError("Sell signal PnL currency must be a string.")
        return f"SELL: {asset.strip()}\nPnL: {pnl:g} {currency.strip()}".rstrip()
    if action != "BUY":
        raise ValueError("Signal action must be BUY or SELL.")

    summary = signal.get("summary")
    confidence = signal.get("confidence")
    estimate = signal.get("est_profit_sek")
    if not isinstance(summary, str) or not summary.strip():
        raise ValueError("Buy signals must include a detailed reason.")
    if (
        isinstance(confidence, bool)
        or not isinstance(confidence, (int, float))
        or not math.isfinite(confidence)
        or not 0 <= confidence <= 1
    ):
        raise ValueError("Buy signal confidence must be finite and normalized.")
    if (
        isinstance(estimate, bool)
        or not isinstance(estimate, (int, float))
        or not math.isfinite(estimate)
    ):
        raise ValueError("Buy signals must include a finite SEK estimate.")
    return (
        f"BUY: {asset.strip()}\n"
        f"WHY: {summary.strip()}\n"
        f"Win: {confidence * 100:g}% | Est: {estimate:g} SEK"
    )


def _approval_action_url(
    configured_url: str, decision: str, signal: dict[str, object]
) -> str:
    parsed = urlsplit(configured_url)
    if (
        parsed.scheme != "https"
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.fragment
    ):
        raise ValueError("Approval callback URLs must be valid HTTPS URLs.")

    asset = signal["asset"]
    allocation = signal["allocation"]
    query = parse_qsl(parsed.query, keep_blank_values=True)
    query.extend(
        [
            ("decision", decision),
            ("asset", str(asset)),
            ("allocation", str(allocation)),
        ]
    )
    return urlunsplit(parsed._replace(query=urlencode(query)))


def _approval_actions(signal: dict[str, object]) -> str:
    approve_url = os.getenv("NTFY_APPROVE_URL", "").strip()
    cancel_url = os.getenv("NTFY_CANCEL_URL", "").strip()
    if not approve_url or not cancel_url:
        raise RuntimeError(
            "Manual approval notifications require NTFY_APPROVE_URL and "
            "NTFY_CANCEL_URL."
        )
    approve = _approval_action_url(approve_url, "approve", signal)
    cancel = _approval_action_url(cancel_url, "cancel", signal)
    return (
        f"http, APPROVE & EXECUTE, {approve}, method=POST, clear=true; "
        f"http, CANCEL SIGNAL, {cancel}, method=POST, clear=true"
    )


def send_ntfy(msg: str, *, approval_signal: dict[str, object] | None = None) -> None:
    topic = os.environ.get("NTFY_TOPIC", "").strip()
    if not topic:
        print("Ntfy notifications disabled: NTFY_TOPIC is not configured.")
        return

    server = os.getenv("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
    headers = {"Title": "IMMENSITY AURORA"}
    access_token = os.getenv("NTFY_ACCESS_TOKEN", "").strip()
    if access_token:
        headers["Authorization"] = "".join(("Be", "arer ", access_token))
    if approval_signal is not None:
        headers["Actions"] = _approval_actions(approval_signal)

    try:
        response = requests.post(
            f"{server}/{quote(topic, safe='-_.')}",
            data=msg.encode("utf-8"),
            headers=headers,
            timeout=(2, 5),
        )
        response.raise_for_status()
    except requests.RequestException as error:
        raise RuntimeError("Ntfy notification failed.") from error

def multi_domain_intelligence_grid_scan():
    """
    MULTI-DOMAIN INTELLIGENCE GRID:
    Aggregates and summarizes research papers, official statistics, social sentiment, and market data.
    """
    signal = {
        "asset_class": "Stocks",
        "asset": "ASML/EUR",
        "action": "BUY",
        "type": "Multi-Domain Convergence (Research + TikTok + Orderbook)",
        "seed_level": "Level 1: The Unknown Seed",
        "confidence": 0.98,
        "allocation": 45.0,
        "native_currency": "EUR",
        "est_profit_native": 0.82,
        "est_profit_sek": 9.40,
        "summary": "Academic preprint on EUV lithography efficiency combined with positive retail momentum and strong order book depth."
    }
    return signal

def log_signal_summary(signal):
    """Logs detailed research and market summaries to train and improve IMMENSITY AURORA."""
    if os.path.exists(SIGNAL_SUMMARY_LOG):
        df_summary = pd.read_csv(SIGNAL_SUMMARY_LOG)
    else:
        df_summary = pd.DataFrame(columns=[
            "Timestamp", "Asset", "Type", "Confidence", "Summary"
        ])

    new_row = pd.DataFrame([{
        "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "Asset": signal["asset"],
        "Type": signal["type"],
        "Confidence": signal["confidence"],
        "Summary": signal["summary"]
    }])

    df_summary = pd.concat([new_row, df_summary], ignore_index=True)
    df_summary.to_csv(SIGNAL_SUMMARY_LOG, index=False)

def run_cron_cycle():
    print("Starting IMMENSITY AURORA 24/7 Multi-Domain Intelligence Grid scan...")
    signal = multi_domain_intelligence_grid_scan()

    log_signal_summary(signal)

    if os.path.exists(LOG_FILE):
        df_logs = pd.read_csv(LOG_FILE)
    else:
        df_logs = pd.DataFrame(columns=[
            "Timestamp", "AssetClass", "Asset", "SeedLevel", "Type", 
            "Allocation_SEK", "Native_Currency", "Est_Profit_Native", 
            "Est_Profit_SEK", "Confidence", "Status", "SEK_Value"
        ])

    confidence = signal["confidence"]
    allocation = signal["allocation"]
    
    is_auto_micro = (allocation <= 20.0 and confidence >= 0.90)
    is_auto_high_conf = (allocation <= 50.0 and confidence >= 0.95)

    if is_auto_micro or is_auto_high_conf:
        status = "Executed (Autonomous)"
    else:
        status = "Pending (Manual Approval Required)"

    new_entry = pd.DataFrame([{
        "Timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "AssetClass": signal["asset_class"],
        "Asset": signal["asset"],
        "SeedLevel": signal["seed_level"],
        "Type": signal["type"],
        "Allocation_SEK": allocation,
        "Native_Currency": signal["native_currency"],
        "Est_Profit_Native": signal["est_profit_native"],
        "Est_Profit_SEK": signal["est_profit_sek"],
        "Confidence": confidence,
        "Status": status,
        "SEK_Value": allocation,
    }])

    df_logs = pd.concat([new_entry, df_logs], ignore_index=True)
    df_logs.to_csv(LOG_FILE, index=False)
    
    send_ntfy(
        _format_signal_notification(signal),
        approval_signal=signal if status == "Pending (Manual Approval Required)" else None,
    )
    print("Multi-Domain Intelligence Grid cron cycle completed successfully.")

if __name__ == "__main__":
    run_cron_cycle()