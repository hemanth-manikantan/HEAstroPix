import logging
import socket
import sys

import pytest

import backend.hvlogic as hvlogic
from backend.simulated_hv import SimClock, SimulatedDetectorHV
from tests.helpers import MAP3


@pytest.fixture(autouse=True)
def offline(monkeypatch, tmp_path):
    """No test may reach real hardware, and none may write into the repo."""
    monkeypatch.chdir(tmp_path)

    def no_caen():
        raise RuntimeError("tests must never load caen_libs / touch the real crate")

    monkeypatch.setattr(hvlogic, "_caen", no_caen)
    monkeypatch.setitem(sys.modules, "tpx3", None)   # the Timepix3 DAQ must never be imported/started by a test
    monkeypatch.setitem(sys.modules, "UI", None)

    def no_remote(self, address, *a, **k):
        if isinstance(address, tuple) and address[0] not in ("127.0.0.1", "::1", "localhost"):
            raise RuntimeError(f"tests must not open network connections (tried {address})")
        return real_connect(self, address, *a, **k)

    real_connect = socket.socket.connect
    monkeypatch.setattr(socket.socket, "connect", no_remote)


@pytest.fixture
def vclock(monkeypatch):
    clock = SimClock()
    monkeypatch.setattr(hvlogic, "time", clock)
    return clock


@pytest.fixture
def log():
    logger = logging.getLogger("hv_steps_test")
    logger.setLevel(logging.INFO)
    return logger


@pytest.fixture
def make_runner(vclock, log, tmp_path):
    from backend.step_runner import StepRunner

    def factory(steps, *, hv=None, **kw):
        hv = hv or SimulatedDetectorHV(clock=vclock)
        kw.setdefault("power_on", True)
        return hv, StepRunner(hv, MAP3, steps, tmp_path / "out", clock=vclock, log=log, **kw)

    (tmp_path / "out").mkdir()
    return factory
