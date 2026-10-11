import streamlit as st
import pandas as pd
import os

# Page configuration
st.set_page_config(
    page_title="IMMENSITY AURORA",
    page_icon="⚡",
    layout="wide"
)

st.title("⚡ IMMENSITY AURORA | Trading Engine Dashboard")
st.markdown("Engine | Capital Budget: 1000 SEK")

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

# Metrics overview
col1, col2, col3 = st.columns(3)

with col1:
    total_trades = len(df_logs) if not df_logs.empty else 0
    st.metric("Total Trades Logged", total_trades)

with col2:
    executed_profits = (
        df_logs[df_logs["Status"].astype(str).str.contains("Executed")]["Estimated_Profit_SEK"].sum() 
        if not df_logs.empty and "Estimated_Profit_SEK" in df_logs.columns 
        else 0.0
    )
    st.metric("Estimated Profit (SEK)", f"{executed_profits:.2f} SEK")

with col3:
    active_signals = len(df_signals) if not df_signals.empty else 0
    st.metric("Total Signals Processed", active_signals)

st.divider()

# Display logs
st.subheader("Trade Logs")
if not df_logs.empty:
    st.dataframe(df_logs, use_container_width=True)
else:
    st.info("No trade logs available yet.")

st.subheader("Signal Summary Logs")
if not df_signals.empty:
    st.dataframe(df_signals, use_container_width=True)
else:
    st.info("No signal summary logs available yet.")
tory Changes (50%)")
        st.progress(0.90, text="P18: FX & Fee Dynamics (90%)")

with tab3:
    st.subheader("Execution History Log")
    st.dataframe(df_logs, use_container_width=True)

with tab4:
    st.subheader("Swedish Tax Agency (Annex K4) & EU DAC8 Compliance")
    st.markdown("Export audit logs formatted for Swedish tax filing and European Union DAC8 transparency requirements.")

    csv_data = generate_tax_export(df_logs)

    st.download_button(
        label="📥 Export Annex K4 / DAC8 Audit File (CSV)",
        data=csv_data,
        file_name=f"immensity_aurora_tax_report_{datetime.now().strftime('%Y%m%d')}.csv",
        mime="text/csv",
    )

    st.info("All transaction values are converted and stored in SEK with complete timestamps to comply with Skatteverket guidelines.")
