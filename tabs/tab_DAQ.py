''' This tab is dedicated to control of HV and data acquistion to minimize user interention'''

import streamlit as st
import time
from backend.hvlogic import DetectorHV, execute_daq_sequence_step
from backend.jobs import start_job
from backend.safety_rules import calculate_safe_trajectory

def init_daq_state():
    """Initialize the session state variables needed for Hardware State"""
    if "hv_unit" not in st.session_state:
        st.session_state.hv_unit = DetectorHV("192.168.0.1")
        st.session_state.is_connected = False
    """Initialize the session state variables needed for the DAQ sequencer."""
    if "daq_steps" not in st.session_state:
        st.session_state.daq_steps = []  # List of dictionaries holding target voltages per step
    if "step_status" not in st.session_state:
        st.session_state.step_status = {} # Tracks state: 'idle', 'ramping', 'settled', 'daq_running', 'done'
    if "active_config" not in st.session_state:
        st.session_state.active_config = "3-Channel"

def DAQ_tab():
    init_daq_state()
    hv = st.session_state.hv_unit
    st.subheader("Semi-Automated HV and DAQ Sequencer")

    # -------------------------------------------------------------------------
    # 0. HARDWARE CONNECTION
    # -------------------------------------------------------------------------
    with st.expander("🔌 Hardware Connection", expanded=not st.session_state.is_connected):
        col_ip, col_btn = st.columns([3, 1])
        ip_addr = col_ip.text_input("Crate IP Address", value=hv.address, key="daq_ip_input")
        
        if not st.session_state.is_connected:
            if col_btn.button("Connect", use_container_width=True, type="primary", key="daq_connect_btn"):
                hv.address = ip_addr
                with st.spinner("Connecting and sensing hardware state..."):
                    if hv.connect():
                        st.session_state.is_connected = True
                        
                        # --- HARDWARE SENSING LOGIC ---
                        # 1. Force an immediate read of all physical telemetry
                        hv.read_all()
                        
                        # 2. Check if ANY channel in the crate is currently powered ON (PW == 1)
                        is_physically_on = any(data.get("PW", 0) == 1 for data in hv.live_data.values())
                        
                        # 3. Sync the Streamlit UI to match the physical reality
                        st.session_state.system_powered = is_physically_on
                        # -----------------------------------
                        
                        st.rerun()
                    else:
                        st.error("Failed to connect. Check network/IP.")
        else:
            if col_btn.button("Disconnect", use_container_width=True, key="daq_disconnect_btn"):
                with st.spinner("Ramping down for safety..."):
                    hv.shutdown() 
                hv.disconnect()
                st.session_state.is_connected = False
                st.session_state.system_powered = False # Reset UI state on disconnect
                st.rerun()

    # -------------------------------------------------------------------------
    # 1. ALWAYS-PRESENT GLOBAL PARAMETERS
    # -------------------------------------------------------------------------
    with st.container(border=True):
        st.markdown("**⚙️ Global DAQ Parameters**")
        
        # Row 1: Duration and Backup
        col1, col2 = st.columns(2)
        with col1:
            obs_time = st.number_input("Observation Duration (s)", min_value=1, value=3600, step=60)
        with col2:
            backup_path = st.text_input("Backup (DAC Settings) File Path", value="/home/Timepix3/backups/W30F11_20260513.TPX3")
    
        # Row 2: Calibration Files
        col3, col4 = st.columns(2)
        with col3:
            eq_file_path = st.text_input("Equalization File Path", value="/home/Timepix3/equalizations/W30-F11_equal_2026-05-11_18-16-35.h5")
        with col4:
            mask_file_path = st.text_input("Mask File Path", value="/home/Timepix3/masks/W30-F11_mask_2026-04-29_15-28-15.h5")

    # -------------------------------------------------------------------------
    # 2. CONFIGURATION & CHANNEL MAPPING
    # -------------------------------------------------------------------------
    st.markdown("### 1. Detector Configuration")
    config_choice = st.radio(
        "Select Layout:", 
        ["3-Channel (Grid, Anode, Window)", "5-Channel (Grid, Anode, FF1, FF2, Window)"],
        horizontal=True
    )
    
    # Define required logical names based on selection
    if "3-Channel" in config_choice:
        st.session_state.active_config = "3-Channel (1 cm Drift)"
        required_channels = ["Grid", "Anode", "Cathode"]
    else:
        st.session_state.active_config = "5-Channel (2-3  cm Drift)"
        required_channels = ["Grid", "Anode", "FF1", "FF2", "Window"]

    # Dynamic Channel Mapper
    st.markdown("**Map Physical CAEN Channels:**")
    map_cols = st.columns(len(required_channels))
    mapped_channels = {}
    
    # Assuming CAEN SY5527LC HV unit has 6 physical channels named Ch0 to Ch5
    # Initialize the default mapping in session state so it doesn't reset on clicks
    if "channel_map" not in st.session_state:
        st.session_state.channel_map = {name: f"Ch{i}" for i, name in enumerate(required_channels)}

    # Assuming your HV unit has 6 physical channels named Ch0 to Ch5
    all_hw_channels = [f"Ch{i}" for i in range(6)] 
    
    # We will build the new mapping here as we iterate
    mapped_channels = {}

    for i, logical_name in enumerate(required_channels):
        with map_cols[i]:
            # 1. Identify channels currently locked by OTHER dropdowns
            used_by_others = [
                ch for name, ch in st.session_state.channel_map.items() 
                if name != logical_name
            ]
            
            # 2. Available options = Total Pool minus Used Pool
            available_for_this = [ch for ch in all_hw_channels if ch not in used_by_others]
            
            # 3. Retrieve the current value (or fallback if the list shifted)
            current_val = st.session_state.channel_map.get(logical_name)
            if current_val not in available_for_this:
                current_val = available_for_this[0]
                
            # 4. Render the Selectbox
            selected_ch = st.selectbox(
                label=logical_name, 
                options=available_for_this, 
                index=available_for_this.index(current_val),
                key=f"dropdown_{logical_name}" # Unique key prevents Streamlit warnings
            )
            
            # Store the active selection
            mapped_channels[logical_name] = selected_ch

    # Overwrite the session state with the final selections for the next UI refresh
    st.session_state.channel_map = mapped_channels

    # This strips the "Ch" and converts "Ch0" to the integer 0 for hvlogic backend
    hv.channels = {name: int(ch_str.replace("Ch", "")) for name, ch_str in mapped_channels.items()}

    st.divider()

    # Block the rest of the UI if hardware isn't connected
    if not st.session_state.is_connected: 
        st.warning("⚠️ Please connect to the CAEN crate to use the sequencer.")
        return

    # -------------------------------------------------------------------------
    # 3. SEQUENCE BUILDER
    # -------------------------------------------------------------------------
    st.markdown("### 2. Sequence Builder")
    
    # Row to add a new step
    st.markdown("**Add New Step Targets (V):**")
    step_cols = st.columns(len(required_channels) + 1)
    
    new_step_targets = {}
    for i, logical_name in enumerate(required_channels):
        with step_cols[i]:
            new_step_targets[logical_name] = st.number_input(f"{logical_name} Target", value=0.0, step=10.0, key=f"target_{logical_name}")
            
    with step_cols[-1]:
        st.write("") # Spacing to align button with inputs
        st.write("")
        if st.button("➕ Add Step", type="primary", use_container_width=True):
            step_id = len(st.session_state.daq_steps)
            st.session_state.daq_steps.append(new_step_targets)
            st.session_state.step_status[step_id] = 'idle'
            st.rerun()

    st.divider()

    # -------------------------------------------------------------------------
    # 4. EXECUTION ENGINE
    # -------------------------------------------------------------------------
    st.markdown("### 3. Execution Engine")

    # --- MASTER POWER CONTROL ---
    with st.container(border=True):
        st.markdown("**Master Power Control**")
        pwr_col1, pwr_col2 = st.columns([2, 1])
        
        with pwr_col1:
            if not getattr(st.session_state, 'system_powered', False):
                if st.button("POWER ON SELECTED CHANNELS", use_container_width=True, type="primary"):
                    with st.spinner("Initializing hardware and sending Power ON commands..."):
                        # 1. Extract exactly which logical channels are currently configured
                        active_names = list(st.session_state.channel_map.keys())
                        
                        # 2. Command the backend to power ONLY those channels
                        hv.set_power(1, active_channel_names=active_names)
                        
                        st.session_state.system_powered = True
                        st.toast(f"Channels powered ON safely.", icon="✅")
                    st.rerun()
            else:
                if st.button("🔌 POWER OFF CHANNELS", use_container_width=True):
                    with st.spinner("Safely powering off active channels..."):
                        active_names = list(st.session_state.channel_map.keys())
                        hv.set_power(0, active_channel_names=active_names)
                        
                        st.session_state.system_powered = False
                        st.toast("Channels powered OFF.", icon="🛑")
                    st.rerun()
                    
        with pwr_col2:
            if getattr(st.session_state, 'system_powered', False):
                st.success("System is LIVE")
            else:
                st.info("System is OFF")

    st.divider()

    # Execution steps
    
    if len(st.session_state.daq_steps) == 0:
        st.info("Add steps above to build your sequence.")
        return

    # Display all steps and their controls
    for step_idx, step_data in enumerate(st.session_state.daq_steps):
        status = st.session_state.step_status.get(step_idx, 'idle')
        
        with st.container(border=True):
            st.markdown(f"**Step {step_idx + 1}**")
            
            # Display target summary
            targets_str = " | ".join([f"{k}: {v}V" for k, v in step_data.items()])
            st.caption(f"Targets: {targets_str}")
            
            ctrl_col1, ctrl_col2, status_col = st.columns([2, 2, 3])
            
            # 1. RAMP BUTTON
            with ctrl_col1:
                ramp_disabled = (status in ['ramping', 'settled', 'daq_running', 'done']) or (not getattr(st.session_state, 'system_powered', False))
                
                if st.button(f"📈 Ramp to Step {step_idx + 1}", key=f"ramp_{step_idx}", disabled=ramp_disabled, use_container_width=True):
                    # 1. Calculate the safe staging trajectory
                    trajectory = calculate_safe_trajectory(hv, step_data)
                    
                    # 2. Start the background sequence job
                    job_name = f"DAQ_Step_{step_idx}"
                    start_job(job_name, execute_daq_sequence_step, hv, trajectory)
                    
                    st.session_state.step_status[step_idx] = 'ramping'
                    st.rerun()

            # 2. DAQ BUTTON
            with ctrl_col2:
                # The visual trick: Use type="primary" to make it green/accented when ready
                is_ready = (status == 'settled')
                btn_type = "primary" if is_ready else "secondary"
                
                if st.button(f"▶️ Start DAQ", key=f"daq_{step_idx}", disabled=not is_ready, type=btn_type, use_container_width=True):
                    # NOTE: Here is where we will fire the subprocess for start_daq_gaincal.py
                    st.session_state.step_status[step_idx] = 'daq_running'
                    st.rerun()

            # 3. STATUS INDICATOR
            with status_col:
                if status == 'idle':
                    st.write("💤 Waiting to ramp")
                elif status == 'ramping':
                    st.info("⏳ Ramping and enforcing safety constraints...")
                    # Simulating a hardware ramp finishing for UI testing:
                    time.sleep(2) 
                    st.session_state.step_status[step_idx] = 'settled'
                    st.rerun()
                elif status == 'settled':
                    st.success("✅ Voltages Stable. Ready for DAQ.")
                elif status == 'daq_running':
                    st.warning("🔄 DAQ Script Running...")
                    # Simulating DAQ finishing for UI testing:
                    time.sleep(2)
                    st.session_state.step_status[step_idx] = 'done'
                    st.rerun()
                elif status == 'done':
                    st.success("🏁 DAQ Complete. Data saved.")

    # Emergency Shutoff overrides everything
    st.write("")
    if st.button("🛑 EMERGENCY RAMP DOWN (0V)", type="primary", use_container_width=True):
        st.toast("Safety Trip! Ramping active channels to 0V.", icon="🚨")
        
        # 1. Instantly kill any running background sequence jobs
        hv.abort_sequence = True
        
        # 2. Command all physically configured channels to 0.0 V safely
        active_names = list(st.session_state.channel_map.keys())
        for name in active_names:
            hv.set_voltage(name, 0.0)
            
        # 3. Reset all UI sequence statuses so you can start over
        for key in st.session_state.step_status:
            st.session_state.step_status[key] = 'idle'
            
        st.rerun()