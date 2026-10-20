# 🌌 IMMENSITY AURORA

Algorithmic decision-support trading and macro intelligence framework designed for automated multi-layer scanning, signal aggregation, and portfolio tracking.

[![Open in Streamlit](https://static.streamlit.io/badges/streamlit_badge_black_white.svg)](https://immensity-aurora.streamlit.app/)

---

## 🚀 Overview

**IMMENSITY AURORA** combines Google Sheets backend logic (`IMMENSITY_AURORA_V1`), Python scripts, Make.com workflows, and Streamlit dashboards to process macro signals, sentiment analysis, and market trends in real-time. The system runs 24/7 via automated GitHub Actions cron workflows.

---

## 📊 System Architecture

* **Dashboard & Database:** Google Sheets core containing Master Signals and Leads Matrix.
* **Automation Engine:** Scheduled GitHub Actions workflows executing continuous macro health checks and data ingestion.
* **Frontend UI:** Streamlit-based interactive web dashboard for real-time monitoring and decision support.

---

## 🛠️ How to run it on your own machine

Prerequisite: install `uv` if you don't already have it.

```bash
$ curl -LsSf [https://astral.sh/uv/install.sh](https://astral.sh/uv/install.sh) | sh
