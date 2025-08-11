import can
import pytest

@pytest.fixture
def bus_factory():
    """Create virtual CAN buses and ensure they are shut down after use."""
    buses = []

    def factory(*, bitrate=500000):
        bus = can.interface.Bus(interface="virtual", bitrate=bitrate, receive_own_messages=True)
        buses.append(bus)
        return bus

    yield factory

    for bus in buses:
        bus.shutdown()
