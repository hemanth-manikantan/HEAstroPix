"""Generate a steps.csv for a 3-electrode voltage scan (Grid x Anode x Cathode matrix).

No hardware imports, no file I/O: safe to use offline and reusable from a future Streamlit tab (every
parameter here maps 1:1 to a plain numeric input widget). generate_scan_csv.py is the CLI wrapper that
writes the result to disk; the file it writes loads straight into run_steps.py.

Anode = Grid + anode_offset, Cathode = Anode + cathode_offset, so the offsets -- not raw volts -- are
what you sweep for the two inner electrodes. Every combination is checked against the same electrode
ordering rule run_steps.py enforces (backend.safety_rules.ordering_violations) before anything is
returned, so a bad range is rejected up front rather than failing mid-scan.
"""
from dataclasses import dataclass

from backend.safety_rules import ordering_violations
from backend.step_csv import parse_steps_csv, map_to_logical

_DAQ_COLUMN = {"Y": "Y", "N": "N", None: None}


@dataclass(frozen=True)
class ScanStep:
    grid: float
    anode: float
    cathode: float
    hold_s: float


def inclusive_range(lo, hi, step, label):
    """lo, lo+step, ... up to and including hi (round() absorbs float drift, e.g. (150-20)/10)."""
    if step <= 0:
        raise ValueError(f"{label} step must be > 0")
    if hi < lo:
        raise ValueError(f"{label} max ({hi:g}) must be >= {label} min ({lo:g})")
    n = round((hi - lo) / step)
    return [lo + i * step for i in range(n + 1)]


def generate_scan_steps(*, grid_min, grid_max, grid_step,
                        anode_offset_min, anode_offset_max, anode_offset_step,
                        cathode_offset_min, cathode_offset_max, cathode_offset_step,
                        hold_s, min_delta=20.0):
    """Returns a list of ScanStep, in Grid-major / Anode-mid / Cathode-minor order. Raises ValueError
    (listing every offending combination, up to 10) if any point in the matrix would break Anode/Cathode
    >= Grid + min_delta or Cathode > Anode -- before a single step is returned."""
    if hold_s <= 0:
        raise ValueError(f"hold time {hold_s:g} s must be > 0")
    grids = inclusive_range(grid_min, grid_max, grid_step, "grid")
    anode_offsets = inclusive_range(anode_offset_min, anode_offset_max, anode_offset_step, "anode offset")
    cathode_offsets = inclusive_range(cathode_offset_min, cathode_offset_max, cathode_offset_step, "cathode offset")

    steps, bad = [], []
    for grid in grids:
        for a_off in anode_offsets:
            anode = grid + a_off
            for c_off in cathode_offsets:
                cathode = anode + c_off
                targets = {"Grid": grid, "Anode": anode, "Cathode": cathode}
                problems = ordering_violations(targets, min_delta, tol=0.0, floor=-1.0, strict_cathode=True)
                if problems:
                    bad.append(f"Grid={grid:g} Anode={anode:g} Cathode={cathode:g}: " + "; ".join(problems))
                else:
                    steps.append(ScanStep(grid, anode, cathode, hold_s))
    if bad:
        more = f"\n... and {len(bad) - 10} more" if len(bad) > 10 else ""
        raise ValueError(f"{len(bad)} combination(s) break the electrode rule:\n" + "\n".join(bad[:10]) + more)
    if not steps:
        raise ValueError("no steps generated (check the ranges)")
    return steps


def scan_csv_text(steps, channels, daq="Y", comment=None):
    """channels: physical channel numbers, e.g. {"Grid": 0, "Anode": 4, "Cathode": 5}. daq: "Y", "N", or
    None to omit the column (follow the run's own --daq-cmd default). comment: optional leading text,
    written one '# '-prefixed line per input line."""
    missing = sorted({"Grid", "Anode", "Cathode"} - set(channels))
    if missing:
        raise ValueError(f"channels missing {missing}; need exactly Grid, Anode and Cathode")
    if daq not in _DAQ_COLUMN:
        raise ValueError("daq must be 'Y', 'N' or None")

    lines = []
    if comment:
        lines += [f"# {line}" if line else "#" for line in comment.splitlines()]
    for s in steps:
        fields = []
        for name, v in (("Grid", s.grid), ("Anode", s.anode), ("Cathode", s.cathode)):
            fields += [str(channels[name]), f"{v:g}"]
        fields.append(f"{s.hold_s:g}")
        if daq:
            fields.append(daq)
        lines.append(",".join(fields))
    return "\n".join(lines) + "\n"


def self_check(csv_text, channels, min_delta=20.0):
    """Round-trips csv_text through the exact parser run_steps.py uses, so a generation bug can never
    write out a file that run_steps.py would then refuse. Returns a list of problems (empty = clean)."""
    parsed, errors = parse_steps_csv(csv_text)
    if errors:
        return errors
    rule = lambda t: ordering_violations(t, min_delta, tol=0.0, floor=-1.0, strict_cathode=True)
    _, errors = map_to_logical(parsed, {n: f"Ch{i}" for n, i in channels.items()}, check=rule)
    return errors
