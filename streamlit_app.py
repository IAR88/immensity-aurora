import streamlit as st
import pandas as pd
import os

# Page configuration
st.set_page_config(
    page_title="IMMENSITY AURORA",
    page_icon="⚡",
    layout="centered"
)

# Soft Lavender Light UI
st.markdown("""
    <style>
    /* Background */
    .stApp {
        background: linear-gradient(180deg, #fbfbfe 0%, #f3f0ff 100%);
        color: #1e1b4b;
    }

    /* Title */
    h1 {
        font-weight: 800 !important;
        letter-spacing: -0.5px;
        background: linear-gradient(90deg, #a855f7, #c084fc);
        -webkit-background-clip: text;
        -webkit-text-fill-color: transparent;
        font-size: 2rem !important;
        margin-bottom: 2px !important;
    }

    /* Subtitle */
    .stCaption {
        color: #7e22ce !important;
        font-weight: 600;
        font-size: 0.85rem !important;
        opacity: 0.8;
    }

    /* Metric Cards */
    div[data-testid="stMetric"] {
        background: rgba(255, 255, 255, 0.9);
        border: 1px solid #f3e8ff;
        border-radius: 16px;
        padding: 16px;
        box-shadow: 0 10px 20px -5px rgba(192, 132, 252, 0.1);
        backdrop-filter: blur(12px);
    }

    div[data-testid="stMetricLabel"] {
        color: #6b21a8 !important;
        font-weight: 700 !important;
        font-size: 0.75rem !important;
        text-transform: uppercase;
        letter-spacing: 0.8px;
    }

    div[data-testid="stMetricValue"] {
        color: #4c1d95 !important;
        font-weight: 800 !important;
        font-size: 1.6rem !important;
    }

    /* Signal Cards */
    .signal-card {
        background: #ffffff;
        border-radius: 14px;
        padding: 14px 18px;
        margin-bottom: 10px;
        border: 1px solid #f3e8ff;
        box-shadow: 0 4px 12px rgba(168, 85, 247, 0.05);
        display: flex;
        justify-content: space-between;
        align-items: center;
    }

    /* Tabs Styling */
    .stTabs [data-baseweb="tab-list"] {
        gap: 8px;
        background-color: rgba(243, 232, 255, 0.6);
        padding: 6px;
        border-radius: 14px;
    }

    .stTabs [data-baseweb="tab"] {
        border-radius: 10px;
        color: #6b21a8;
        font-weight: 700;
        font-size: 0.85rem;
        padding: 8px 16px;
    }

    .stTabs [aria-selected="true"] {
        background-color: #ffffff !important;
        color: #9333ea !important;
        box-shadow: 0 4px 12px rgba(168, 85, 247, 0.15);
    }

    /* Dataframe Table Clean */
    div[data-testid="stDataFrame"] {
        border:
        
