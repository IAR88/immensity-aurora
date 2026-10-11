import streamlit as st
import pandas as pd
import os

# Page configuration
st.set_page_config(
    page_title="IMMENSITY AURORA",
    page_icon="⚡",
    layout="centered"
)

# App-like styling
st.markdown("""
    <style>
    .metric-card {
        background-color: #1e1e1e;
        padding: 15px;
        border-radius: 10px;
        text-align: center;
        border: 1px solid #333;
    }
    </style>
""", unsafe_allow_html=True)

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
    st.metric(label="Real Trades", value=total_trades)

with col2:
    executed_profits = (
        df_real_trades[df_real_trades["Status"].astype(str).str.contains("Executed")]["Estimated_Profit_SEK"].sum() 
        if not df_real_trades.empty and "Estimated_Profit_SEK" in df_real_trades.columns 
        else 0.0
    )
    st.metric(label="Est. Profit", value=f"{executed_profits:.2f} SEK")

with col3:
    test_count = len(df_test_trades)
    st.metric(label="🧪 Test Trades", value=test_count)

st.divider()

# App Navigation Tabs
tab1, tab2, tab3 = st.tabs(["🧪 Test", " Real", "📊 Signals"])

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
        st.markdown("### Latest Signals")
        # Visa de senaste signalerna som snygga kort istället för en bred tabell
        for index, row in df_signals.tail(5).iterrows():
            with st.container():
                st.markdown(f"""
                - **Asset:** {row.get('Asset', 'N/A')}  
                  **Status:** `{row.get('Status', 'N/A')}`  
                  **Time:** {row.get('Timestamp', 'N/A')}
                """)
                st.markdown("---")
        with st.expander("Visa alla signalloggar"):
            st.dataframe(df_signals, use_container_width=True)
    else:
        st.info("No signal summary logs available yet.")
        
