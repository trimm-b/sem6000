"""Python API for the Voltcraft SEM6000 Bluetooth smart plug.

    import asyncio
    from sem6000 import SEM6000

    async def main():
        async with SEM6000("AA:BB:CC:DD:EE:FF") as plug:
            m = await plug.measure()
            print(f"{m.power_w} W, socket {'on' if m.on else 'off'}")
            await plug.turn_off()

    asyncio.run(main())
"""

from .client import (
    DEFAULT_PIN,
    SEM6000,
    AuthenticationError,
    NotConnectedError,
    SEM6000Error,
    discover,
)
from .energy import Integrator, Sample, measure_energy
from .logger import CsvSink, Row, SqliteSink, StdoutSink, run_logger
from .protocol import (
    DeviceInfo,
    Measurement,
    ProtocolError,
    Settings,
)
from .sync import SEM6000Sync

__version__ = "0.1.0"

__all__ = [
    "SEM6000",
    "SEM6000Sync",
    "discover",
    "DEFAULT_PIN",
    "Measurement",
    "Settings",
    "DeviceInfo",
    "Integrator",
    "Sample",
    "measure_energy",
    "run_logger",
    "CsvSink",
    "SqliteSink",
    "StdoutSink",
    "Row",
    "SEM6000Error",
    "AuthenticationError",
    "NotConnectedError",
    "ProtocolError",
]
