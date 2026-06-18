import time
import csv
import pandas as pd
from datetime import datetime
from caen_libs import caenhvwrapper as hv

class DetectorHV:
    def __init__(self, address, user="admin", password="admin"):
        self.system_type = hv.SystemType.SY5527
        self.link_type = hv.LinkType.TCPIP
        self.address = address
        self.user = user
        self.password = password
        
        self.device = None
        self.slot = 0
        #self.channels = {"Cathode": 1, "Anode": 2, "Grid": 3}
        self.channels = {f"Ch{i}": i for i in range(6)}
        
        self.PARAM_VSET = "V0Set"
        self.PARAM_VMON = "VMon"
        self.PARAM_ISET = "I0Set"
        self.PARAM_IMON = "IMon"
        self.PARAM_PW = "Pw"
        self.PARAM_RUP = "RUp"    
        self.PARAM_RDWN = "RDWn"

        #Seqeunce Status Updates
        self.sequence_status = "Idle"
        # Sequence Abort flags
        self.sequence_progress = 0
        self.abort_sequence = False  

        # Data Buffers
        self.timestamps = []
        self.data_history_v = {name: [] for name in self.channels}
        self.data_history_i = {name: [] for name in self.channels}
        
        self.start_time = time.time()
        self.log_filename = f"hv_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        
        self.live_data = {
            name: {"V": 0.0, "I": 0.0, "PW": 0, "VSET": 0.0} 
            for name in self.channels
        }

    def connect(self):
        try:
            self.device = hv.Device.open(self.system_type, self.link_type, 
                                        self.address, self.user, self.password)
            # We still initialize safety limits (Ramp rates) as a precaution
            self.initialize_safety_limits(rate=10.0)
            
            # REMOVED: self.device.set_ch_param(..., self.PARAM_PW, 1)
            return True
        except Exception as e:
            print(f"Connection Error: {e}")
            return False

    # def set_power(self, state: int):
    #     """Explicitly turn Power ON (1) or OFF (0) for ONLY the unique mapped channels."""
    #     # This converts [0, 1, 1] into [0, 1]. Ch 2 is ignored!
    #     unique_channels = list(set(self.channels.values()))
    # def set_power(self, state: int):
    #     # Even with the new dropdowns, we keep this set() for backend safety
    #     unique_channels = list(set(self.channels.values()))
        
    #     if state == 1:
    #         # Force 0V startup
    #         for name in self.channels:
    #             self.set_voltage(name, 0.0)
    #         time.sleep(0.2)

    #     self.device.set_ch_param(self.slot, unique_channels, self.PARAM_PW, state)

    #     # Send the Master Power command
    #     self.device.set_ch_param(self.slot, unique_channels, self.PARAM_PW, state)
        
    #     # Update local cache so the UI reflects the change immediately
    #     for name in self.channels:
    #         if name in self.live_data:
    #             self.live_data[name]["PW"] = state
    #             if state == 1:
    #                 self.live_data[name]["VSET"] = 0.0
        
    #     if self.device:
    #         self.device.set_ch_param(self.slot, unique_channels, self.PARAM_PW, state)
    #         print(f"Hardware Command: Power {state} on channels {unique_channels}")

    def set_power(self, state: int, active_channel_names=None):
        """
        Turns Power ON (1) or OFF (0) for a specific list of channels.
        If no list is provided, it defaults to all mapped channels.
        """
        if active_channel_names is None:
            active_channel_names = list(self.channels.keys())
            
        unique_indices = list(set([self.channels[name] for name in active_channel_names]))
        
        if state == 1:
            # HARDWARE SAFETY CHECK
            # Only force 0V if the physical channel is actually OFF right now
            for name in active_channel_names:
                ch_idx = self.channels[name]
                is_on = self.device.get_ch_param(self.slot, [ch_idx], self.PARAM_PW)[0]
                
                if not is_on:
                    self.set_voltage(name, 0.0)
            time.sleep(0.2)

        if self.device:
            self.device.set_ch_param(self.slot, unique_indices, self.PARAM_PW, state)
            print(f"Hardware Command: Power {state} on channels {unique_indices}")
        
        # Update local UI cache
        for name in active_channel_names:
            if name in self.live_data:
                self.live_data[name]["PW"] = state
                # Let the next read_all() cycle update the VSET to avoid caching incorrect values
    
    def initialize_safety_limits(self, rate=10.0):
        for idx in self.channels.values():
            self.device.set_ch_param(self.slot, [idx], self.PARAM_RUP, float(rate))
            self.device.set_ch_param(self.slot, [idx], self.PARAM_RDWN, float(rate))

    def read_all(self):
        now = datetime.now()
        elapsed = round(time.time() - self.start_time, 2)
        current_data = {"Timestamp": now.strftime("%Y-%m-%d %H:%M:%S"), "Elapsed": elapsed}
        #current_data = {"Timestamp": now.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3], "Elapsed": elapsed}
        
        # 1. FAIL-SAFE: Create missing buffers
        for name in self.channels:
            if name not in self.live_data:
                self.live_data[name] = {"V": 0.0, "I": 0.0, "PW": 0, "VSET": 0.0}
            if name not in self.data_history_v:
                self.data_history_v[name] = []
                self.data_history_i[name] = []

        # 2. THE SPEED OPTIMIZATION: Batch Network Requests
        all_ch = [0, 1, 2, 3, 4, 5]
        
        try:
            # We pass the entire list of channels at once.
            # The library returns a list of 6 values back.
            v_sets = self.device.get_ch_param(self.slot, all_ch, self.PARAM_VSET)
            v_mons = self.device.get_ch_param(self.slot, all_ch, self.PARAM_VMON)
            i_sets = self.device.get_ch_param(self.slot, all_ch, self.PARAM_ISET)
            i_mons = self.device.get_ch_param(self.slot, all_ch, self.PARAM_IMON)
            pw_stats = self.device.get_ch_param(self.slot, all_ch, self.PARAM_PW)
            
            # 3. UNPACK THE BATCH DATA
            for idx in all_ch:
                # Format CSV Columns
                current_data[f"ch{idx}_setV"] = v_sets[idx]
                current_data[f"ch{idx}_monV"] = v_mons[idx]
                current_data[f"ch{idx}_setI"] = i_sets[idx]
                current_data[f"ch{idx}_monI"] = i_mons[idx]
                current_data[f"ch{idx}_status"] = int(pw_stats[idx])

                # Update UI cache only for mapped names
                for name, mapped_idx in self.channels.items():
                    if idx == mapped_idx:
                        self.live_data[name] = {"V": v_mons[idx], "I": i_mons[idx], "PW": pw_stats[idx], "VSET": v_sets[idx]}
                        self.data_history_v[name].append(v_mons[idx])
                        self.data_history_i[name].append(i_mons[idx])
            
            # 4. APPEND TIMESTAMP & SAVE
            self.timestamps.append(elapsed)
            self.save_to_log(current_data)
            
        except Exception as e:
            print(f"Batch Network Read Error: {e}")
            # If a network packet drops, we safely skip this cycle to prevent unequal arrays
            pass
        
        # 5. SLIDING WINDOW MEMORY MANAGEMENT
        if len(self.timestamps) > 300:
            self.timestamps.pop(0)
            for name in self.channels:
                if len(self.data_history_v.get(name, [])) > 0:
                    self.data_history_v[name].pop(0)
                    self.data_history_i[name].pop(0)

    # def get_dataframe(self, dtype='V'):
    #     """Returns a formatted DataFrame for Streamlit plotting."""
    #     data = self.data_history_v if dtype == 'V' else self.data_history_i
    #     df = pd.DataFrame(data)
    #     df.index = self.timestamps
    #     return df
    def get_dataframe(self, dtype='V'):
        """Returns a formatted DataFrame for Streamlit plotting, safe against mismatched array lengths."""
        data = self.data_history_v if dtype == 'V' else self.data_history_i
        target_len = len(self.timestamps)
        
        safe_data = {}
        
        # Only attempt to plot the channels currently active in the configuration
        for name in self.channels.keys():
            arr = data.get(name, [])
            
            # Balance the array lengths to prevent Pandas crashes
            if len(arr) < target_len:
                # Pad missing historical data with None (blanks on the chart)
                safe_data[name] = [None] * (target_len - len(arr)) + arr
            elif len(arr) > target_len:
                # Truncate if the network buffer somehow got ahead of the timestamps
                safe_data[name] = arr[-target_len:]
            else:
                safe_data[name] = arr
                
        df = pd.DataFrame(safe_data)
        df.index = self.timestamps
        return df

    def save_to_log(self, data_dict):
        file_exists = False
        try:
            with open(self.log_filename, 'r'): file_exists = True
        except FileNotFoundError: pass
        with open(self.log_filename, 'a', newline='') as f:
            writer = csv.DictWriter(f, fieldnames=data_dict.keys())
            if not file_exists: writer.writeheader()
            writer.writerow(data_dict)

    def set_voltage(self, channel_name, voltage):
        """Sets voltage with a hard-coded safety ceiling of 9000V."""
        MAX_SAFE_V = 9000.0
        safe_voltage = min(float(voltage), MAX_SAFE_V)#Select voltage always lower than safe voltage
        if float(voltage) > MAX_SAFE_V:
            print(f"⚠️ WARNING: Requested {voltage}V exceeds safety limit. Clamping to {MAX_SAFE_V}V.")
        ch_idx = self.channels[channel_name]
        self.device.set_ch_param(self.slot, [ch_idx], self.PARAM_VSET, safe_voltage)

    def start_new_log(self):
        """Generates a new log file and resets the elapsed time and plots."""
        # Generate a new timestamped filename
        #self.log_filename = f"hv_log_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')[:-3]}.csv"
        self.log_filename = f"hv_log_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        
        # Reset the internal clock and clear the UI plots so everything starts fresh
        self.reset_buffers()
        
        return self.log_filename

    # def shutdown(self):
    #     for name in self.channels:
    #         self.set_voltage(name, 0.0)
    #     time.sleep(2) # Initial wait
    #     self.device.set_ch_param(self.slot, list(self.channels.values()), self.PARAM_PW, 0)

    def shutdown(self):
        """
        Safely ramps down only the channels that are currently powered ON,
        then cuts the master power (Pw=0).
        """
        # 1. Identify which physical channels are actually powered ON
        # We use a set to ensure we only handle each physical channel once
        unique_mapped_channels = list(set(self.channels.values()))
        active_channels = []

        for ch_idx in unique_mapped_channels:
            # Check the hardware status of 'Pw'
            is_on = self.device.get_ch_param(self.slot, [ch_idx], self.PARAM_PW)[0]
            if is_on:
                active_channels.append(ch_idx)

        # 2. If no channels are active, we can skip the ramp and just ensure Pw is 0
        if not active_channels:
            return

        # 3. Ramp all active channels to 0.0V
        # Since 'set_voltage' takes a name, we find the name associated with the index
        for name, idx in self.channels.items():
            if idx in active_channels:
                self.set_voltage(name, 0.0)
        
        # 4. Wait for the hardware to process the ramp (Wait time depends on RDWN rate)
        # Assuming 10V/s and a max possible voltage, 2-3s is a safe 'initial' wait
        time.sleep(2) 

        # 5. Finally, cut the Power (Pw=0) for all unique mapped channels
        self.device.set_ch_param(self.slot, unique_mapped_channels, self.PARAM_PW, 0)
        
        # 6. Clear local cache for UI consistency
        for name in self.channels:
            if name in self.live_data:
                self.live_data[name]["PW"] = 0

    def disconnect(self):
        if self.device: self.device.close()

    def reset_buffers(self):
        """Clears all historical data and resets the elapsed time clock."""
        self.timestamps = []
        for name in self.channels:
            self.data_history_v[name] = []
            self.data_history_i[name] = []
        self.start_time = time.time() # Reset clock to 0.0


