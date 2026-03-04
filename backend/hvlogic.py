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
        self.channels = {"Cathode": 1, "Anode": 2, "Grid": 3}
        
        self.PARAM_VSET = "V0Set"
        self.PARAM_VMON = "VMon"
        self.PARAM_IMON = "IMon"
        self.PARAM_PW = "Pw"
        self.PARAM_RUP = "RUp"    
        self.PARAM_RDWN = "RDWn"  

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
    def set_power(self, state: int):
        # Even with the new dropdowns, we keep this set() for backend safety
        unique_channels = list(set(self.channels.values()))
        
        if state == 1:
            # Force 0V startup
            for name in self.channels:
                self.set_voltage(name, 0.0)
            time.sleep(0.2)

        self.device.set_ch_param(self.slot, unique_channels, self.PARAM_PW, state)

        # Send the Master Power command
        self.device.set_ch_param(self.slot, unique_channels, self.PARAM_PW, state)
        
        # Update local cache so the UI reflects the change immediately
        for name in self.channels:
            if name in self.live_data:
                self.live_data[name]["PW"] = state
                if state == 1:
                    self.live_data[name]["VSET"] = 0.0
        
        if self.device:
            self.device.set_ch_param(self.slot, unique_channels, self.PARAM_PW, state)
            print(f"Hardware Command: Power {state} on channels {unique_channels}")
    def initialize_safety_limits(self, rate=10.0):
        for idx in self.channels.values():
            self.device.set_ch_param(self.slot, [idx], self.PARAM_RUP, float(rate))
            self.device.set_ch_param(self.slot, [idx], self.PARAM_RDWN, float(rate))

    def read_all(self):
        now = datetime.now()
        elapsed = round(time.time() - self.start_time, 2)
        self.timestamps.append(elapsed)
        
        current_data = {"Timestamp": now.strftime("%H:%M:%S"), "Elapsed": elapsed}
        
        for name, idx in self.channels.items():
            # FETCH EVERYTHING IN ONE LOOP
            v_mon = self.device.get_ch_param(self.slot, [idx], self.PARAM_VMON)[0]
            i_mon = self.device.get_ch_param(self.slot, [idx], self.PARAM_IMON)[0]
            pw_stat = self.device.get_ch_param(self.slot, [idx], self.PARAM_PW)[0]
            v_set = self.device.get_ch_param(self.slot, [idx], self.PARAM_VSET)[0]
            
            # Update Local Cache (This is what makes the UI fast)
            self.live_data[name] = {"V": v_mon, "I": i_mon, "PW": pw_stat, "VSET": v_set}
            
            # Update Buffers for plots
            self.data_history_v[name].append(v_mon)
            self.data_history_i[name].append(i_mon)
            
            # For CSV
            current_data[f"{name}_Ch{idx}_V"] = v_mon
            current_data[f"{name}_Ch{idx}_I"] = i_mon
            current_data[f"{name}_Ch{idx}_Status"] = int(pw_stat)
        
        self.save_to_log(current_data)
        
        if len(self.timestamps) > 300:
            self.timestamps.pop(0)
            for name in self.channels:
                self.data_history_v[name].pop(0)
                self.data_history_i[name].pop(0)

    def get_dataframe(self, dtype='V'):
        """Returns a formatted DataFrame for Streamlit plotting."""
        data = self.data_history_v if dtype == 'V' else self.data_history_i
        df = pd.DataFrame(data)
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
        """Sets voltage with a hard-coded safety ceiling of 3500V."""
        MAX_SAFE_V = 3500.0
        safe_voltage = min(float(voltage), MAX_SAFE_V)#Select voltage always lower than safe voltage
        if float(voltage) > MAX_SAFE_V:
            print(f"⚠️ WARNING: Requested {voltage}V exceeds safety limit. Clamping to {MAX_SAFE_V}V.")
        ch_idx = self.channels[channel_name]
        self.device.set_ch_param(self.slot, [ch_idx], self.PARAM_VSET, safe_voltage)

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

def run_100V_test(hv_unit):
    """
    Sequence: Ramp to 100V (all channels), hold 10s, ramp to 0V.
    Hardware limits (10V/s) are already set in the class.
    """
    # 1. Command Ramp Up
    hv_unit.set_voltage("Cathode", 100.0)
    hv_unit.set_voltage("Anode", 100.0)
    hv_unit.set_voltage("Grid", 100.0)
    
    # 2. Wait for stable 100V (10s ramp + 5s soak)
    time.sleep(15) 
    
    # 3. Command Ramp Down
    hv_unit.set_voltage("Cathode", 0.0)
    hv_unit.set_voltage("Anode", 0.0)
    hv_unit.set_voltage("Grid", 0.0)
    
    # 4. Wait for 0V (10s ramp)
    time.sleep(12)
    
    return "100V Test Complete"