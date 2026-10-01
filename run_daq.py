#!/usr/bin/env python
"""One Timepix3 data-taking run with explicit settings files, for use as run_steps.py's DAQ command.

Loads the backup (DAC settings), mask and equalisation by FULL PATH (tpx3_cli's own Load_* commands only
accept names inside ~/Timepix3/... and print "loaded" even when the file was not found, so this verifies
what was actually loaded), then calls the same Run_Datataking function the tpx3 CLI uses.

Prints one 'DAQ_OUTPUT: <path>' line per new data file so the HV runner can log it against the step.
--continuous runs with no fixed duration (tpx3-daq scan_timeout=0) until stopped by SIGINT/SIGTERM,
for continuous monitoring during an HV ramp; a deliberate stop with a valid file is then exit 0, not 130.
Exit codes: 0 ok | 1 bad input / settings not loaded (nothing acquired) | 3 no data file produced
            130 interrupted with --duration (fixed-length run stopped early; the partial data is kept)
Needs the environment where tpx3-daq is installed (the editable install in hypex2).
"""
import argparse
import json
import multiprocessing
import signal
import sys
import time
from pathlib import Path


class BackendUnavailable(Exception):
    pass


class Tpx3Backend:
    """Thin wrapper over tpx3-daq's global datalogger and CLI function object (imported lazily)."""

    def __init__(self):
        try:
            from UI.tpx3_logger import TPX3_datalogger
            from UI.CLI.tpx3_cli import TPX3_CLI_function_call
        except Exception as e:
            raise BackendUnavailable(f"cannot import tpx3-daq ({type(e).__name__}: {e}) in {sys.executable}") from e
        self._log, self._calls = TPX3_datalogger, TPX3_CLI_function_call()

    def set_data(self, data):
        return bool(self._log.set_data(config=data))

    def write_backup_to_yaml(self):
        self._log.write_backup_to_yaml()

    def write_value(self, name, value):
        return self._log.write_value(name=name, value=value)

    def read_value(self, name):
        return self._log.read_value(name=name)

    def run_datataking(self, seconds):
        self._calls.Run_Datataking(scan_timeout=seconds)


def check_daq_inputs(backup, mask, equalisation):
    """Pure file checks (no tpx3 import, no hardware). Returns a list of error strings."""
    errors = []
    for label, p in (("backup", backup), ("mask", mask), ("equalisation", equalisation)):
        path = Path(p).expanduser()
        if not path.is_file():
            errors.append(f"{label} file not found: {path}")
        elif path.stat().st_size == 0:
            errors.append(f"{label} file is empty: {path}")
    if not errors:
        try:
            if not isinstance(json.loads(Path(backup).expanduser().read_text()), dict):
                errors.append("backup file is not a settings dictionary")
        except ValueError as e:
            errors.append(f"backup file is not valid JSON: {e}")
    return errors


def whole_seconds(duration):
    """tpx3-daq treats scan_timeout=0 as an INFINITE run, so anything that rounds to 0 is refused."""
    seconds = int(round(float(duration)))
    if seconds < 1:
        raise ValueError(f"duration {duration} s rounds to {seconds}; 0 would mean an infinite run")
    return seconds


def new_data_files(data_dir, before, t_start):
    found = []
    for p in sorted(Path(data_dir).glob("*.h5")):
        mtime = p.stat().st_mtime
        if (p not in before or mtime > before[p]) and mtime >= t_start - 2:
            found.append(p)
    return found


def build_parser():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--backup", required=True, help="DAC settings backup (.TPX3), full path")
    p.add_argument("--mask", required=True, help="mask file (.h5), full path")
    p.add_argument("--equalisation", required=True, help="equalisation file (.h5), full path")
    p.add_argument("--duration", type=float, help="data-taking time in seconds (>= 1); exactly one of "
                                                   "--duration/--continuous is required")
    p.add_argument("--continuous", action="store_true", help="run with no fixed duration, until stopped "
                                                              "by SIGINT/SIGTERM (see module docstring)")
    p.add_argument("--step", help="HV step number, only for the log")
    p.add_argument("--data-dir", help="where tpx3-daq writes runs (default ~/Timepix3/data/hdf)")
    return p


def main(argv=None, backend=None):
    args = build_parser().parse_args(argv)
    errors = check_daq_inputs(args.backup, args.mask, args.equalisation)
    seconds = 0
    if args.continuous and args.duration is not None:
        errors.append("use either --continuous or --duration, not both")
    elif not args.continuous and args.duration is None:
        errors.append("either --continuous or --duration is required")
    elif not args.continuous:
        try:
            seconds = whole_seconds(args.duration)
        except ValueError as e:
            errors.append(str(e))
    if errors:
        print("DAQ not started:\n" + "\n".join(f"  - {e}" for e in errors), file=sys.stderr)
        return 1
    backup, mask, equal = (str(Path(p).expanduser().resolve()) for p in (args.backup, args.mask, args.equalisation))

    try:
        backend = backend or Tpx3Backend()
    except BackendUnavailable as e:
        print(f"DAQ not started: {e}", file=sys.stderr)
        return 1

    if not backend.set_data(json.loads(Path(backup).read_text())):
        print("DAQ not started: tpx3-daq rejected the backup file (corrupted or invalid).", file=sys.stderr)
        return 1
    backend.write_backup_to_yaml()
    for name, path in (("Mask_path", mask), ("Equalisation_path", equal)):
        if backend.write_value(name, path) is not True or backend.read_value(name) != path:
            print(f"DAQ not started: could not set {name} to {path}", file=sys.stderr)
            return 1
    duration_note = "continuous" if args.continuous else seconds
    print(f"DAQ_CONFIG: step={args.step} backup={backup} mask={mask} equalisation={equal} duration_s={duration_note}", flush=True)

    data_dir = Path(args.data_dir) if args.data_dir else Path.home() / "Timepix3" / "data" / "hdf"
    before = {p: p.stat().st_mtime for p in data_dir.glob("*.h5")} if data_dir.is_dir() else {}

    def stop(signum, frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, stop)

    t_start, interrupted = time.time(), False
    try:
        backend.run_datataking(seconds)
    except KeyboardInterrupt:
        interrupted = True
        print("DAQ interrupted; waiting for the acquisition process to close its file", flush=True)
    finally:
        for child in multiprocessing.active_children():
            child.join(timeout=60)

    files = new_data_files(data_dir, before, t_start) if data_dir.is_dir() else []
    for f in files:
        print(f"DAQ_OUTPUT: {f}", flush=True)
    have_data = bool(files) and any(f.stat().st_size > 0 for f in files)

    if args.continuous:
        # A SIGINT/SIGTERM is the ONLY way a continuous run ever ends, so a deliberate stop that
        # produced a valid file is success (0), not the "interrupted early" 130 used for --duration.
        if have_data:
            print(f"DAQ finished (stopped on request): {len(files)} data file(s)", flush=True)
            return 0
        print(f"DAQ FAILED: no data file appeared in {data_dir} (stopped before any data was captured?)",
              file=sys.stderr)
        return 3

    if interrupted:
        return 130
    if not have_data:
        print(f"DAQ FAILED: no data file appeared in {data_dir} (check the messages above)", file=sys.stderr)
        return 3
    print(f"DAQ finished: {len(files)} data file(s)", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