# def run_multi_step_test(hv_unit):
#     """
#     Sequence: 
#     - Ramp to 400V, hold 15 mins.
#     - Ramp to 500V, hold 15 mins.
#     - Ramp to 600V, hold 15 mins.
#     - Ramp down to 0V.
#     Calculates wait times automatically based on a 10 V/s ramp rate.
#     """
#     ramp_rate = 10.0
#     settle_buffer = 15.0      # 15 extra seconds for current to stabilize after a ramp
#     hold_time = 15 * 60       # 15 minutes in seconds (900 seconds)

#     # Helper function to dynamically set voltage for ALL active channels in the current config
#     def set_all_channels(target_v):
#         for name in hv_unit.channels.keys():
#             hv_unit.set_voltage(name, float(target_v))

#     # --- STEP 1: 0V to 400V ---
#     set_all_channels(400.0)
#     # 400V / 10Vps = 40s ramp + 15s settle
#     wait_time_1 = (400.0 / ramp_rate) + settle_buffer
#     time.sleep(wait_time_1)
    
#     # 15 Minute Hold
#     time.sleep(hold_time)

#     # --- STEP 2: 400V to 500V ---
#     set_all_channels(500.0)
#     # 100V jump / 10Vps = 10s ramp + 15s settle
#     wait_time_2 = (100.0 / ramp_rate) + settle_buffer
#     time.sleep(wait_time_2)
    
