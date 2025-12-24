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
    "step": list(range(1, 64)),
    "Vgrid_CH3": [
        0, 50, 100, 150, 200, 250, 275, 300, 325, 325, 325, 325, 325, 325, 325, 
        325, 325, 325, 325, 325, 325, 325, 325, 325, 325, 325, 325, 325, 335, 
        345, 355, 365, 375, 385, 395, 405, 410, 410, 410, 410, 410, 410, 410, 
        410, 410, 410, 410, 410, 410, 410, 410, 410, 410, 410, 410, 410, 410, 
        410, 410, 410, 410, 410, 410
    ],
    "Vanode_CH2": [
        0, 50, 100, 150, 200, 250, 275, 300, 325, 325, 325, 325, 325, 325, 325, 
        325, 325, 325, 325, 325, 325, 325, 325, 325, 325, 325, 325, 325, 335,
          345, 355, 365, 375, 385, 395, 405, 410, 420, 430, 440, 450, 460, 460, 
          460, 460, 460, 460, 460, 460, 460, 460, 460, 460, 460, 460, 460, 460, 
          460, 460, 470, 480, 490, 510
    ],
    "Vcathode_CH1": [
        50, 100, 150, 200, 250, 300, 325, 350, 375, 425, 475, 525, 575, 625, 675,
        725, 775, 825, 875, 925, 975, 1025, 1075, 1125, 1175, 1225, 1275, 1325, 1325, 1325, 
        1325, 1325, 1325, 1325, 1325, 1325, 1325, 1375, 1425, 1475, 1525, 1575, 1625, 1675, 
        1725, 1775, 1825, 1875, 1925, 2025, 2075, 2125, 2175, 2225, 2275, 2325, 2375, 
        2425, 2460, 2480, 2480, 2490, 2510
    ]
}

LOG_DIR = "hv_ramp_logs"
Path(LOG_DIR).mkdir(parents=True, exist_ok=True)#Make log directory if not present

