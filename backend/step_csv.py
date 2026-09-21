"""Step-sequence CSV parsing. No hardware imports: safe to use and test offline.

Row format:  ch,V,ch,V,...,hold_s   (physical channel numbers, volts, seconds)
Blank lines and lines starting with '#' are ignored. An optional header row
(first field non-numeric) is allowed as the first row only.
"""
import csv
import math
from dataclasses import dataclass


@dataclass(frozen=True)
class CsvStep:
    line: int
    voltages: dict  # physical channel index -> volts
    hold_s: float


def _to_float(text):
    value = float(text)
    if not math.isfinite(value):
        raise ValueError("not finite")
    return value


def parse_steps_csv(text, n_channels=6, max_v=9000.0):
    """Returns (steps, errors). If errors is non-empty, steps must not be used."""
    steps, errors = [], []
    seen_first_row = False

    for line_no, row in enumerate(csv.reader(text.splitlines()), start=1):
        fields = [f.strip() for f in row]
        while fields and fields[-1] == "":
            fields.pop()
        if not fields or fields[0].startswith("#"):
            continue

        if not seen_first_row:
            seen_first_row = True
            try:
                _to_float(fields[0])
            except ValueError:
                continue  # header row

        if "" in fields:
            errors.append(f"line {line_no}: empty field inside the row")
            continue
        if len(fields) < 3 or len(fields) % 2 == 0:
            errors.append(f"line {line_no}: expected ch,V,ch,V,...,hold_s (an odd number of fields, at least 3), got {len(fields)}")
            continue

        row_errors = []
        voltages = {}
        for i in range(0, len(fields) - 1, 2):
            ch_text, v_text = fields[i], fields[i + 1]
            try:
                ch = int(ch_text)
            except ValueError:
                row_errors.append(f"channel '{ch_text}' is not an integer")
                continue
            if not 0 <= ch < n_channels:
                row_errors.append(f"channel {ch} outside 0-{n_channels - 1}")
                continue
            if ch in voltages:
                row_errors.append(f"channel {ch} listed twice")
                continue
            try:
                v = _to_float(v_text)
            except ValueError:
                row_errors.append(f"voltage '{v_text}' for channel {ch} is not a number")
                continue
            if not 0.0 <= v <= max_v:
                row_errors.append(f"voltage {v} V for channel {ch} outside 0-{max_v:g} V")
                continue
            voltages[ch] = v

        try:
            hold = _to_float(fields[-1])
            if hold <= 0:
                row_errors.append(f"hold time {hold} s must be > 0")
        except ValueError:
            hold = None
            row_errors.append(f"hold time '{fields[-1]}' is not a number")

        if row_errors:
            errors.extend(f"line {line_no}: {e}" for e in row_errors)
        else:
            steps.append(CsvStep(line=line_no, voltages=voltages, hold_s=hold))

    if not steps and not errors:
        errors.append("no steps found in file")
    return steps, errors


def grid_offset_violations(targets, min_offset=10.0):
    """Non-Grid channels below Grid + min_offset (the DAQ step builder's rule)."""
    grid_v = targets.get("Grid", 0.0)
    return [name for name, v in targets.items() if name != "Grid" and v < grid_v + min_offset]


def _default_check(targets):
    bad = grid_offset_violations(targets)
    return [f"{', '.join(bad)} must be at least 10 V above the Grid"] if bad else []


def map_to_logical(steps, channel_map, check=_default_check):
    """Translate physical channels to logical names using the DAQ tab's channel_map
    ({"Grid": "Ch0", ...}). Every mapped channel must appear in every step, and no
    other channel may appear. `check(targets)` returns a list of problem strings; the
    default is the dashboard's Grid+10 V rule. Returns ([(targets, hold_s)], errors)."""
    phys_to_name = {int(ch.replace("Ch", "")): name for name, ch in channel_map.items()}
    out, errors = [], []
    for s in steps:
        unknown = sorted(set(s.voltages) - set(phys_to_name))
        missing = sorted(set(phys_to_name) - set(s.voltages))
        if unknown:
            errors.append(f"line {s.line}: channel(s) {unknown} are not in the current channel map")
        if missing:
            errors.append(f"line {s.line}: mapped channel(s) {missing} missing from the step")
        if unknown or missing:
            continue
        targets = {name: s.voltages[idx] for idx, name in phys_to_name.items()}
        problems = check(targets)
        if problems:
            errors.extend(f"line {s.line}: {p}" for p in problems)
            continue
        out.append((targets, s.hold_s))
    return out, errors