#     # 15 Minute Hold
#     time.sleep(hold_time)

#     # --- STEP 3: 500V to 600V ---
#     set_all_channels(600.0)
#     # 100V jump / 10Vps = 10s ramp + 15s settle
#     wait_time_3 = (100.0 / ramp_rate) + settle_buffer
#     time.sleep(wait_time_3)
    
#     # 15 Minute Hold
#     time.sleep(hold_time)

#     # --- STEP 4: Ramp Down to 0V ---
#     set_all_channels(0.0)
#     # 600V drop / 10Vps = 60s ramp + 10s settle
#     wait_time_down = (600.0 / ramp_rate) + 10.0
#     time.sleep(wait_time_down)

#     return "400-500-600V Sequence Complete"

# Multi step HV sequence
def run_multi_step_test(hv_unit):
    """
    Sequence: 
    - Ramp to 400, 500, 600, 1000, 1400, 1500, 1600, 2000, 2500, 3000, 3500V.
    - Hold each level for 15 mins.
    - Ramp down to 0V.
    Calculates wait times automatically based on a 10 V/s ramp rate.
    """
    ramp_rate = 10.0
    settle_buffer = 15.0      # 15 extra seconds for current to stabilize after a ramp
    hold_time = 15 * 60       # 15 minutes in seconds (900 seconds)

    # Define your specific target steps
    voltage_steps = [400.0, 500.0, 600.0, 1000.0, 1400.0, 1500.0, 1600.0, 2000.0, 2500.0, 3000.0, 3500.0, 4500.0, 5500.0, 6500.0, 7500.0, 8500.0, 9500.0, 10500.0, 11500.0]

    # Helper function to dynamically set voltage for ALL active channels in the current config
    def set_all_channels(target_v):
        for name in hv_unit.channels.keys():
            hv_unit.set_voltage(name, float(target_v))

    hv_unit.abort_sequence = False # Reset flag when starting a new test
    hv_unit.sequence_progress = 0
    total_time = 0.0
    sim_v = 0.0
    for t in voltage_steps:
        total_time += (abs(t - sim_v) / ramp_rate) + settle_buffer + hold_time
        sim_v = t
    total_time += (sim_v / ramp_rate) + 10.0
    elapsed_time = 0.0

    # Sleep/abort function that holds s/w during ramping and checks for abort every second
    def smart_sleep(duration):
        nonlocal elapsed_time
        chunks = int(duration)
        remainder = duration - chunks
        
        for _ in range(chunks):
            # CHECK FLAG EVERY SECOND
            if getattr(hv_unit, 'abort_sequence', False):
                return False # Signifies we need to abort
            
            time.sleep(1)
            elapsed_time += 1
            hv_unit.sequence_progress = min(int((elapsed_time / total_time) * 100), 100)
            
        if remainder > 0:
            if getattr(hv_unit, 'abort_sequence', False):
                return False
            time.sleep(remainder)
            elapsed_time += remainder
            hv_unit.sequence_progress = min(int((elapsed_time / total_time) * 100), 100)
            
        return True # Signifies normal completion
    
    # Track the current theoretical voltage to calculate the jump size
    current_v = 0.0
    target = 0.0 

    # for step, target in enumerate(voltage_steps):
    #     # 1. Command the new voltage
    #     set_all_channels(target)
    #     hv_unit.sequence_status = f"Ramping to {target}V..."
        
    #     # 2. Calculate the exact time needed for this specific jump
    #     v_jump = abs(target - current_v)
    #     wait_time = (v_jump / ramp_rate) + settle_buffer
    #     time.sleep(wait_time)
        
    #     hv_unit.sequence_status = f"Holding at {target}V (Step {step+1}/{len(voltage_steps)})..."
    #     # 3. Hold for 15 minutes
    #     time.sleep(hold_time)
        
    #     # 4. Update the tracker for the next loop iteration
    #     current_v = target

    # # --- Final Step: Ramp Down to 0V ---
    # hv_unit.sequence_status = "Ramping down to 0V..."
    # set_all_channels(0.0)
    # wait_time_down = (current_v / ramp_rate) + 10.0
    # time.sleep(wait_time_down)


    for step, target in enumerate(voltage_steps):
        # 1. Command the new voltage
        hv_unit.sequence_status = f"Ramping to {target}V..."
        set_all_channels(target)
        
        # 2. Use smart_sleep for the ramp time
        v_jump = abs(target - current_v)
        wait_time = (v_jump / ramp_rate) + settle_buffer
        if not smart_sleep(wait_time):
            break # Breaks out of loop if aborted
        
        # 3. Use smart_sleep for the hold time
        hv_unit.sequence_status = f"Holding at {target}V (Step {step+1}/{len(voltage_steps)})..."
        if not smart_sleep(hold_time):
            break # Breaks out of loop if aborted
        
        current_v = target

    if getattr(hv_unit, 'abort_sequence', False):
        hv_unit.sequence_status = "🚨 ABORTING: Safely ramping down to 0V..."
    else:
        hv_unit.sequence_status = "Ramping down to 0V..."
        
    set_all_channels(0.0)

    # Calculate how long the hardware physically needs to return to 0V
    wait_time_down = (target / ramp_rate) + 10.0
    time.sleep(wait_time_down) # Standard sleep here is fine, the test is already ending

    hv_unit.sequence_status = "Idle"
    hv_unit.sequence_progress = 0
    hv_unit.abort_sequence = False
    return "Sequence Finished"

