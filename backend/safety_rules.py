# backend/safety_rules.py

'''The safety rules for ramping up and dwon the HV unit should appear in this block'''

def calculate_safe_trajectory(hv_unit, target_voltages):
    """
    Generates a list of safe intermediate checkpoints.
    Enforces the rule: Stage at Grid=300V, Others=350V before proceeding.
    """
    trajectory = []
    staging_step = {}
    
    # Check if Grid is part of this configuration
    if "Grid" in target_voltages:
        current_grid_v = hv_unit.live_data.get("Grid", {}).get("V", 0.0)
        target_grid_v = target_voltages["Grid"]
        
        # If we are crossing the 300V threshold (either up or down), insert the staging checkpoint
        needs_staging = (current_grid_v < 300.0 and target_grid_v >= 300.0) or (current_grid_v > 300.0 and target_grid_v <= 300.0)
        
        if needs_staging:
            for ch_name in target_voltages.keys():
                if ch_name == "Grid":
                    staging_step[ch_name] = 300.0
                else:
                    staging_step[ch_name] = 350.0
                    
            trajectory.append(staging_step)
            
    # Always append the final user-requested targets at the end
    trajectory.append(target_voltages)
    
    return trajectory