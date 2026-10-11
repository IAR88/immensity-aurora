import streamlit as st
import pandas as pd
import os

# Page configuration
st.set_page_config(
    page_title="IMMENSITY AURORA",
    page_icon="⚡",
    layout="centered"
)

# Custom Soft White, Purple & Blue UI
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

/* Fully Rounded Pill Tabs (White, Purple & Blue) */
.stTabs [data-baseweb="tab-list"] {
    gap: 8px;
    background: rgba(238, 242, 255, 0.8);
    padding: 6px;
    border-radius: 30px;
    border: 1px solid #e0e7ff;
}

.stTabs [data-baseweb="tab"] {
    border-radius: 24px !important;
    color: #4338ca;
    font-weight: 700;
    font-size: 0.82rem;
    padding: 8px 18px;
    border: none !important;
    transition: all 0.2s ease-in-out;
}

.stTabs [aria-selected="true"] {
    background: linear-gradient(135deg, #a855f7 0%, #3b82f6 100%) !important;
    color: #ffffff !important;
    box-shadow: 0 4px 14px rgba(168, 85, 247, 0.3);
}

/* Hide Red Line Under Active Tab */
.stTabs [data-baseweb="tab-highlight-title"] {
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

df_logs = pd.read_csv(trade_log_path) if os.path.exists(trade_log_path)