#Test case ramp to 100V, ramp down to 0V
def run_100V_test(hv_unit):
    """
    Sequence: Ramp to 100V (all channels), hold 10s, ramp to 0V.
    Hardware limits (10V/s) are already set in the class.
    """
    # # 1. Command Ramp Up
    # hv_unit.set_voltage("Cathode", 100.0)
    # hv_unit.set_voltage("Anode", 100.0)
    # hv_unit.set_voltage("Grid", 100.0)
    
    # # 2. Wait for stable 100V (10s ramp + 5s soak)
    # time.sleep(15) 
    
    # # 3. Command Ramp Down
    # hv_unit.set_voltage("Cathode", 0.0)
    # hv_unit.set_voltage("Anode", 0.0)
    # hv_unit.set_voltage("Grid", 0.0)

    # Helper function to dynamically set voltage for ALL active channels in the current config
    def set_all_channels(target_v):
        for name in hv_unit.channels.keys():
            hv_unit.set_voltage(name, float(target_v))
    
    # Ramp Up to 100V
    set_all_channels(100.0)
    hv_unit.sequence_status = "Ramping up to 100 V"
    
    # Wait for ramp up (10s ramp)
    time.sleep(12)

    # Ramp Down to 0V
    set_all_channels(0.0)
    hv_unit.sequence_status = "Ramping down to 0 V"

    # Wait for ramp down (10s ramp)
    time.sleep(12)

    hv_unit.sequence_status = "Idle"#To default back to idle after sequence completion
    return "100V Test Complete"

