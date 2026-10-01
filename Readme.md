# HEAstroPix

**HEAstroPix** is a Python web-based dashboard for **GridPix/Timepix3 detectors** used in **high-energy astrophysics**. It provides a unified interface for **detector operations and data analysis**.

---

## Features

- **HV Ramp Control with Logging**
  - Stepwise ramp-up and ramp-down of detector channels (CH1: Vcathode, CH2: Vanode, CH3: Vgrid)
  - Real-time status indicators for each channel
  - Checkbox-based interface for tracking HV steps
  - Flashing logging indicator when logging is active
  - Automatic timestamped CSV logging of HV states for each channel state change

- **Data Analysis**
  - To be added

---

## Usages

- Make a scan grid of three electrodes
<code>python generate_scan_csv.py scan_steps.csv   --channel-map Grid=0,Anode=4,Cathode=5   --grid-min 420 --grid-max 430 --grid-step 10   --anode-offset-min 120 --anode-offset-max 150 --anode-offset-step 30   --cathode-offset-min 1000 --cathode-offset-max 1000 --cathode-offset-step 100   --hold-s 300 --force<code/>