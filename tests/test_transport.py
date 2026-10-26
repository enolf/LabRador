import pytest
import pyvisa
from pyvisa import constants

from labrador.instruments.transport import TransportError, VisaTransport
from tests.fakes import FakeVisaResource, visa_timeout


def test_sets_line_feed_termination_and_timeout():
    resource = FakeVisaResource("USB0::1::2::3::INSTR")
    transport = VisaTransport(resource, timeout_s=2.5)
    assert resource.read_termination == "\n"
    assert resource.write_termination == "\n"
    assert resource.timeout == 2500
    transport.timeout_s = 20
    assert resource.timeout == 20000
    assert transport.timeout_s == 20.0


def test_query_strips_reply():
    transport = VisaTransport(FakeVisaResource("X", {":TER?": " +1 "}), 1)
    assert transport.query(":TER?") == "+1"


def test_query_decodes_with_the_instrument_encoding():
    resource = FakeVisaResource("X", {"OUTPut1:IMPedance?": b"50\xa6\xb8"})
    assert VisaTransport(resource, 1, encoding="gbk").query("OUTPut1:IMPedance?") == "50Ω"


def test_undecodable_reply_is_kept_visible_instead_of_raising():
    resource = FakeVisaResource("X", {"OUTPut1:IMPedance?": b"50\xa6\xb8"})
    assert VisaTransport(resource, 1).query("OUTPut1:IMPedance?") == "50\\xa6\\xb8"


def test_query_block_reads_ieee_block_as_bytes_and_consumes_terminator():
    resource = FakeVisaResource("X")
    resource.block = b"\x00\x01\x80\xff"
    transport = VisaTransport(resource, 1)
    assert transport.query_block(":WAV:DATA?") == b"\x00\x01\x80\xff"
    message, options = resource.binary_calls[0]
    assert message == ":WAV:DATA?"
    assert options == {"datatype": "B", "container": bytes, "expect_termination": True}


def test_timeout_becomes_transport_error_marked_timed_out():
    resource = FakeVisaResource("X")
    resource.query_error = visa_timeout()
    with pytest.raises(TransportError) as raised:
        VisaTransport(resource, 1).query("*IDN?")
    assert raised.value.timed_out


def test_other_visa_errors_become_transport_errors():
    resource = FakeVisaResource("X")
    resource.query_error = pyvisa.VisaIOError(constants.StatusCode.error_io)
    with pytest.raises(TransportError) as raised:
        VisaTransport(resource, 1).query("*IDN?")
    assert not raised.value.timed_out


def test_go_to_local_uses_address_gtl():
    resource = FakeVisaResource("X")
    assert VisaTransport(resource, 1).go_to_local() is True
    assert resource.ren_calls == [constants.RENLineOperation.address_gtl]


def test_go_to_local_reports_unsupported_without_raising():
    resource = FakeVisaResource("X")
    resource.ren_error = pyvisa.VisaIOError(constants.StatusCode.error_nonsupported_operation)
    assert VisaTransport(resource, 1).go_to_local() is False


def test_close_closes_resource():
    resource = FakeVisaResource("X")
    VisaTransport(resource, 1).close()
    assert resource.closed
