"""Simulation doubles for the test seams of TS §0.3: clock, PID checker, scripted prices, calendar, fault injector.

Nothing here is imported by production code. The doubles are deterministic and in-process; the fault injector
simulates a process kill (``SimulatedKill``) at a named write step of the SS §7.1 commit order.
"""

from stockmanager.sim.calendar import FakeCalendar
from stockmanager.sim.channels import RecordingChannel
from stockmanager.sim.clock import PT, FakeClock, FakePidChecker
from stockmanager.sim.faults import CRASH_POINTS, FaultInjector, SimulatedKill
from stockmanager.sim.prices import ScriptedPriceProvider
from stockmanager.sim.reasoner import StubReasoner
from stockmanager.sim.verifier import FakeSymbolVerifier

__all__ = [
    "CRASH_POINTS",
    "PT",
    "FakeCalendar",
    "FakeClock",
    "FakePidChecker",
    "FakeSymbolVerifier",
    "FaultInjector",
    "RecordingChannel",
    "ScriptedPriceProvider",
    "SimulatedKill",
    "StubReasoner",
]
