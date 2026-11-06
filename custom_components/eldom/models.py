"""The Eldom integration models."""

from dataclasses import dataclass

from .coordinator import EldomCoordinator
from .energy import EldomEnergyCoordinator


@dataclass
class EldomData:
    """Data for the Eldom integration."""

    coordinator: EldomCoordinator
    energy_coordinator: EldomEnergyCoordinator | None = None