def log_event(step, channel, checked, vgrid, vanode, vcathode, note=""):
    if not st.session_state.get("logging_active", False):
        return  # Do nothing if logging not started
    
    log_file = st.session_state.get("log_file")
    if log_file is None:
        return
    
    file_exists = Path(log_file).exists()

    with open(log_file, "a", newline="") as f:
        writer = csv.writer(f)

        writer.writerow([
            datetime.now().isoformat(timespec="seconds"),
            f"{step:02d}",
            channel,
            checked,
            vgrid,
            vanode,
            vcathode,
            note
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
def HVramp_tab():

    if "fine_tune_rows" not in st.session_state:#Initialize fine tune rows list if not present
        st.session_state.fine_tune_rows = []
    
    #UI Log
    st.subheader("HV Ramp Logbook")

    operator = st.text_input("Operator name(s). Ex: Alice Bob, Dinkan Pankila")
    setup_info = st.text_area(
        "Setup info Ex: WGW15-G6, ArDME8020, 160-40ccpm, 1.1 bar",
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

        with open(log_file, "w", newline="") as f:#Open new file and write header info. Later to be incorporated into log_event() function by pasing additonal arguments setup_info and operator
            f.write("# HV Ramp Log\n")
            f.write(f"# Start time: {datetime.now().isoformat(timespec='seconds')}\n")
            f.write(f"# Operator(s): {operator}\n")
            f.write(f"# Setup: {setup_info.replace(chr(10), ' | ')}\n")
            f.write("#\n")
            f.write("timestamp,step,channel,checked,Vgrid,Vanode,Vcathode,Note\n")

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

    st.markdown(
        """
        **Rule:**  
        - Check CH3, CH2, CH1 in any order. Step complete only when all 3 are checked.  
        - Reverse order for Ramp-Down.  
        """
    )

    # UI Ramp-up
    # ---- header ----
    header = st.columns([1, 2.5, 2.5, 2.5, 1, 3, 1])
    header[0].markdown("**Step**")
    header[1].markdown("**CH3 (Vgrid)**")
    header[2].markdown("**CH2 (Vanode)**")
    header[3].markdown("**CH1 (Vcathode)**")
    header[4].markdown("**State**")
    header[5].markdown("**Note**")
    # header[6].markdown("**Add Log to note**")

    st.divider()

    # ---- steps ----
    for i, step in enumerate(hv_steps["step"]):
        cols = st.columns([1, 2.5, 2.5, 2.5, 1, 3, 1])

        with cols[0]:#Step number
            st.markdown(f"**{step:02d}**")
            

        with cols[1]:# CH3 Grid
            key_ch3 = f"s{step:02d}_ch3"
            ch3 = st.checkbox(
                f"CH3 → **{hv_steps['Vgrid_CH3'][i]} V**",
                key=f"s{step:02d}_ch3",
                disabled=not edit_enabled
            )
            handle_checkbox_change(
                key_ch3, step, "CH3", ch3, i
            )

        with cols[2]:# CH2 Anode
            key_ch2 = f"s{step:02d}_ch2"
            ch2 = st.checkbox(
                f"CH2 → **{hv_steps['Vanode_CH2'][i]} V**",
                key=f"s{step:02d}_ch2",
                disabled=not edit_enabled
            )
            handle_checkbox_change(
                key_ch2, step, "CH2", ch2, i
            )

        with cols[3]:#CH1 Cathode
            key_ch1 = f"s{step:02d}_ch1"
            ch1 = st.checkbox(
                f"CH1 → **{hv_steps['Vcathode_CH1'][i]} V**",
                key=f"s{step:02d}_ch1",
                disabled=not edit_enabled
            )
            handle_checkbox_change(
                key_ch1, step, "CH1", ch1, i
            )

        # ---- compute state and change bulb color----
        n_on = sum([ch1, ch2, ch3])

        with cols[4]:
            st.markdown(f"### {bulb(n_on)}")
        
        with cols[5]:# Note entry
            note_key = f"s{step:02d}_note"
            note_text = cols[5].text_input(
                " ",
                key=note_key,
                placeholder="Enter note...",
                disabled=not edit_enabled
            )

        with cols[6]:#Log button
            if cols[6].button("Log Note 📝", key=f"s{step:02d}_lognote", disabled=not edit_enabled):
                if not st.session_state.logging_active:
                    st.error(
                        "🚫 Logging is not active. Turn it on from the top to log notes."
                    )
                else:    
                    log_event(
                        step=step,
                        channel="NOTE",
                        checked=(n_on == 3),# if all three channels in this step are checked
                        vgrid=hv_steps["Vgrid_CH3"][i],
                        vanode=hv_steps["Vanode_CH2"][i],
                        vcathode=hv_steps["Vcathode_CH1"][i],
                        note=note_text
                    )
                    st.toast(f"Note logged for step {step:02d}!", icon="📝")

        st.divider()

    #Fine tuning section after ramp (outside the loop for 31 steps)
    st.subheader("🔧 Post-Ramp Fine Tuning (Ramp-Up only)")
    fine_tune_enabled = (# Enavle fine tuning only if editing and logging are active
        edit_enabled
        and st.session_state.logging_active
    )

    # ---- show logged FT rows ----
    for idx, ft in enumerate(st.session_state.fine_tune_rows):
        cols = st.columns([1, 2.5, 2.5, 2.5, 3, 1])
        cols[0].markdown(f"**FT{idx+1}**")# Fine tune step started from 1
        cols[1].markdown(f"{ft['vgrid']} V")
        cols[2].markdown(f"{ft['vanode']} V")
        cols[3].markdown(f"{ft['vcathode']} V")
        cols[4].markdown(ft["note"])
        cols[5].markdown("✅")
    
    if fine_tune_enabled:
        cols = st.columns([1, 2.5, 2.5, 2.5, 3, 1])
        cols[0].markdown("**FineT**")

        # Manual voltage inputs
        vgrid_ft = cols[1].number_input(
            "CH3 (Vgrid)",
            value=hv_steps["Vgrid_CH3"][-1],
            step=5,
            disabled=not fine_tune_enabled,
            key="ft_vgrid"
        )

        vanode_ft = cols[2].number_input(
            "CH2 (Vanode)",
            value=hv_steps["Vanode_CH2"][-1],
            step=5,
            disabled=not fine_tune_enabled,
            key="ft_vanode"
        )

        vcathode_ft = cols[3].number_input(
            "CH1 (Vcathode)",
            value=hv_steps["Vcathode_CH1"][-1],
            step=5,
            disabled=not fine_tune_enabled,
            key="ft_vcathode"
        )

        # Notes
        note_ft = cols[4].text_input(
            "Note",
            placeholder="Describe tweak / observation",
            disabled=not fine_tune_enabled,
            key="ft_note"
        )

        # Log button
        if cols[5].button(
            "Confirm and Log Fine Tune Step 📝",
            disabled=not fine_tune_enabled,
            help="Log manual HV fine-tuning step"
        ):
            entry = {#Make a dictionary entry for the current fine tune log entry
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "vgrid": vgrid_ft,
            "vanode": vanode_ft,
            "vcathode": vcathode_ft,
            "note": note_ft
            }
            st.session_state.fine_tune_rows.append(entry)#Store the fine tune row log entry in session state list

            log_event(#Log event to csv
                step=len(st.session_state.fine_tune_rows),
                channel="FINETUNE",
                checked=None,
                vgrid=vgrid_ft,
                vanode=vanode_ft,
                vcathode=vcathode_ft,
                note=note_ft
            )
            st.toast("HV fine tune logged", icon="📝")#Display a short success message for csv save
            st.session_state.ft_note_active = ""                