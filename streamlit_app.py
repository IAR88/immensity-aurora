import streamlit as st
import pandas as pd
import os

# Page configuration
st.set_page_config(
    page_title="IMMENSITY AURORA",
    page_icon="⚡",
    layout="centered"
)

# Professional Light Purple / Glassmorphism UI
st.markdown("""
    <style>
    /* Global Background */
    .stApp {
        background-color: #f8fafc;
        color: #0f172a;
    }

    /* Modern Title */
    h1 {
        font-weight: 800 !important;
        letter-spacing: -0.5px;
        color: #581c87 !important;
        font-size: 1.8rem !important;
        margin-bottom: 0px !important;
    }

    /* Subtitle / Caption */
    .stCaption {
        color: #6b21a8 !important;
        font-weight: 500;
        font-size: 0.85rem !important;
    }

    /* Metric Cards */
    div[data-testid="stMetric"] {
        background: #ffffff;
        border: 1px solid #f1f5f9;
        border-radius: 12px;
        padding: 14px 16px;
        box-shadow: 0 4px 6px -1px rgba(0, 0, 0, 0.03), 0 2px 4px -2px rgba(0, 0, 0, 0.03);
    }

    div[data-testid="stMetricLabel"] {
        color: #64748b !important;
        font-weight: 600 !important;
        font-size: 0.75rem !important;
        text-transform: uppercase;
        letter-spacing: 0.5px;
    }

    div[data-testid="stMetricValue"] {
        color: #3b0764 !important;
        font-weight: 700 !important;
        font-size: 1.5rem !important;
    }

    /* Minimalist Cards for Signals */
    .signal-card {
        background: #ffffff;
        border-radius: 10px;
        padding: 12px 16px;
        margin-bottom: 8px;
        border: 1px solid #e2e8f0;
        box-shadow: 0 1px 3px rgba(0, 0, 0, 0.02);
        display: flex;
        justify-content: space-between;
        align-items: center;
    }

    /* Tabs Styling */
    .stTabs [data-baseweb="tab-list"] {
        gap: 6px;
        background-color: #f1f5f9;
        padding: 4px;
        border-radius: 10px;
    }

    .stTabs [data-baseweb="tab"] {
        border-radius: 8px;
        color: #64748b;
        font-weight: 600;
        font-size: 0.85rem;
        padding: 8px 16px;
    }

    .stTabs [aria-selected="true"] {
        background-color: #ffffff !important;
        color: #6b21a8 !important;
        box-shadow: 0 1px 3px rgba(0,0,0,0.05);
    }

    /* Tables Clean Up */
    div[data-testid="stDataFrame"] {
        border: 1px solid #e2e8f0;
        border-radius: 10px;
        overflow: hidden;
    }
    </style>
""", unsafe_allow_html=True)

st.title("⚡ IMMENSITY AURORA")
st.caption("Engine | Capital Budget: 1,000.00 SEK")

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

# Main Tabs
tab1, tab2, tab3 = st.tabs(["🧪 Test Trades", "💼 Real Trades", "📊 Signal Summary"])

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
        st.markdown("##### Recent Activity")
        for _, row in df_signals.tail(5).iterrows():
            status = str(row.get('Status', 'N/A'))
            status_color = "#9333ea" if "Executed" in status else "#d97706"
            
            st.markdown(f"""
                <div class="signal-card">
                    <div>
                        <strong style="color: #0f172a; font-size: 0.95rem;">{row.get('Asset', 'N/A')}</strong><br>
                        <span style="color: #94a3b8; font-size: 0.75rem;">{row.get('Timestamp', 'N/A')}</span>
                    </div>
                    <div>
                        <span style="background: #faf5ff; color: {status_color}; border: 1px solid #f3e8ff; padding: 3px 8px; border-radius: 6px; font-size: 0.75rem; font-weight: 600;">{status}</span>
                    </div>
                </div>
            """, unsafe_allow_html=True)
            
        st.write("")
        with st.expander("Show full signal log"):
            st.dataframe(df_signals, use_container_width=True)
    else:
        st.info("No signal summary logs available.")
        
