import streamlit as st

import time

from backend.jobs import start_job, jobs, logs
from tabs.tab_dacphysics import dacphysics_tab
from tabs.tab_HVramp import HVramp_tab

st.set_page_config(layout="wide", page_title='HypeX Operations')
st.title("HypeX Operations UI v0.1")


# -------------------------
# MAIN FUNCTIONAL TABS (1) Calculator (2) HV Ramp (2) Data Interpreation/Morph (3) Analysis
# -------------------------
tab_dacphysics, tab_HVramp, tab_interpret, tab_analysis = st.tabs([
    "Calculator",
    "HV Ramp-Up/Down procedure",
    "Data Interpretation",
    "Analysis"
])


with tab_dacphysics:
    dacphysics_tab()

with tab_HVramp:
    HVramp_tab()

with tab_interpret:
    st.subheader("Data Interpretation")
    if st.button("Run interpretation"):
        start_job(
            name="interpretation",
            target=run_interpretation,
            input_path="raw.dat",
            output_path="interpreted.txt"
        )

with tab_analysis:
    st.subheader("Analysis")
    if st.button("Run analysis"):
        start_job(
            name="analysis",
            target=run_analysis,
            parquet_path="data.parquet",
            clustering_gap=50
        )

# -------------------------
# PERSISTENT CLI / STATUS
# -------------------------
st.markdown("---")

with st.expander("Background Jobs & Logs", expanded=True):

    st.markdown("### Jobs")
    if not jobs:
        st.write("No jobs running.")
    else:
        for name, info in jobs.items():
            st.write(f"**{name}** → {info['status']}")

    st.markdown("### Logs")
    if logs:
        st.code("\n".join(logs[-15:]))
    else:
        st.write("No logs yet.")

# Simple refresh loop for cli updates
time.sleep(0.5)
st.rerun()