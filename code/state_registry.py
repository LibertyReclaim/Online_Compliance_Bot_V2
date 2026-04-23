"""State runner registry."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from states import california, connecticut, newjersey, newyork, texas

StateRunner = Callable[..., Any]


def get_state_runner(state_code: str) -> StateRunner:
    normalized = str(state_code).strip().upper()

    registry: dict[str, StateRunner] = {
        "NY": newyork.run,
        "CA": california.run,
        "CT": connecticut.run,
        "NJ": newjersey.run,
        "TX": texas.run,
    }
    print("Registered states:", ", ".join(registry.keys()))

    if normalized not in registry:
        raise ValueError(f"Unsupported state_code '{state_code}'. Supported: {', '.join(sorted(registry.keys()))}")

    return registry[normalized]


def get_registered_states() -> list[str]:
    return ["NY", "CA", "CT", "NJ", "TX"]
