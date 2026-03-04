import streamlit as st
import pandas as pd
import time
from backend.hvlogic import DetectorHV, run_100V_test
from backend.jobs import start_job, jobs 

def HVramp_Caen_tab():
    st.subheader("CAEN High Voltage Ramp Control System")

    # 1. Initialize Persistent Connection
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
                    st.toast("Connected to CAEN Crate!", icon="✅")
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

    is_powered = hv.live_data['Cathode']['PW']

    # --- Channel Mapping Expander ---
    with st.expander("🔌 Channel Configuration", expanded=not is_powered):
        if is_powered:
            st.warning("🔒 Configuration Locked: Power must be OFF to change channels.")
        else:
            st.info("Assign unique physical channels (1-5) to each component.")
            
        # Define available physical channels
        all_channels = [0, 1, 2, 3, 4, 5]
        
        c1, c2, c3 = st.columns(3)
        
        # --- CATHODE SELECTION ---
        # Current value or default to 1
        current_cat = hv.channels.get("Cathode", 1)
        u_cat = c1.selectbox("Cathode Ch", all_channels, 
                             index=all_channels.index(current_cat) if current_cat in all_channels else 0,
                             disabled=is_powered, key="sel_cat")

        # --- ANODE SELECTION ---
        # Filter out what Cathode took
        anode_options = [ch for ch in all_channels if ch != u_cat]
        current_ano = hv.channels.get("Anode", 2)
        # Handle index carefully if previous selection is now filtered out
        ano_idx = anode_options.index(current_ano) if current_ano in anode_options else 0
        u_ano = c2.selectbox("Anode Ch", anode_options, index=ano_idx,
                             disabled=is_powered, key="sel_ano")

        # --- GRID SELECTION ---
        # Filter out what Cathode AND Anode took
        grid_options = [ch for ch in all_channels if ch not in [u_cat, u_ano]]
        current_gri = hv.channels.get("Grid", 3)
        gri_idx = grid_options.index(current_gri) if current_gri in grid_options else 0
        u_gri = c3.selectbox("Grid Ch", grid_options, index=gri_idx,
                             disabled=is_powered, key="sel_gri")
        
        # Since the UI literally prevents duplicates, mapping_valid is always True here
        mapping_valid = True
        
        if not is_powered:
            # Update the backend dictionary immediately
            hv.channels = {"Cathode": u_cat, "Anode": u_ano, "Grid": u_gri}
            st.success(f"✅ Mapping Locked: Cathode--> Ch{u_cat}, Anode-->Ch{u_ano}, Grid-->Ch{u_gri}")

    # --- THE PERFORMANCE FIX: SINGLE CALL ---
    # This fetches V, I, PW, and VSET for all channels in one network trip
    hv.read_all() 
    
    # 2. Power Status Row (READING FROM LOCAL MEMORY - NO LATENCY)
    ps1, ps2, ps3 = st.columns(3)
    ps1.markdown(f"**Cathode:** {'🟢 ON' if hv.live_data['Cathode']['PW'] else '⚪ OFF'}")
    ps2.markdown(f"**Anode:** {'🟢 ON' if hv.live_data['Anode']['PW'] else '⚪ OFF'}")
    ps3.markdown(f"**Grid:** {'🟢 ON' if hv.live_data['Grid']['PW'] else '⚪ OFF'}")

    # 3. Metrics Row (READING FROM LOCAL MEMORY - NO LATENCY)
    if hv.timestamps:
        m1, m2, m3 = st.columns(3)
        m1.metric(f"Cathode (Ch {hv.channels['Cathode']})", f"{hv.live_data['Cathode']['V']:.1f} V", f"{hv.live_data['Cathode']['I']:.3f} μA")
        m2.metric(f"Anode (Ch {hv.channels['Anode']})", f"{hv.live_data['Anode']['V']:.1f} V", f"{hv.live_data['Anode']['I']:.3f} μA")
        m3.metric(f"Grid (Ch {hv.channels['Grid']})", f"{hv.live_data['Grid']['V']:.1f} V", f"{hv.live_data['Grid']['I']:.3f} μA")

    st.divider()

    # --- Control Interface ---
    col_ctrl, col_plots = st.columns([1, 2])

    with col_ctrl:
        job_name = "HV_100V_Sequence"
        is_running = jobs.get(job_name, {}).get("status") == "running"

        # Power Controls (READING FROM LOCAL MEMORY)
        st.markdown("### Power Control")
        is_powered = hv.live_data['Cathode']['PW'] 
        
        if not is_powered:
            if st.button("⚡ POWER ON CHANNELS", use_container_width=True, type="primary"):
                hv.set_power(1)
                st.rerun()
        else:
            if st.button("🔌 POWER OFF CHANNELS", use_container_width=True):
                hv.set_power(0)
                st.rerun()

        st.divider()
        ramp_disabled = not is_powered or is_running or not mapping_valid

        # Automated Sequence
        st.markdown("### Automated Sequence")
        if st.button("📈 Run 100V Test", use_container_width=True, disabled=ramp_disabled):
            hv.reset_buffers()
            start_job(job_name, run_100V_test, hv)
            st.toast("Starting 100V Sequence...")

        if job_name in jobs:
            status = jobs[job_name]["status"]
            if status == "running": st.info("⏳ Sequence: Ramping/Holding...")
            elif status == "done": st.success("✅ Sequence Finished")

        st.markdown("---")
        
        # Manual Staircase (READING FROM LOCAL MEMORY)
        st.markdown("### Manual Staircase")
        current_v_set = hv.live_data['Cathode']['VSET']

        step_size = st.number_input("Step Size (V)", 10, 500, 100, 10)
        c_up, c_down = st.columns(2)
        
        next_v = min(3500.0, current_v_set + step_size)
        prev_v = max(0.0, current_v_set - step_size)

        if c_up.button(f"🔼 Ramp Up to: {next_v}V", use_container_width=True, disabled=ramp_disabled):
            hv.reset_buffers()
            hv.set_voltage("Cathode", next_v); hv.set_voltage("Anode", next_v); hv.set_voltage("Grid", next_v)
            
        if c_down.button(f"🔽 Ramp Down to: {prev_v}V", use_container_width=True, disabled=ramp_disabled or current_v_set == 0):
            hv.reset_buffers()
            hv.set_voltage("Cathode", prev_v); hv.set_voltage("Anode", prev_v); hv.set_voltage("Grid", prev_v)

        st.markdown("---")
        st.markdown("### Custom Manual Jump")
        v_target = st.number_input(
            "Target Voltage (All)", 
            min_value=0, 
            max_value=3500,  # HARD LIMIT
            value=int(current_v_set), 
            step=10, 
            disabled=ramp_disabled
        )
        if st.button("🚀 Jump to Target", use_container_width=True, type="primary", disabled=ramp_disabled):
            hv.reset_buffers()
            hv.set_voltage("Cathode", v_target); hv.set_voltage("Anode", v_target); hv.set_voltage("Grid", v_target)

        st.markdown("---")
        if st.button("🚨 EMERGENCY KILL", use_container_width=True):
            hv.shutdown()
            st.warning("All Channels set to 0V and Powered OFF.")

    with col_plots:
        st.caption("Voltage Monitoring (V)")
        st.line_chart(hv.get_dataframe(dtype='V'), height=220)

        st.caption("Current / Spark Monitor (μA)")
        df_i = hv.get_dataframe(dtype='I')
        if not df_i.empty:
            st.line_chart(df_i, height=220)
        else:
            st.write("Waiting for telemetry...")