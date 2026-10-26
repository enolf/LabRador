import math

import pytest

from labrador.errors import (
    InstrumentNotConnectedError,
    InstrumentNotFoundError,
    InstrumentNotRespondingError,
    ObserveModeError,
    VisaNotFoundError,
)
from labrador.instruments import base
from labrador.instruments.base import Mode
from labrador.instruments.bench import Bench
from labrador.instruments.generator import Generator
from tests.fakes import FakeResourceManager, FakeVisaResource, visa_timeout

SCOPE_ADDRESS = "USB0::0x2A8D::0x0396::CN001::0::INSTR"
GENERATOR_ADDRESS = "USB0::0x0699::0x0353::C001::0::INSTR"
SCOPE_IDN = "KEYSIGHT TECHNOLOGIES,EDUX1052A,CN001,1.0"
GENERATOR_IDN = "TEKTRONIX,AFG1062,C001,SCPI:99.0 FV:1.0"


def scope_resource():
    return FakeVisaResource(SCOPE_ADDRESS, {"*IDN?": SCOPE_IDN, ":SYSTem:ERRor?": '+0,"No error"'})


def generator_resource():
    return FakeVisaResource(GENERATOR_ADDRESS, {"*IDN?": GENERATOR_IDN, "SYSTem:ERRor?": '0,""'})


def test_scope_only_bench_reports_missing_generator_when_needed():
    manager = FakeResourceManager({SCOPE_ADDRESS: scope_resource()})
    bench = Bench.connect(scope=True, generator=True, mode=Mode.CONTROL, resource_manager=manager)
    assert bench.has_scope()
    assert not bench.has_generator()
    assert bench.status() == {"scope": "connected", "generator": "not found"}
    with pytest.raises(InstrumentNotConnectedError, match="function generator") as raised:
        _ = bench.generator
    assert isinstance(raised.value.__cause__, InstrumentNotFoundError)


def test_generator_only_bench_reports_missing_scope_when_needed():
    manager = FakeResourceManager({GENERATOR_ADDRESS: generator_resource()})
    bench = Bench.connect(scope=True, generator=True, mode=Mode.CONTROL, resource_manager=manager)
    assert bench.generator.identity.model == "AFG1062"
    assert bench.status()["scope"] == "not found"
    with pytest.raises(InstrumentNotConnectedError, match="oscilloscope"):
        _ = bench.scope


def test_neither_instrument_is_a_normal_state_until_used():
    bench = Bench.connect(
        scope=True, generator=True, mode=Mode.CONTROL, resource_manager=FakeResourceManager({})
    )
    assert bench.status() == {"scope": "not found", "generator": "not found"}
    with pytest.raises(InstrumentNotConnectedError):
        _ = bench.scope


def test_unresponsive_instrument_is_recorded_as_cause():
    silent = scope_resource()
    silent.query_error = visa_timeout()
    bench = Bench.connect(resource_manager=FakeResourceManager({SCOPE_ADDRESS: silent}))
    assert bench.status()["scope"] == "not responding"
    with pytest.raises(InstrumentNotConnectedError) as raised:
        _ = bench.scope
    assert isinstance(raised.value.__cause__, InstrumentNotRespondingError)


def test_only_requested_instruments_are_opened():
    manager = FakeResourceManager(
        {SCOPE_ADDRESS: scope_resource(), GENERATOR_ADDRESS: generator_resource()}
    )
    bench = Bench.connect(scope=True, generator=False, mode=Mode.CONTROL, resource_manager=manager)
    assert manager.opened == [SCOPE_ADDRESS]
    assert bench.status()["generator"] == "not requested"
    with pytest.raises(InstrumentNotConnectedError, match="not requested"):
        _ = bench.generator


def test_blank_address_means_auto_discover():
    manager = FakeResourceManager({SCOPE_ADDRESS: scope_resource()})
    bench = Bench.connect(scope_address="", resource_manager=manager)
    assert bench.scope.address == SCOPE_ADDRESS


