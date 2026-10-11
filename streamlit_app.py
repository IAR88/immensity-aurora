import streamlit as st
import pandas as pd
import os

# Page configuration
st.set_page_config(
    page_title="IMMENSITY AURORA",
    page_icon="⚡",
    layout="wide"
)

st.title("⚡ IMMENSITY AURORA")
st.caption("Engine | Capital Budget: 1000 SEK")

# Load logs safely
trade_log_path = "trade_log.csv"
signal_summary_path = "signal_summary_log.csv"

if os.path.exists(trade_log_path):
    try:
        df_logs = pd.read_csv(trade_log_path)
    except Exception:
        df_logs = pd.DataFrame()
else:
    df_logs = pd.DataFrame()

if os.path.exists(signal_summary_path):
    try:
        df_signals = pd.read_csv(signal_summary_path)
    except Exception:
        df_signals = pd.DataFrame()
else:
    df_signals = pd.DataFrame()

# Separate Test Trades from Real Trades
if not df_logs.empty:
    df_test_trades = df_logs 
    df_real_trades = pd.DataFrame(columns=df_logs.columns)
else:
    df_test_trades = pd.DataFrame()
    df_real_trades = pd.DataFrame()

st.divider()

# Key Metrics Cards
col1, col2, col3 = st.columns(3)

with col1:
    total_trades = len(df_real_trades)
    st.metric(label=" Real Trades Logged", value=total_trades)

with col2:
    executed_profits = (
        df_real_trades[df_real_trades["Status"].astype(str).str.contains("Executed")]["Estimated_Profit_SEK"].sum() 
        if not df_real_trades.empty and "Estimated_Profit_SEK" in df_real_trades.columns 
        else 0.0
    )
    st.metric(label=" Estimated Profit", value=f"{executed_profits:.2f} SEK")

with col3:
    test_count = len(df_test_trades)
    st.metric(label="🧪 Test Trades Logged", value=test_count)

st.divider()

# Tabbed Layout for Logs
tab1, tab2, tab3 = st.tabs(["🧪 Test Trades", " Real Trades", "📊 Signal Summary"])

with tab1:
    if not df_test_trades.empty:
        st.dataframe(df_test_trades, use_container_width=True)
    else:
        st.info("No test trades available.")

with tab2:
    if not df_real_trades.empty:
        st.dataframe(df_real_trades, use_container_width=True)
    else:
        st.info("No real trades executed yet.")

with tab3:
    if not df_signals.empty:
        st.dataframe(df_signals, use_container_width=True)
    else:
        st.info("No signal summary logs available yet.")
        
