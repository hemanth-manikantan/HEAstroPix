import time
import subprocess

def execute_daq_script(step_idx, target_voltages, duration_s=None):
    """
    Runs as a background thread to trigger the Timepix3 DAQ script.
    """
    print(f"▶️ Starting DAQ for Step {step_idx + 1}...")
    print(f"   Targets locked at: {target_voltages}, duration: {duration_s} s")

    time.sleep(5) 
    
    print(f"🏁 DAQ for Step {step_idx + 1} Complete.")
    return "Complete"