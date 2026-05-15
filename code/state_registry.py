"""State runner registry."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from states import alabama, arkansas, california, connecticut, delaware, illinois, iowa, kansas, louisiana, maine, maryland, minnesota, nebraska, massachusetts, michigan, newjersey, newyork, north_carolina, ohio, south_carolina, texas
import states.indiana as indiana
import states.virginia as virginia

StateRunner = Callable[..., Any]


def get_state_runner(state_code: str) -> StateRunner:
    normalized = str(state_code).strip().upper()

    registry: dict[str, StateRunner] = {
        "NY": newyork.run,
        "CA": california.run,
        "CT": connecticut.run,
        "NJ": newjersey.run,
        "TX": texas.run,
        "IL": illinois.run,
        "OH": ohio.run,
        "MI": michigan.run,
        "MA": massachusetts.run,
        "IN": indiana.run,
        "VA": virginia.run,
        "MD": maryland.run,
        "DE": delaware.run,
        "NC": north_carolina.run,
        "SC": south_carolina.run_south_carolina,
        "LA": louisiana.run_louisiana,
        "AL": alabama.run_alabama,
        "AR": arkansas.run_arkansas,
        "IA": iowa.run_iowa,
        "KS": kansas.run_kansas,
        "ME": maine.run_maine,
        "MN": minnesota.run_minnesota,
        "NE": nebraska.run_nebraska,
    }
    print("Registered states:", ", ".join(registry.keys()))

    if normalized not in registry:
        raise ValueError(f"Unsupported state_code '{state_code}'. Supported: {', '.join(sorted(registry.keys()))}")

    return registry[normalized]


def get_registered_states() -> list[str]:
    return ["NY", "CA", "CT", "NJ", "TX", "IL", "OH", "MI", "MA", "IN", "VA", "MD", "DE", "NC", "SC", "LA", "AL", "AR", "IA", "KS", "ME", "MN", "NE"]
