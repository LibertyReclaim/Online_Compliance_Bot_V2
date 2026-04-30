"""State runner registry."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from states import california, connecticut, illinois, massachusetts, michigan, newjersey, newyork, ohio, texas
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
    }
    print("Registered states:", ", ".join(registry.keys()))

    if normalized not in registry:
        raise ValueError(f"Unsupported state_code '{state_code}'. Supported: {', '.join(sorted(registry.keys()))}")

    return registry[normalized]


def get_registered_states() -> list[str]:
    return ["NY", "CA", "CT", "NJ", "TX", "IL", "OH", "MI", "MA", "IN", "VA"]
