import streamlit as st
import pandas as pd
import time
from backend.hvlogic import DetectorHV, run_100V_test, run_multi_step_test
from backend.jobs import start_job, jobs 

def HVramp_Caen_tab():
    st.subheader("⚡ CAEN Multi-Config HV Control")

    if "hv_unit" not in st.session_state:
        st.session_state.hv_unit = DetectorHV("192.168.0.1")
        st.session_state.is_connected = False

    hv = st.session_state.hv_unit

    # --- Connection Management ---
    with st.expander("🔌 Connection Settings", expanded=not st.session_state.is_connected):
        col_ip, col_btn = st.columns([3, 1])
        ip_addr = col_ip.text_input("Crate IP Address", value=hv.address)
        
        if not st.session_state.is_connected:
            if col_btn.button("Connect", use_container_width=True, type="primary"):
                hv.address = ip_addr
                if hv.connect():
                    st.session_state.is_connected = True
                    st.rerun()
                else:
                    st.error("Failed to connect. Check network/IP.")
        else:
            if col_btn.button("Disconnect", use_container_width=True):
                with st.spinner("Ramping down for safety..."):
                    hv.shutdown() 
                hv.disconnect()
                st.session_state.is_connected = False
                st.rerun()

    if not st.session_state.is_connected:
        st.info("Please connect to the HV Crate to enable controls.")
        return

    # Check power state using the first mapped channel safely
    first_key = list(hv.channels.keys())[0] if hv.channels else 'Ch0'
    is_powered = hv.live_data.get(first_key, {}).get('PW', 0)

    # --- CONFIGURATION SELECTOR ---
    st.markdown("---")
    config_mode = st.selectbox(
        "🛠️ Detector Setup", 
        ["Global 6-Ch Test", "Config 1 (1-Bar Detector)", "Config 2 (3-Bar Detector)"],
        disabled=is_powered
    )

    # --- CHANNEL MAPPING ---
    with st.expander("🔌 Channel Map", expanded=not is_powered):
        if is_powered:
            st.warning("🔒 Locked: Power must be OFF to change channels.")
        else:
            all_ch = [0, 1, 2, 3, 4, 5]
            
            if config_mode == "Global 6-Ch Test":
                st.info("Monitoring all 6 physical channels.")
                hv.channels = {f"Ch{i}": i for i in range(6)}

            elif config_mode == "Config 1 (1-Bar Detector)":
                st.info("Assign 3 channels. Duplicates are prevented automatically.")
                c1, c2, c3 = st.columns(3)
                
                ch_c = c1.selectbox("Cathode", all_ch, index=0)
                rem1 = [c for c in all_ch if c != ch_c]
                
                ch_a = c2.selectbox("Anode", rem1, index=0)
                rem2 = [c for c in rem1 if c != ch_a]
                
                ch_g = c3.selectbox("Grid", rem2, index=0)
                
                hv.channels = {"Cathode": ch_c, "Anode": ch_a, "Grid": ch_g}

            elif config_mode == "Config 2 (3-Bar Detector)":
                st.info("Assign 5 channels. Duplicates are prevented automatically.")
                c1, c2, c3, c4, c5 = st.columns(5)
                
                ch_c = c1.selectbox("Cath", all_ch, index=0)
                rem1 = [c for c in all_ch if c != ch_c]
                
                ch_a = c2.selectbox("Anod", rem1, index=0)
                rem2 = [c for c in rem1 if c != ch_a]
                
                ch_g = c3.selectbox("Grid", rem2, index=0)
                rem3 = [c for c in rem2 if c != ch_g]
                
                ch_f1 = c4.selectbox("FF1", rem3, index=0)
                rem4 = [c for c in rem3 if c != ch_f1]
                
                ch_f2 = c5.selectbox("FF2", rem4, index=0)
                
                hv.channels = {"Cathode": ch_c, "Anode": ch_a, "Grid": ch_g, "FF1": ch_f1, "FF2": ch_f2}

    # --- Fetch Data ---
    hv.read_all() 
    
    # --- Dynamic Metrics Row ---
    st.markdown("### Live Telemetry")
    cols = st.columns(3)
    
    for i, (name, idx) in enumerate(hv.channels.items()):
        ch_data = hv.live_data.get(name, {"V": 0.0, "I": 0.0, "PW": 0})
        status_icon = "🟢 ON" if ch_data['PW'] else "⚪ OFF"
        
        with cols[i % 3]:
            st.metric(
                label=f"{status_icon} | {name} (Ch {idx})", 
                value=f"{ch_data['V']:.1f} V", 
                delta=f"{ch_data['I']:.3f} μA",
                delta_color="inverse"
            )

    st.divider()

    # --- Control Interface ---
    col_ctrl, col_plots = st.columns([1, 2])

    with col_ctrl:
        # Check running status of both jobs to safely disable manual controls
        job_100v = "HV_100V_Sequence"
        job_multi = "HV_Multi_Step_Sequence"
        is_100v_running = jobs.get(job_100v, {}).get("status") == "running"
        is_multi_running = jobs.get(job_multi, {}).get("status") == "running"
        is_any_job_running = is_100v_running or is_multi_running

        st.markdown("### Power Control")
        if not is_powered:
            if st.button("⚡ POWER ON SYSTEM", use_container_width=True, type="primary"):
                hv.set_power(1)
                st.rerun()
        else:
            if st.button("🔌 POWER OFF SYSTEM", use_container_width=True):
                hv.set_power(0)
                st.rerun()

        st.divider()

        # --- Data Logging Control ---
        st.markdown("### Data Logging")
        if st.button("📝 Start New Log File", use_container_width=True):
            new_file = hv.start_new_log()
            st.success(f"New log started: {new_file}")
            st.rerun()

        st.divider()
        
        ramp_disabled = not is_powered or is_any_job_running

        # --- Automated Sequences ---
        st.markdown("### Automated Sequences")
        
        c_job1, c_job2 = st.columns(2)
        if c_job1.button("📈 100V Test", use_container_width=True, disabled=ramp_disabled):
            hv.reset_buffers()
            start_job(job_100v, run_100V_test, hv)
            st.rerun()
            
        if c_job2.button("⏳ Multi-Step (long)", use_container_width=True, disabled=ramp_disabled):
            hv.reset_buffers()
            start_job(job_multi, run_multi_step_test, hv)
            st.rerun()

        # Display Live Status Messages for Jobs
        if is_100v_running:
            st.info(f"⏳ 100V Sequence : {hv.sequence_status}")
        elif jobs.get(job_100v, {}).get("status") == "done":
            st.success("✅ 100V Sequence Finished")
            
        if is_multi_running:
            # st.info("⏳ Multi-Step (400-600V) Sequence is running...")
            progress_val = getattr(hv, 'sequence_progress', 0)
            status_text = getattr(hv, 'sequence_status', 'Sequence running...')
            st.progress(progress_val, text=f"⏳ {status_text}")

            # THE ABORT BUTTON
            if st.button("🛑 ABORT SEQUENCE", use_container_width=True, type="secondary"):
                hv.abort_sequence = True
                st.toast("Abort signal sent! Ramping down to 0V safely.", icon="🚨")
                st.rerun()
            
            # Auto-refresh loop
            time.sleep(1)
            st.rerun()

        elif jobs.get(job_multi, {}).get("status") == "done":
            st.success("✅ Multi-Step Sequence Finished")

        st.divider()

        # --- Manual Controls ---
        ref_name = list(hv.channels.keys())[0]
        current_v_set = hv.live_data.get(ref_name, {}).get('VSET', 0.0)

        st.markdown("### Manual Staircase")
        step_size = st.number_input("Step Size (V)", 10, 500, 100, 10)
        
        c_up, c_down = st.columns(2)
        next_v = min(9000.0, current_v_set + step_size)
        prev_v = max(0.0, current_v_set - step_size)

        if c_up.button(f"🔼 Ramp Up to: {next_v}V", use_container_width=True, disabled=ramp_disabled):
            hv.reset_buffers()
            for name in hv.channels.keys():
                hv.set_voltage(name, next_v)
            st.rerun()
            
        if c_down.button(f"🔽 Ramp Down to: {prev_v}V", use_container_width=True, disabled=ramp_disabled or current_v_set == 0):
            hv.reset_buffers()
            for name in hv.channels.keys():
                hv.set_voltage(name, prev_v)
            st.rerun()

        st.markdown("---")
        st.markdown("### Custom Manual Jump")
        v_target = st.number_input(
            "Target Voltage (All)", 
            min_value=0, max_value=9000, value=int(current_v_set), step=10, disabled=ramp_disabled
        )
        if st.button("🚀 Jump to Target", use_container_width=True, type="primary", disabled=ramp_disabled):
            hv.reset_buffers()
            for name in hv.channels.keys():
                hv.set_voltage(name, v_target)
            st.rerun()

        st.markdown("---")
        if st.button("🚨 EMERGENCY KILL", use_container_width=True):
            hv.shutdown()

    with col_plots:
        st.caption("Voltage Monitoring (V)")
        df_v = hv.get_dataframe(dtype='V')
        if not df_v.empty: st.line_chart(df_v, height=220)
        else: st.info("Waiting for data alignment...")

        st.caption("Current / Spark Monitor (μA)")
        df_i = hv.get_dataframe(dtype='I')
        if not df_i.empty: st.line_chart(df_i, height=220)
        else: st.info("Waiting for data alignment...")