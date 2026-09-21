''' This tab is dedicated to control of HV and data acquistion to minimize user interention'''

import streamlit as st
import time
from backend.hvlogic import DetectorHV, execute_daq_sequence_step, run_safe_step_sequence
from backend.jobs import start_job, jobs
from backend.safety_rules import calculate_safe_trajectory
from backend.daq_engine import execute_daq_script
from backend.step_csv import parse_steps_csv, map_to_logical, grid_offset_violations

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
    if "daq_step_hold" not in st.session_state:
        st.session_state.daq_step_hold = {}  # step index -> hold time (s) loaded from CSV
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
                        # Force an immediate read of all physical telemetry
                        hv.read_all()
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

        # Row 3: Safety Parameters
        col5, col6 = st.columns(2)
        with col5:
            max_curr_limit = st.number_input(
                "Max Current Limit (μA)",
                min_value=0.1,
                max_value=100.0,
                value=getattr(hv, "max_current_limit", 5.0),
                step=0.5,
                key="daq_max_current_limit",
                help="Emergency shutdown triggers if active current exceeds this value."
            )
            hv.max_current_limit = max_curr_limit
        with col6:
            st.empty()

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
    # st.markdown("### 2. Sequence Builder")
    
    # # Row to add a new step
    # st.markdown("**Add New Step Targets (V):**")
    # step_cols = st.columns(len(required_channels) + 1)
    
    # new_step_targets = {}
    # for i, logical_name in enumerate(required_channels):
    #     with step_cols[i]:
    #         new_step_targets[logical_name] = st.number_input(f"{logical_name} Target", value=0.0, step=10.0, key=f"target_{logical_name}")
            
    # with step_cols[-1]:
    #     st.write("") # Spacing to align button with inputs
    #     st.write("")
    #     if st.button("➕ Add Step", type="primary", use_container_width=True):
    #         step_id = len(st.session_state.daq_steps)
    #         st.session_state.daq_steps.append(new_step_targets)
    #         st.session_state.step_status[step_id] = 'idle'
    #         st.rerun()

    #    st.markdown("### 2. Sequence Builder")
    
    # Row to add a new step
    st.markdown("**Add New Step Targets (V):**")
    step_cols = st.columns(len(required_channels) + 1)
    
    new_step_targets = {}
    for i, logical_name in enumerate(required_channels):
        with step_cols[i]:
            new_step_targets[logical_name] = st.number_input(
                f"{logical_name} Target", 
                min_value=0.0, 
                step=10.0, 
                key=f"target_{logical_name}"
            )
            
    # Display validation errors from previous submit attempt
    if st.session_state.get("sequence_builder_error"):
        st.error(st.session_state.sequence_builder_error)

    with step_cols[-1]:
        st.write("") # Spacing to align button with inputs
        st.write("")
        if st.button("➕ Add Step", type="primary", use_container_width=True):
            grid_v = new_step_targets.get("Grid", 0.0)
            invalid_channels = grid_offset_violations(new_step_targets)

            if invalid_channels:
                st.session_state.sequence_builder_error = (
                    f"⚠️ Cannot add step: {', '.join(invalid_channels)} must be at least 10V above the Grid ({grid_v + 10.0}V)."
                )
                st.rerun()
            else:
                st.session_state.sequence_builder_error = None
                step_id = len(st.session_state.daq_steps)
                st.session_state.daq_steps.append(new_step_targets)
                st.session_state.step_status[step_id] = 'idle'
                st.rerun()

    # --- LOAD STEPS FROM CSV ---
    with st.expander("📂 Load steps from CSV"):
        st.caption(
            "Row format: `ch,V,ch,V,...,hold_s` (physical channel numbers, volts, seconds). "
            "Lines starting with # are ignored. Every channel in the map above must appear in every step. "
            "Loaded steps use the same Ramp / Start DAQ buttons and safety checks as manually added steps."
        )
        uploaded = st.file_uploader("Steps CSV", type=["csv"], key="daq_steps_csv")
        if uploaded is not None:
            try:
                text = uploaded.getvalue().decode("utf-8-sig")
            except UnicodeDecodeError:
                text, csv_errors = "", ["File is not valid UTF-8 text."]
            else:
                parsed, csv_errors = parse_steps_csv(text)
            loaded = []
            if not csv_errors:
                loaded, csv_errors = map_to_logical(parsed, st.session_state.channel_map)

            if csv_errors:
                st.error("Cannot load this file:\n\n" + "\n".join(f"- {e}" for e in csv_errors))
            else:
                st.dataframe(
                    [{"Step": i + 1, **{f"{n} (V)": v for n, v in t.items()}, "Hold (s)": h}
                     for i, (t, h) in enumerate(loaded)],
                    hide_index=True,
                )
                steps_busy = (
                    any(s in ("ramping", "daq_running") for s in st.session_state.step_status.values())
                    or jobs.get("HV_Safe_Step_Sequence", {}).get("status") == "running"
                )
                if steps_busy:
                    st.warning("A ramp or DAQ is in progress. Loading is disabled until it finishes.")
                if st.button(f"Replace current steps with these {len(loaded)}", disabled=steps_busy, key="daq_load_csv_btn"):
                    st.session_state.daq_steps = [t for t, _ in loaded]
                    st.session_state.daq_step_hold = {i: h for i, (_, h) in enumerate(loaded)}
                    st.session_state.step_status = {i: 'idle' for i in range(len(loaded))}
                    st.session_state.sequence_builder_error = None
                    st.rerun()

    st.divider()

    # -------------------------------------------------------------------------
    # 4. EXECUTION ENGINE
    # -------------------------------------------------------------------------
    st.markdown("### 3. Execution Engine")

    # Display validation/telemetry errors
    if st.session_state.get("power_error"):
        st.error(st.session_state.power_error)
    if st.session_state.get("ramp_error"):
        st.error(st.session_state.ramp_error)

    # --- MASTER POWER CONTROL ---
    with st.container(border=True):
        st.markdown("**Master Power Control**")
        pwr_col1, pwr_col2 = st.columns(2)
        
        with pwr_col1:
            if st.button("⚡ POWER ON SELECTED CHANNELS", use_container_width=True, type="primary"):
                active_names = list(st.session_state.channel_map.keys())
                try:
                    # Check physical state of all active electrodes
                    indices = [hv.channels[name] for name in active_names]
                    pw_stats = hv.device.get_ch_param(hv.slot, indices, hv.PARAM_PW)
                    
                    if any(pw_stats):
                        st.session_state.power_error = "⚠️ Cannot Power ON: Some channels are already powered ON."
                    else:
                        st.session_state.power_error = None
                        with st.spinner("Initializing hardware and sending Power ON commands..."):
                            hv.set_power(1, active_channel_names=active_names)
                            st.toast("Channels powered ON safely.", icon="✅")
                except Exception as e:
                    st.session_state.power_error = f"⚠️ Connection Error: {e}"
                st.rerun()

        with pwr_col2:
            if st.button("🔌 POWER OFF SELECTED CHANNELS", use_container_width=True):
                active_names = list(st.session_state.channel_map.keys())
                with st.spinner("Safely powering off active channels..."):
                    hv.set_power(0, active_channel_names=active_names)
                    st.session_state.power_error = None
                    st.toast("Channels powered OFF.", icon="🛑")
                st.rerun()

    # --- AUTOMATED SAFE RAMPING ---
    with st.container(border=True):
        st.markdown("**Automated Safe Ramping (3-Channel Mode Only)**")
        
        job_safe_step = "HV_Safe_Step_Sequence"
        is_safe_step_running = jobs.get(job_safe_step, {}).get("status") == "running"
        
        ramp_cols = st.columns(2)
        
        # Check layout configuration type
        is_3ch = "3-Channel" in st.session_state.active_config
        safe_ramp_disabled = not is_3ch or is_safe_step_running
        
        with ramp_cols[0]:
            if st.button("📈 Ramp to Safe Step (Up)", key="ramp_safe_up", use_container_width=True, type="primary", disabled=safe_ramp_disabled):
                active_names = list(st.session_state.channel_map.keys())
                try:
                    indices = [hv.channels[name] for name in active_names]
                    pw_stats = hv.device.get_ch_param(hv.slot, indices, hv.PARAM_PW)
                    
                    if not all(pw_stats):
                        st.session_state.ramp_error = "⚠️ Cannot ramp: Electrodes are powered OFF. Verify the monitor."
                    else:
                        st.session_state.ramp_error = None
                        hv.reset_buffers()
                        start_job(job_safe_step, run_safe_step_sequence, hv, "up")
                except Exception as e:
                    st.session_state.ramp_error = f"⚠️ Connection Error: {e}"
                st.rerun()
                
        with ramp_cols[1]:
            if st.button("📉 Ramp to Zero (Safe Down)", key="ramp_safe_down", use_container_width=True, disabled=safe_ramp_disabled):
                active_names = list(st.session_state.channel_map.keys())
                try:
                    indices = [hv.channels[name] for name in active_names]
                    pw_stats = hv.device.get_ch_param(hv.slot, indices, hv.PARAM_PW)
                    
                    if not all(pw_stats):
                        st.session_state.ramp_error = "⚠️ Cannot ramp down: Electrodes are powered OFF. Verify the monitor."
                    else:
                        st.session_state.ramp_error = None
                        hv.reset_buffers()
                        start_job(job_safe_step, run_safe_step_sequence, hv, "down")
                except Exception as e:
                    st.session_state.ramp_error = f"⚠️ Connection Error: {e}"
                st.rerun()
                
        # Sequence Progress & Status Telemetry Monitor
        if is_safe_step_running:
            progress_val = getattr(hv, 'sequence_progress', 0)
            status_text = getattr(hv, 'sequence_status', 'Safe step sequence running...')
            st.progress(progress_val, text=f"⏳ {status_text}")
            
            if st.button("🛑 ABORT SAFE SEQUENCE", use_container_width=True, type="secondary"):
                hv.abort_sequence = True
                st.toast("Abort signal sent! Ramping down to 0V safely.", icon="🚨")
                st.rerun()
                
            time.sleep(1)
            st.rerun()
            
        elif jobs.get(job_safe_step, {}).get("status") == "done":
            st.success(f"✅ Safe Step Sequence Completed: {hv.sequence_status}")

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
            step_hold = st.session_state.daq_step_hold.get(step_idx)
            hold_str = f" | Hold: {step_hold:g} s" if step_hold else ""
            st.caption(f"Targets: {targets_str}{hold_str}")
            
            ctrl_col1, ctrl_col2, status_col = st.columns([2, 2, 3])
            
            # 1. RAMP BUTTON
            with ctrl_col1:
                ramp_disabled = status in ['ramping', 'settled', 'daq_running', 'done']
                
                if st.button(f"📈 Ramp to Step {step_idx + 1}", key=f"ramp_{step_idx}", disabled=ramp_disabled, use_container_width=True):
                    # Check physical state of all active electrodes
                    try:
                        active_names = list(st.session_state.channel_map.keys())
                        indices = [hv.channels[name] for name in active_names]
                        pw_stats = hv.device.get_ch_param(hv.slot, indices, hv.PARAM_PW)
                        
                        if not all(pw_stats):
                            st.session_state.ramp_error = f"⚠️ Cannot ramp to Step {step_idx + 1}: Electrodes are powered OFF. Verify the monitor."
                        else:
                            st.session_state.ramp_error = None
                            st.session_state.power_error = None
                            # 1. Calculate the safe staging trajectory
                            trajectory = calculate_safe_trajectory(hv, step_data)
                            
                            # 2. Start the background sequence job
                            job_name = f"DAQ_Step_{step_idx}"
                            start_job(job_name, execute_daq_sequence_step, hv, trajectory)
                            
                            st.session_state.step_status[step_idx] = 'ramping'
                    except Exception as e:
                        st.session_state.ramp_error = f"⚠️ Connection Error: {e}"
                    st.rerun()

            # 2. DAQ BUTTON
            with ctrl_col2:
                is_ready = (status == 'settled')
                btn_type = "primary" if is_ready else "secondary"
                
                if st.button(f"▶️ Start DAQ", key=f"daq_{step_idx}", disabled=not is_ready, type=btn_type, use_container_width=True):
                    # Start the DAQ script in the background thread!
                    job_name = f"Run_DAQ_{step_idx}"
                    duration_s = st.session_state.daq_step_hold.get(step_idx, obs_time)
                    start_job(job_name, execute_daq_script, step_idx, step_data, duration_s=duration_s)
                    
                    st.session_state.step_status[step_idx] = 'daq_running'
                    st.rerun()

            # 3. STATUS INDICATOR & LIVE MONITORING
            with status_col:
                if status == 'idle':
                    st.write("💤 Ready to Ramp")
                        
                elif status == 'ramping':
                    job_name = f"DAQ_Step_{step_idx}"
                    job_status = jobs.get(job_name, {}).get("status")
                    
                    if job_status == "running":
                        # Listens to the background hardware loop
                        progress = getattr(hv, 'sequence_progress', 0)
                        msg = getattr(hv, 'sequence_status', 'Ramping...')
                        st.progress(progress, text=f"⏳ {msg}")
                        
                        if st.button("🛑 ABORT RAMP", key=f"abort_{step_idx}", use_container_width=True):
                            hv.abort_sequence = True
                            st.rerun()
                            
                        time.sleep(1) # Safely throttles the UI refresh
                        st.rerun()
                        
                    elif job_status == "done":
                        if getattr(hv, 'abort_sequence', False):
                            st.error("🚨 Sequence Aborted.")
                            st.session_state.step_status[step_idx] = 'idle'
                        else:
                            st.session_state.step_status[step_idx] = 'settled'
                        st.rerun()
                        
                elif status == 'settled':
                    st.success("✅ Voltages Stable. Ready for DAQ.")
                    
                # --- THE CORRECTED DAQ WATCHER ---
                elif status == 'daq_running':
                    daq_job_name = f"Run_DAQ_{step_idx}"
                    daq_status = jobs.get(daq_job_name, {}).get("status")
                    
                    if daq_status == "running":
                        st.warning("🔄 DAQ Script Running in background...")
                        # Throttles the UI refresh while it waits for the background DAQ to finish
                        time.sleep(1) 
                        st.rerun()
                    elif daq_status == "done":
                        # ONLY advance to 'done' when the background script says it is finished!
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