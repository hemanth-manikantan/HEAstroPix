MAP3 = {"Grid": 0, "Anode": 1, "Cathode": 2}


def step(grid, hold, anode_offset=50.0, cathode_offset=100.0):
    return ({"Grid": grid, "Anode": grid + anode_offset, "Cathode": grid + cathode_offset}, hold)
