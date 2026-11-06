"""The Eldom integration models."""

from dataclasses import dataclass

from .coordinator import EldomCoordinator
from .energy import EldomEnergyCoordinator
from .weekly_manager import WeeklyProgramManager


@dataclass
class EldomData:
    """Data for the Eldom integration."""

    coordinator: EldomCoordinator
    energy_coordinator: EldomEnergyCoordinator | None = None
    weekly_manager: WeeklyProgramManager | None = None
