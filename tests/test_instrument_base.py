import logging

import pytest
import pyvisa
from pyvisa import constants

from labrador.errors import (
    InstrumentCommandError,
    InstrumentNotFoundError,
    InstrumentNotRespondingError,
    ObserveModeError,
    UnexpectedReplyError,
    VisaNotFoundError,
)
from labrador.instruments import base
from labrador.instruments.base import Identity, Mode, open_resource_manager, usb_vendor_id
from labrador.instruments.transport import TransportError
from tests.fakes import (
    FakeInstrument,
    FakeResourceManager,
    FakeVisaResource,
    ScriptedTransport,
    visa_timeout,
)


def make(transport=None, mode=Mode.OBSERVE):
    return FakeInstrument(transport or ScriptedTransport({":LEVel?": "1.5"}), mode=mode)


def test_identity_parsed_from_idn():
    instrument = make()
    assert instrument.identity == Identity(
        "ACME", "FAKE1000", "SN42", "1.0", "ACME,FAKE1000,SN42,1.0"
    )


def test_identity_tolerates_short_reply():
    assert Identity.parse("ACME,X").firmware == ""


def test_wrong_model_is_not_found():
    with pytest.raises(InstrumentNotFoundError, match="OTHER"):
        FakeInstrument(ScriptedTransport(idn="ACME,OTHER,1,1"))


def test_old_errors_are_drained_on_connect(caplog):
    transport = ScriptedTransport(errors=['-113,"Undefined header"', '-222,"Data out of range"'])
    with caplog.at_level(logging.INFO, logger="labrador.instruments"):
        make(transport)
    assert transport.error_queue == []
    assert "discarded old errors" in caplog.text


def test_observe_mode_is_default_and_refuses_settings_without_sending():
    transport = ScriptedTransport()
    instrument = make(transport)
    sent_before = list(transport.sent)
    assert instrument.mode is Mode.OBSERVE
    with pytest.raises(ObserveModeError):
        instrument.write(":LEVel 2")
    assert transport.sent == sent_before


def test_observe_mode_allows_listed_queries_and_transfer_settings():
    instrument = make()
    assert instrument.query_float(":LEVel?") == 1.5
    instrument.write(":WAVeform:SOURce CHANnel1")


def test_control_mode_write_checks_error_queue():
    transport = ScriptedTransport()
    transport.errors_after_write[":LEVel 99"] = '-222,"Data out of range"'
    instrument = make(transport, Mode.CONTROL)
    instrument.write(":LEVel 2")
    with pytest.raises(InstrumentCommandError) as raised:
        instrument.write(":LEVel 99")
    assert raised.value.code == -222
    assert raised.value.error_message == "Data out of range"


def test_error_check_can_be_disabled():
    transport = ScriptedTransport()
    transport.errors_after_write[":LEVel 99"] = '-222,"Data out of range"'
    instrument = FakeInstrument(transport, mode=Mode.CONTROL, check_errors=False)
    instrument.write(":LEVel 99")
    assert transport.error_queue == ['-222,"Data out of range"']


def test_lock_is_refused_even_in_control_mode():
    transport = ScriptedTransport()
    instrument = make(transport, Mode.CONTROL)
    with pytest.raises(ObserveModeError, match="lock"):
        instrument.write(":SYSTem:LOCK ON")
    assert ":SYSTem:LOCK ON" not in transport.sent


def test_mode_can_be_switched():
    instrument = make()
    instrument.mode = Mode.CONTROL
    instrument.write(":LEVel 2")
    instrument.mode = "observe"
    with pytest.raises(ObserveModeError):
        instrument.write(":LEVel 2")


def test_transport_failure_becomes_not_responding():
    transport = ScriptedTransport({":LEVel?": "1"})
    instrument = make(transport)
    transport.failing[":LEVel?"] = TransportError("timeout", timed_out=True)
    with pytest.raises(InstrumentNotRespondingError):
        instrument.query(":LEVel?")


def test_timeout_override_is_restored_after_failure():
    transport = ScriptedTransport({":LEVel?": "1"})
    instrument = make(transport)
    transport.failing[":LEVel?"] = TransportError("timeout", timed_out=True)
    with pytest.raises(InstrumentNotRespondingError):
        instrument.query(":LEVel?", timeout_s=20)
    assert transport.timeout_s == 5.0


def test_timeout_override_applies_during_query():
    transport = ScriptedTransport({":LEVel?": "1"})
    instrument = make(transport)
    instrument.query(":LEVel?", timeout_s=20)
    assert transport.timeout_history[-1] == 20
    assert transport.timeout_s == 5.0


def test_non_numeric_reply_to_numeric_query_is_unexpected_reply():
    instrument = make(ScriptedTransport({":LEVel?": "high"}))
    with pytest.raises(UnexpectedReplyError, match="high"):
        instrument.query_float(":LEVel?")


def test_unparsable_error_reply_is_not_responding():
    transport = ScriptedTransport(errors=["garbage"])
    with pytest.raises(InstrumentNotRespondingError, match="garbage"):
        make(transport)


def test_close_goes_to_local_then_closes_once():
    transport = ScriptedTransport()
    instrument = make(transport)
    instrument.close()
    instrument.close()
    assert transport.calls == ["go_to_local", "close"]
    assert instrument.closed


