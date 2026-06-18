import streamlit as st
from backend.dacphysics import vtp_to_electrons, electrons_to_vtp, thlDAC_to_electrons, electrons_to_thlDAC, vtpDAC_to_electrons, electrons_to_tot
from backend.dacphysics import VTP_DAC_STEP_MV, E_CHARGE

def dacphysics_tab():
    st.subheader("DAC / Pulse Amplitude / Electrons Converter")
    st.markdown("Enter the input capacitance in fF and **any one** of the values below to compute the rest.")

    col1, col2, col3, col4, col5 = st.columns(5)

    with col1:
        pulse_mV = st.number_input("Test pulse height [mV]", value=10.0, step=VTP_DAC_STEP_MV)

    with col2:
        capacitance_fF = st.number_input("Input capacitance [fF]", value=3.0, step=0.1, min_value=0.1)

    with col3:
        thl_dac_value = st.number_input("Threshold DAC", value=1500, step=1, min_value=0, max_value=2911)

    with col4:
        thl_per_electron = st.number_input("(THLCalib) Slope [THLDAC/e-]", value=0.078, step=0.001)

    with col5:
        thl_intercept = st.number_input("(THLCalib) Intercept THL [THLDAC at 0 e]", value=1289, step=1)

    col6, col7, col8, col9, col10 = st.columns(5)

    with col6:
        vtpC_dac_value = st.number_input("VTP Coarse DAC", value=100, step=1, min_value=0, max_value=255)

    with col7:
        vtpF_dac_value = st.number_input("VTP Fine DAC", value=200, step=1, min_value=0, max_value=255)
    
    with col8:
        electrons = st.number_input("Number of electrons [e]", value=1000, step=1, min_value=0)

    col11, col12, col13, col14, col15 = st.columns(5)
    
    with col11:
        tot_calib_a = st.number_input("(ToTCalib) Slope [a]", value=0.25, step=0.01)

    with col12:
        tot_calib_b = st.number_input("(ToTCalib) Intercept [b]", value=-6.07, step=0.1)

    with col13:
        tot_calib_c = st.number_input("(ToTCalib) Parameter [c]", value=7.51, step=0.01)

    with col14:
        tot_calib_t = st.number_input("(ToTCalib) Parameter [t]", value=51.91, step=0.01)

    with col15:
        tot = st.number_input("Time-over-Threshold [ToT] (CC)", value=100, step=1, min_value=int(tot_calib_t))

    electrons_from_pulse = vtp_to_electrons(pulse_mV, capacitance_fF)
    electrons_from_thlDAC = thlDAC_to_electrons(thl_dac_value, slope=thl_per_electron, intercept=thl_intercept)
    electrons_from_vtpDAC = vtpDAC_to_electrons(vtpF_DAC=vtpF_dac_value, vtpC_DAC=vtpC_dac_value, C_fF=capacitance_fF)
    electrons_from_tot = electrons_to_tot(electrons=electrons, a=tot_calib_a, b=tot_calib_b, c=tot_calib_c, t=tot_calib_t)

    st.markdown('''
        :red[Calculated values:]
        ---''')

    st.markdown(f"**Test Pulse height** {pulse_mV} mV is {electrons_from_pulse:.1f} electrons")
    st.markdown(f"**THL Combined DAC** {thl_dac_value} is {electrons_from_thlDAC:.1f} electrons")
    st.markdown(f"**VTP DACs** Fine: {vtpF_dac_value}, Coarse: {vtpC_dac_value} is {electrons_from_vtpDAC:.1f} electrons")
    st.markdown(f"**ToT for** {electrons} **electrons** with given calibration is {electrons_from_tot:.1f} Clock Cycles [25 ns/CC]")