import streamlit as st
import csv
from pathlib import Path
from datetime import datetime

# Return a colored bulb based on number of channels on
def bulb(n_on: int) -> str:
    if n_on == 0:
        return "🔴"
    elif n_on == 1:
        return "🟡"
    elif n_on == 2:
        return "🟠"
    else:
        return "🟢"


#Dictionary of HV steps   
hv_steps = {
    "step": list(range(1, 32)),
    "Vgrid_CH3": [
        0, 50, 100, 150, 200, 250, 275, 300, 325, 325, 325, 325, 325, 325, 325, 
        325, 325, 325, 325, 325, 325, 325, 325, 325, 325, 325, 325, 325, 335, 345, 355
    ],
    "Vanode_CH2": [
        0, 50, 100, 150, 200, 250, 275, 300, 325, 325, 325, 325, 325, 325, 325, 
        325, 325, 325, 325, 325, 325, 325, 325, 325, 325, 325, 325, 325, 335, 345, 355
    ],
    "Vcathode_CH1": [
        50, 100, 150, 200, 250, 300, 325, 350, 375, 425, 475, 525, 575, 625, 675,
        725, 775, 825, 875, 925, 975, 1025, 1075, 1125, 1175, 1225, 1275, 1325, 1325, 1325, 1325
    ]
}

LOG_DIR = "hv_ramp_logs"

def log_event(step, channel, checked, vgrid, vanode, vcathode):
    if not st.session_state.get("logging_active", False):
        return  # Do nothing if logging not started
    
    log_file = st.session_state.get("log_file")
    if log_file is None:
        return
    
    file_exists = Path(log_file).exists()

    with open(log_file, "a", newline="") as f:
        writer = csv.writer(f)

        if not file_exists:
            writer.writerow([
                "timestamp",
                "step",
                "channel",
                "checked",
                "Vgrid(CH3)",
                "Vanode(CH2)",
                "Vcathode(CH1)"
            ])

        writer.writerow([
            datetime.now().isoformat(timespec="seconds"),
            f"{step:02d}",
            channel,
            checked,
            vgrid,
            vanode,
            vcathode
        ])

#For logging, log when a step is completed (tracked by return variable of checkbox for each channel). Streamlit runs fresh on each interaction, mandatory to store previous state to track changes.
def handle_checkbox_change(key, step, channel, checked, i):
    prev_key = f"{key}_prev"# make a key for previous state from current session state

    if prev_key not in st.session_state:# If previous state key not present, inititalize it to current state (first time)
        st.session_state[prev_key] = checked
        return

    if st.session_state[prev_key] != checked:#If previous state key not equal to current state, log the event and update previous state key variable
        #Log the event
        log_event(
            step=step,
            channel=channel,
            checked=checked,
            vgrid=hv_steps["Vgrid_CH3"][i],
            vanode=hv_steps["Vanode_CH2"][i],
            vcathode=hv_steps["Vcathode_CH1"][i],
        )
        st.session_state[prev_key] = checked

# Show UI

# UI Ramp-up
def HVramp_tab():
    
    #UI Log
    st.subheader("HV Ramp Logbook")

    operator = st.text_input("Operator name(s). Ex: Alice Bob, Dinkan Pankila")
    setup_info = st.text_area(
        "Setup info Ex: WGW15-G6|ArDME8020|160-40gcmm|1.1Bar",
        height=80
    )
    
    # ---- start / stop logging ----
    if "logging_active" not in st.session_state:
        st.session_state.logging_active = False

    if "log_file" not in st.session_state:
        st.session_state.log_file = None

    col1, col2 = st.columns(2)

    with col1:
        start_logging = st.button("▶ Start logging", disabled=st.session_state.logging_active)

    with col2:
        stop_logging = st.button("■ Stop logging", disabled=not st.session_state.logging_active)

    if start_logging:#If start logging button is pressed
        ts = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")#Record the timestamp
        log_file = f"{LOG_DIR}/hv_log_{ts}.csv"#Logfile name with the timestamp

        with open(log_file, "w", newline="") as f:#Open new file and write header info
            f.write("# HV Ramp Log\n")
            f.write(f"# Start time: {datetime.now().isoformat(timespec='seconds')}\n")
            f.write(f"# Operator(s): {operator}\n")
            f.write(f"# Setup: {setup_info.replace(chr(10), ' | ')}\n")
            f.write("#\n")
            f.write("timestamp,step,channel,checked,Vgrid,Vanode,Vcathode\n")

        st.session_state.log_file = log_file
        st.session_state.logging_active = True
        st.success(f"Logging started: {log_file}")

    if stop_logging:
        st.session_state.logging_active = False
        st.success("Logging stopped")


    st.subheader("HV Ramp Procedure")

    edit_enabled = st.toggle(
        "🔓 Enable editing (HV operation in progress)",
        value=False
    )

    mode = st.radio(
        "Operation mode",
        ["Ramp-Up", "Ramp-Down"],
        horizontal=True
    )

    st.markdown(
        """
        **Rule:**  
        - Set CH3, CH2, CH1 in any order  
        - Step complete only when all 3 are checked  
        - Reverse order for Ramp-Down  
        """
    )

    # ---- header ----
    header = st.columns([1, 2.5, 2.5, 2.5, 1])
    header[0].markdown("**Step**")
    header[1].markdown("**CH3 (Vgrid)**")
    header[2].markdown("**CH2 (Vanode)**")
    header[3].markdown("**CH1 (Vcathode)**")
    header[4].markdown("**State**")

    st.divider()

    # ---- steps ----
    for i, step in enumerate(hv_steps["step"]):
        cols = st.columns([1, 2.5, 2.5, 2.5, 1])

        with cols[0]:
            st.markdown(f"**{step:02d}**")
            

        with cols[1]:
            key_ch3 = f"s{step:02d}_ch3"
            ch3 = st.checkbox(
                f"CH3 → **{hv_steps['Vgrid_CH3'][i]} V**",
                key=f"s{step:02d}_ch3",
                disabled=not edit_enabled
            )
            handle_checkbox_change(
                key_ch3, step, "CH3", ch3, i
            )

        with cols[2]:
            key_ch2 = f"s{step:02d}_ch2"
            ch2 = st.checkbox(
                f"CH2 → **{hv_steps['Vanode_CH2'][i]} V**",
                key=f"s{step:02d}_ch2",
                disabled=not edit_enabled
            )
            handle_checkbox_change(
                key_ch2, step, "CH2", ch2, i
            )

        with cols[3]:
            key_ch1 = f"s{step:02d}_ch1"
            ch1 = st.checkbox(
                f"CH1 → **{hv_steps['Vcathode_CH1'][i]} V**",
                key=f"s{step:02d}_ch1",
                disabled=not edit_enabled
            )
            handle_checkbox_change(
                key_ch1, step, "CH1", ch1, i
            )

        # ---- compute state ----
        n_on = sum([ch1, ch2, ch3])

        with cols[4]:
            st.markdown(f"### {bulb(n_on)}")

        st.divider()