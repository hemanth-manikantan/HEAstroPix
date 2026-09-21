# backend/safety_rules.py

'''The safety rules for ramping up and dwon the HV unit should appear in this block'''

def calculate_safe_trajectory(hv_unit, target_voltages):
    """
    Generates a list of safe intermediate checkpoints for the voltage ramp.
    Enforces the primary staging rule: Pause at Grid=300V, Others=350V 
    before proceeding to higher voltages.
    """
    trajectory = []
    staging_step = {}
    
    # We only need to enforce staging if the Grid is part of the active configuration
    if "Grid" in target_voltages:
        current_grid_v = hv_unit.live_data.get("Grid", {}).get("V", 0.0)
        target_grid_v = target_voltages["Grid"]
        
        # Check if our flight path crosses the 300V threshold (either ramping up or down)
        needs_staging = (current_grid_v < 300.0 and target_grid_v > 300.0) or \
                        (current_grid_v > 300.0 and target_grid_v < 300.0)
        
        if needs_staging:
            for ch_name, final_target in target_voltages.items():
                if ch_name == "Grid":
                    staging_step[ch_name] = 300.0
                elif ch_name == "Cathode":
                    # Don't force the drift Cathode to +350V! 
                    # Stage it towards its target safely.
                    staging_step[ch_name] = final_target
                else:
                    # Anode, FF1, FF2, Window get the +50V staging offset
                    staging_step[ch_name] = 350.0
                    
            trajectory.append(staging_step)
            
    # Always append the final user-requested targets
    trajectory.append(target_voltages)
    
    return trajectory


def validate_50v_delta(live_voltages):
    """
    A strict safety watchdog to evaluate the set voltages.
    Returns True if the set voltages are safe, False if the 50V rule is violated.
    """
    if "Grid" not in live_voltages:
        return True # Cannot check delta if Grid isn't active
        
    # Extract the set voltage of the Grid
    grid_v = live_voltages["Grid"].get("VSET", 0.0)
    
    for ch_name, data in live_voltages.items():
        # 1. Ignore the Grid itself, and completely ignore the Cathode
        if ch_name in ["Grid", "Cathode"]:
            continue

        # 2. Ignore any channels that are physically powered OFF right now
        if data.get("PW", 0) == 0:
            continue
        
        ch_v = data.get("VSET", 0.0)
        # 3. The 50V Rule Check (with a 5V noise buffer)
        # If Anode/FF drops more than 55V below the Grid, trigger the abort.
        if ch_v < (grid_v - 55.0):
            print(f"🚨 WATCHDOG TRIP: Grid (VSET) is {grid_v}V, but {ch_name} (VSET) is {ch_v}V!")
            return False
                
    return True


def validate_current_limits(live_data, max_current_ua=5.0):
    """
    A safety watchdog to evaluate physical current telemetry.
    Returns True if currents are safe, False if any channel exceeds max_current_ua.
    """
    for ch_name, data in live_data.items():
        # Ignore channels that are physically powered OFF
        if data.get("PW", 0) == 0:
            continue
            
        ch_i = data.get("I", 0.0) # Current is in microamps (uA)
        if ch_i > max_current_ua:
            print(f"🚨 OVERCURRENT TRIP: {ch_name} current is {ch_i} uA, exceeding limit of {max_current_ua} uA!")
            return False
    return True


# ---------------------------------------------------------------------------
# Electrode ordering rule (operator decision, 2026-09-21). Additive: the older
# functions above are unchanged and still used by the dashboard.
#   * every non-Grid channel >= Grid + min_delta (20 V), at all times
#   * Cathode >= Anode (targets must have Cathode > Anode)
#   * only enforced while Grid > floor (the rule cannot hold at 0/0/0)
# Ramps: Anode/Cathode lead the Grid by `offset` (>= min_delta) until Grid reaches
# its target, then make the final ramp. All channels ramp at the same V/s.
# ---------------------------------------------------------------------------
def ordering_violations(volts, min_delta=20.0, tol=0.0, floor=0.0, strict_cathode=False):
    """volts: {"Grid": V, "Anode": V, "Cathode": V, ...}. Returns a list of violation messages."""
    if "Grid" not in volts or volts["Grid"] <= floor:
        return []
    grid = volts["Grid"]
    out = [f"{name} is {v - grid:.1f} V above Grid (needs >= {min_delta:g} V)"
           for name, v in volts.items() if name != "Grid" and v - grid < min_delta - tol]
    if "Anode" in volts and "Cathode" in volts:
        anode, cathode = volts["Anode"], volts["Cathode"]
        if (cathode <= anode) if strict_cathode else (cathode < anode - tol):
            out.append(f"Cathode ({cathode:g} V) must be {'above' if strict_cathode else 'at least'} Anode ({anode:g} V)")
    return out


def _lift_cathode(state):
    if "Anode" in state and "Cathode" in state:
        state["Cathode"] = max(state["Cathode"], state["Anode"])
    return state


def _plan_segment(cur, tgt, offset):
    others = [n for n in tgt if n != "Grid"]
    cps, state = [], dict(cur)

    def push(cp):
        nonlocal state
        if cp != state:
            cps.append(cp)
            state = cp

    if tgt["Grid"] > cur["Grid"]:
        # 1. Anode/Cathode lead at the current Grid; 2. Grid and they rise together to Grid target + offset
        push(_lift_cathode({**state, **{n: max(state[n], cur["Grid"] + offset) for n in others}}))
        push(_lift_cathode({**state, "Grid": tgt["Grid"], **{n: max(state[n], tgt["Grid"] + offset) for n in others}}))
    push(dict(tgt))  # 3. final ramp of the non-Grid channels (or a plain move when Grid does not rise)
    return cps


def plan_ramp_checkpoints(cur, tgt, offset=50.0, min_delta=20.0, waypoints=()):
    """Checkpoint list (setpoints) from state `cur` to `tgt`, optionally via `waypoints`, such that
    simultaneous equal-rate ramps between consecutive checkpoints never break the ordering rule.
    Raises ValueError if cur/waypoints/tgt themselves break it."""
    if offset < min_delta:
        raise ValueError(f"stage offset {offset:g} V must be >= min delta {min_delta:g} V")
    problems = ordering_violations(cur, min_delta, tol=0.01, floor=0.0)
    if problems:
        raise ValueError("current state breaks the rule: " + "; ".join(problems))
    plan, prev = [], dict(cur)
    for state in [*waypoints, tgt]:
        if set(state) != set(cur):
            raise ValueError("channel sets differ between current state and target")
        problems = ordering_violations(state, min_delta, tol=0.0, floor=-1.0)
        if problems:
            raise ValueError("target/waypoint breaks the rule: " + "; ".join(problems))
        plan += _plan_segment(prev, dict(state), offset)
        prev = dict(state)
    return plan