def execute_daq_sequence_step(hv_unit, trajectory, hold_time=15.0):
    """
    Added 20260618
    Executes a multi-checkpoint trajectory provided by the Safety Engine.
    trajectory: List of dictionaries, e.g., [{'Grid': 300, 'Anode': 350}, {'Grid': 1460, 'Anode': 460}]
    """
    ramp_rate = 10.0 # V/s
    settle_buffer = 10.0
    elapsed_time = 0.0
    
    hv_unit.abort_sequence = False
    hv_unit.sequence_progress = 0
    
    # Calculate total sequence time for the progress bar
    total_time = 0.0
    current_v_tracker = {name: hv_unit.live_data.get(name, {}).get("V", 0.0) for name in trajectory[0].keys()}
    
    for step_targets in trajectory:
        max_jump = 0.0
        for name, target in step_targets.items():
            jump = abs(target - current_v_tracker.get(name, 0.0))
            if jump > max_jump: max_jump = jump
            current_v_tracker[name] = target
            
        total_time += (max_jump / ramp_rate) + settle_buffer
    
    # Smart Sleep with Watchdog
    def smart_sleep_watchdog(duration):
        nonlocal elapsed_time
        chunks = int(duration)
        remainder = duration - chunks
        
        for _ in range(chunks):
            if getattr(hv_unit, 'abort_sequence', False): return False
            time.sleep(1)
            elapsed_time += 1
            if total_time > 0:
                hv_unit.sequence_progress = min(int((elapsed_time / total_time) * 100), 100)
            
        if remainder > 0:
            if getattr(hv_unit, 'abort_sequence', False): return False
            time.sleep(remainder)
            elapsed_time += remainder
            if total_time > 0:
                hv_unit.sequence_progress = min(int((elapsed_time / total_time) * 100), 100)
        return True

    # --- EXECUTE THE TRAJECTORY ---
    for i, step_targets in enumerate(trajectory):
        hv_unit.sequence_status = f"Executing safe checkpoint {i+1}/{len(trajectory)}..."
        
        # 1. Calculate the max time needed for the biggest voltage jump in this specific step
        max_v_jump = 0.0
        for name, target in step_targets.items():
            current_v = hv_unit.live_data.get(name, {}).get("V", 0.0)
            jump = abs(target - current_v)
            if jump > max_v_jump: max_v_jump = jump
            
            # Send the actual CAEN command
            hv_unit.set_voltage(name, float(target))
            
        wait_time = (max_v_jump / ramp_rate) + settle_buffer
        
        # 2. Wait for the ramp to finish
        if not smart_sleep_watchdog(wait_time):
            break
            
    # --- COMPLETION CHECK ---
    if getattr(hv_unit, 'abort_sequence', False):
        hv_unit.sequence_status = "🚨 ABORTED: Ramping to safe 0V state..."
        for name in trajectory[-1].keys():
            hv_unit.set_voltage(name, 0.0)
        return "Aborted"
        
    hv_unit.sequence_status = "Voltages Settled. Waiting for DAQ trigger."
    hv_unit.sequence_progress = 100
    return "Complete"