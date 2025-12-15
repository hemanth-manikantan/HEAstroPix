#backend/dacphysics.py
'''DAC / Pulse Amplitude / Electrons conversion utilities'''

E_CHARGE = 1.602e-19 # Charge of electron in Coloumbs
VTP_DAC_STEP_MV = 2.5 # Test Pulse DAC step in mV

def vtp_to_electrons(pH_mV, C_fF):
    '''Convert test pulse height (mV) and input capacitance (fF) to number of electrons'''
    pH_V = pH_mV * 1e-3  # Convert mV to V
    C_F = C_fF * 1e-15   # Convert fF to F
    electrons = (pH_V * C_F) / E_CHARGE
    return electrons

def electrons_to_vtp(electrons, C_fF):
    '''Convert number of electrons and input capacitance (fF) to test pulse height (mV)'''
    C_F = C_fF * 1e-15   # Convert fF to F
    pH_V = (electrons * E_CHARGE) / C_F
    pH_mV = pH_V * 1e3   # Convert V to mV
    return pH_mV

def thlDAC_to_electrons(thl_DAC, slope=0.078, intercept=1289):
    '''Convert Threshold DAC value to number of electrons using calibration parameters'''
    electrons = (thl_DAC - intercept) / slope
    return electrons

def electrons_to_thlDAC(electrons, slope=0.078, intercept=1289):
    '''Convert number of electrons to Threshold DAC value using calibration parameters'''
    thl_DAC = (electrons * slope) + intercept
    return thl_DAC

def vtpDAC_to_electrons(vtpF_DAC, vtpC_DAC, C_fF=3.0):
    '''Convert VTP Fine and Coarse DAC values to number of electrons'''
    total_vtp_mV = (vtpF_DAC * VTP_DAC_STEP_MV) - (vtpC_DAC * 2 * VTP_DAC_STEP_MV)
    electrons = vtp_to_electrons(total_vtp_mV, C_fF)
    return electrons

def electrons_to_vtpFDAC(electrons, vtpC_DAC=100, C_fF=3.0):
    '''Convert number of electrons to VTP Fine DAC value given Coarse DAC value'''
    total_vtp_mV = electrons_to_vtp(electrons, C_fF)
    vtpF_DAC = (total_vtp_mV + (vtpC_DAC * 2 * VTP_DAC_STEP_MV)) / VTP_DAC_STEP_MV
    if 511 < vtpF_DAC < 0:
        raise ValueError("Calculated VTP Fine DAC value out of range (0-511), consider changing VTP Coarse DAC value.")
    else:
        return int(round(vtpF_DAC))
    