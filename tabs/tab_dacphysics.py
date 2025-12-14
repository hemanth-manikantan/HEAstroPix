import streamlit as st
from backend.dacphysics import vtp_to_electrons, electrons_to_vtp, thlDAC_to_electrons, electrons_to_thlDAC
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
        vtpC_dac_value = st.number_input("VTP Coarse DAC", value=100, step=1)

    electrons_from_pulse = vtp_to_electrons(pulse_mV, capacitance_fF)
    electrons_from_thlDAC = thlDAC_to_electrons(thl_dac_value, slope=thl_per_electron, intercept=thl_intercept)

    st.markdown('''
        :red[Calculated values:]
        ---''')

    st.markdown(f"**Test Pulse height** {pulse_mV} mV is {electrons_from_pulse:.1f} electrons")
    st.markdown(f"**THL Combined DAC** {thl_dac_value} is {electrons_from_thlDAC:.1f} electrons")