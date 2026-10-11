import streamlit as st
import pandas as pd
import os

# Page configuration
st.set_page_config(
    page_title="IMMENSITY AURORA",
    page_icon="⚡",
    layout="centered"
)

# Custom Soft White, Purple & Blue UI with Robust Tab Styling
st.markdown("""
<style>
/* Background - White to Soft Purple/Blue Gradient */
.stApp {
    background: linear-gradient(180deg, #ffffff 0%, #f4f3ff 50%, #eff6ff 100%);
    color: #0f172a;
}

/* Header Styling */
h1 {
    font-weight: 800 !important;
    letter-spacing: -0.5px;
    background: linear-gradient(90deg, #8b5cf6, #3b82f6);
    -webkit-background-clip: text;
    -webkit-text-fill-color: transparent;
    font-size: 1.8rem !important;
    margin-bottom: 0px !important;
}

.stCaption {
    color: #6366f1 !important;
    font-weight: 600;
    font-size: 0.8rem !important;
}

/* Metric Cards - Clean White Glass */
div[data-testid="stMetric"] {
    background: rgba(255, 255, 255, 0.9);
    border: 1px solid #e0e7ff;
    border-radius: 20px;
    padding: 16px;
    box-shadow: 0 8px 20px -5px rgba(99, 102, 241, 0.08);
    backdrop-filter: blur(10px);
}

div[data-testid="stMetricLabel"] {
    color: #4f46e5 !important;
    font-weight: 700 !important;
    font-size: 0.75rem !important;
    text-transform: uppercase;
    letter-spacing: 0.5px;
}

div[data-testid="stMetricValue"] {
    color: #1e1b4b !important;
    font-weight: 800 !important;
    font-size: 1.6rem !important;
}

/* Robust Pill Tabs Styling Overhaul */
.stTabs {
    background: transparent;
}

.stTabs [data-baseweb="tab-list"] {
    display: flex;
    gap: 8px;
    background: rgba(238, 242, 255, 0.8);
    padding: 6px;
    border-radius: 30px;
    border: 1px solid #e0e7ff;
}

.stTabs [data-baseweb="tab"] {
    height: 40px;
    background-color: transparent !important;
    border-radius: 24px !important;
    color: #4338ca !important;
    font-weight: 700;
    font-size: 0.82rem;
    padding: 0px 20px;
    border: none !important;
    box-shadow: none !important;
}

.stTabs [aria-selected="true"] {
    background: linear-gradient(135deg, #a855f7 0%, #3b82f6 100%) !important;
    color: #ffffff !important;
    box-shadow: 0 4px 14px rgba(168, 85, 247, 0.3) !important;
}

/* Remove default indicators and lines */
.stTabs [data-baseweb="tab-highlight"] {
    display: none !important;
}

.stTabs [data-baseweb="tab-border"] {
    display: none !important;
}

/* Rounded Tables Container */
div[data-testid="stDataFrame"] {
    border: 1px solid #e0e7ff;
    border-radius: 20px;
    overflow: hidden;
    background: #ffffff;
    box-shadow: 0 4px 15px rgba(0, 0, 0, 0.02);
}

/* Soft Cards for Signal Activity */
.soft-card {
    background: #ffffff;
    border-radius: 18px;
    padding: 14px 18px;
    margin-bottom: 10px;
    border: 1px solid #e0e7ff;
    box-shadow: 0 4px 12px rgba(99, 102, 241, 0.05);
    display: flex;
    justify-content: space-between;
    align-items: center;
}
</style>
""", unsafe_allow_html=True)

st.title("⚡ IMMENSITY AURORA")
st.caption("ENGINE | CAPITAL BUDGET: 1,000.00 SEK")

# Load logs safely
trade_log_path = "trade_log.csv"
signal_summary_path = "signal_summary_log.csv"

df_logs = pd.read_csv(trade_log_path) if os.path.exists(trade_log_path) else pd.DataFrame()
df_signals = pd.read_csv(signal_summary_path) if os.path.exists(signal_summary_path) else pd.DataFrame()

df_test_trades = df_logs if not df_logs.empty else pd.DataFrame()
df_real_trades = pd.DataFrame(columns=df_logs.columns) if not df_logs.empty else pd.DataFrame()

st.write("")

# Metrics Row
col1, col2, col3 = st.columns(3)

with col1:
    st.metric(label="Real Trades", value=len(df_real_trades))

with col2:
    executed_profits = (
        df_real_trades[df_real_trades["Status"].astype(str).str.contains("Executed")]["Estimated_Profit_SEK"].sum() 
        if not df_real_trades.empty and "Estimated_Profit_SEK" in df_real_trades.columns 
        else 0.0
    )
    st.metric(label="Est. Profit", value=f"{executed_profits:.2f} SEK")

with col3:
    st.metric(label="Test Trades", value=len(df_test_trades))

st.write("")

# Rounded Tabs
tab1, tab2, tab3 = st.tabs(["🧪 Test Trades", "💼 Real Trades", "📊 Signal Summary"])

with tab1:
    if not df_test_trades.empty:
        st.dataframe(df_test_trades, use_container_width=True, hide_index=True)
    else:
        st.info("No test trades available.")

with tab2:
    if not df_real_trades.empty:
        st.dataframe(df_real_trades, use_container_width=True, hide_index=True)
    else:
        st.info("No real trades executed yet.")

with tab3:
    if not df_signals.empty:
        st.markdown("##### Recent Events")
        for _, row in df_signals.tail(5).iterrows():
            status = str(row.get('Status', 'N/A'))
            status_bg = "#f3e8ff" if "Executed" in status else "#eff6ff"
            status_color = "#7e22ce" if "Executed" in status else "#2563eb"
            
            st.markdown(f"""
                <div class="soft-card">
                    <div>
                        <strong style="color: #0f172a; font-size: 0.95rem;">{row.get('Asset', 'N/A')}</strong><br>
                        <span style="color: #94a3b8; font-size: 0.75rem;">{row.get('Timestamp', 'N/A')}</span>
                    </div>
                    <div>
                        <span style="background: {status_bg}; color: {status_color}; border: 1px solid #e0e7ff; padding: 5px 12px; border-radius: 14px; font-size: 0.75rem; font-weight: 700;">{status}</span>
                    </div>
                </div>
            """, unsafe_allow_html=True)
            
        st.write("")
        with st.expander("Show full signal log"):
            st.dataframe(df_signals, use_container_width=True, hide_index=True)
    else:
        st.info("No signal summary logs available.")
        