def test_close_never_raises():
    transport = ScriptedTransport()
    transport.go_to_local_result = RuntimeError("no")
    instrument = make(transport)
    instrument.close()
    assert transport.calls == ["go_to_local", "close"]


def test_go_to_local_without_closing_reports_support():
    transport = ScriptedTransport({":LEVel?": "1"})
    instrument = make(transport)
    assert instrument.go_to_local() is True
    transport.go_to_local_result = False
    assert instrument.go_to_local() is False
    transport.go_to_local_result = RuntimeError("no")
    assert instrument.go_to_local() is False
    assert instrument.query(":LEVel?") == "1"
    assert "close" not in transport.calls


def test_closed_instrument_refuses_io():
    instrument = make()
    instrument.close()
    with pytest.raises(InstrumentNotRespondingError, match="closed"):
        instrument.query("*IDN?")


def test_context_manager_closes():
    transport = ScriptedTransport()
    with make(transport):
        pass
    assert "close" in transport.calls


@pytest.mark.parametrize(
    "failure", [ValueError("Could not locate a VISA implementation"), OSError("bad dll")]
)
def test_missing_visa_raises_visa_not_found(monkeypatch, failure):
    def raise_failure():
        raise failure

    monkeypatch.setattr(base.pyvisa, "ResourceManager", raise_failure)
    with pytest.raises(VisaNotFoundError, match="R&S VISA") as raised:
        open_resource_manager()
    assert str(failure) in raised.value.detail


def test_visa_not_found_message_includes_download_page_when_given():
    assert "https://example.test/visa" in str(
        VisaNotFoundError(download_page="https://example.test/visa")
    )


@pytest.mark.parametrize(
    ("address", "expected"),
    [
        ("USB0::0x2A8D::0x0396::CN123::0::INSTR", 0x2A8D),
        ("USB0::10893::918::CN123::0::INSTR", 10893),
        ("ASRL11::INSTR", None),
        ("USB0::bad::1::2::INSTR", None),
    ],
)
def test_usb_vendor_id(address, expected):
    assert usb_vendor_id(address) == expected


FAKE_ADDRESS = "USB0::0x1234::0x0001::SN42::0::INSTR"


def test_open_auto_discovers_by_vendor_and_model():
    manager = FakeResourceManager(
        {
            "ASRL11::INSTR": FakeVisaResource("ASRL11::INSTR"),
            "USB0::0x1234::0x0002::OTHER::0::INSTR": FakeVisaResource(
                "other", {"*IDN?": "ACME,OTHER,1,1"}
            ),
            FAKE_ADDRESS: FakeVisaResource(FAKE_ADDRESS),
        }
    )
    instrument = FakeInstrument.open(resource_manager=manager)
    assert instrument.address == FAKE_ADDRESS
    assert "ASRL11::INSTR" not in manager.opened
    assert manager.resources["USB0::0x1234::0x0002::OTHER::0::INSTR"].closed


def test_open_with_no_matching_vendor_is_not_found():
    manager = FakeResourceManager({"ASRL11::INSTR": FakeVisaResource("ASRL11::INSTR")})
    with pytest.raises(InstrumentNotFoundError, match="0x1234"):
        FakeInstrument.open(resource_manager=manager)


def test_open_with_empty_resource_list_is_not_found():
    with pytest.raises(InstrumentNotFoundError, match="none"):
        FakeInstrument.open(resource_manager=FakeResourceManager({}))


def test_open_explicit_missing_address_is_not_found():
    with pytest.raises(InstrumentNotFoundError):
        FakeInstrument.open(FAKE_ADDRESS, resource_manager=FakeResourceManager({}))


def test_open_busy_resource_is_not_found():
    busy = pyvisa.VisaIOError(constants.StatusCode.error_resource_busy)
    with pytest.raises(InstrumentNotFoundError):
        FakeInstrument.open(
            FAKE_ADDRESS, resource_manager=FakeResourceManager({FAKE_ADDRESS: busy})
        )


def test_open_silent_instrument_is_not_responding_and_closed():
    resource = FakeVisaResource(FAKE_ADDRESS)
    resource.query_error = visa_timeout()
    with pytest.raises(InstrumentNotRespondingError):
        FakeInstrument.open(resource_manager=FakeResourceManager({FAKE_ADDRESS: resource}))
    assert resource.closed


def test_open_sets_timeout_and_terminations():
    resource = FakeVisaResource(FAKE_ADDRESS)
    FakeInstrument.open(
        FAKE_ADDRESS, timeout_s=3, resource_manager=FakeResourceManager({FAKE_ADDRESS: resource})
    )
    assert resource.timeout == 3000
    assert resource.read_termination == "\n"


def test_find_returns_confirmed_addresses_and_closes_them():
    resource = FakeVisaResource(FAKE_ADDRESS)
    manager = FakeResourceManager(
        {
            FAKE_ADDRESS: resource,
            "USB0::0x1234::0x0002::X::0::INSTR": FakeVisaResource("x", {"*IDN?": "A,B,C,D"}),
        }
    )
    assert FakeInstrument.find(manager) == [FAKE_ADDRESS]
    assert resource.closed


def test_open_simulated_without_simulator_is_an_error():
    with pytest.raises(NotImplementedError):
        FakeInstrument.open(simulated=True)