def test_missing_visa_is_raised_immediately(monkeypatch):
    def no_visa():
        raise ValueError("Could not locate a VISA implementation")

    monkeypatch.setattr(base.pyvisa, "ResourceManager", no_visa)
    with pytest.raises(VisaNotFoundError):
        Bench.connect()


def test_observe_mode_never_holds_the_generator():
    manager = FakeResourceManager(
        {SCOPE_ADDRESS: scope_resource(), GENERATOR_ADDRESS: generator_resource()}
    )
    bench = Bench.connect(generator=True, generator_metadata=False, resource_manager=manager)
    assert GENERATOR_ADDRESS not in manager.opened
    assert bench.status()["generator"] == "not used (observe mode)"
    with pytest.raises(ObserveModeError, match="front panel"):
        _ = bench.generator
    assert bench.generator_settings() is None
    assert GENERATOR_ADDRESS not in manager.opened


def test_observe_generator_metadata_opens_queries_and_releases():
    generator = generator_resource()
    generator.replies.update(
        {
            "SOURce1:FUNCtion:SHAPe?": "SIN",
            "SOURce2:FUNCtion:SHAPe?": "SQU",
            "SOURce1:FREQuency:FIXed?": "1.0E+03",
            "SOURce2:FREQuency:FIXed?": "2.0E+03",
            "SOURce1:VOLTage:LEVel:IMMediate:AMPLitude?": "1.0",
            "SOURce2:VOLTage:LEVel:IMMediate:AMPLitude?": "2.0",
            "SOURce1:VOLTage:LEVel:IMMediate:OFFSet?": "0",
            "SOURce2:VOLTage:LEVel:IMMediate:OFFSet?": "0",
            "OUTPut1:IMPedance?": "9.9E+37",
            "OUTPut2:IMPedance?": b"50\xa6\xb8",
            "OUTPut1:STATe?": "1",
            "OUTPut2:STATe?": "0",
        }
    )
    manager = FakeResourceManager({SCOPE_ADDRESS: scope_resource(), GENERATOR_ADDRESS: generator})
    bench = Bench.connect(generator_metadata=True, resource_manager=manager)
    settings = bench.generator_settings()
    assert settings.channels[1].load_ohms == math.inf
    assert settings.channels[2].shape == "SQU"
    assert settings.channels[2].load_ohms == 50.0
    assert generator.closed
    assert generator.ren_calls
    assert generator.written == []
    assert bench.status()["generator"] == "queried briefly for settings (observe mode)"


def test_generator_metadata_is_on_by_default_after_the_hardware_check():
    bench = Bench.connect(resource_manager=FakeResourceManager({SCOPE_ADDRESS: scope_resource()}))
    assert bench.generator_metadata is True


def test_observe_generator_metadata_returns_none_when_absent():
    manager = FakeResourceManager({SCOPE_ADDRESS: scope_resource()})
    bench = Bench.connect(generator_metadata=True, resource_manager=manager)
    assert bench.generator_settings() is None


def test_close_releases_each_instrument_independently():
    scope = scope_resource()
    generator = generator_resource()
    generator.ren_error = RuntimeError("not supported")
    manager = FakeResourceManager({SCOPE_ADDRESS: scope, GENERATOR_ADDRESS: generator})
    with Bench.connect(generator=True, mode=Mode.CONTROL, resource_manager=manager):
        pass
    assert scope.closed and scope.ren_calls
    assert generator.closed


def test_simulated_bench_links_generator_to_scope():
    with Bench.connect(generator=True, mode=Mode.CONTROL, simulated=True) as bench:
        assert isinstance(bench.generator, Generator)
        assert bench.simulation is not None
        bench.generator.configure(1, frequency=2e3, amplitude=1.0, load=math.inf, output=True)
        waveform = bench.scope.read_waveform(1)
        assert waveform.volts.max() - waveform.volts.min() == pytest.approx(1.0, abs=0.05)


def test_simulated_observe_bench_reads_generator_metadata_repeatedly():
    bench = Bench.connect(simulated=True, generator_metadata=True)
    first = bench.generator_settings()
    second = bench.generator_settings()
    assert first == second
    assert bench.simulation.generator_device.go_to_local_calls == 2
