import math

import pytest

from labrador.errors import InstrumentCommandError, ObserveModeError, UnexpectedReplyError
from labrador.instruments.base import Mode
from labrador.instruments.generator import Generator, Shape, read_generator_settings
from labrador.instruments.simulated import SimulatedGeneratorDevice


@pytest.fixture
def device():
    return SimulatedGeneratorDevice()


@pytest.fixture
def generator(device):
    return Generator(device, mode=Mode.CONTROL)


def commands_after(device, start):
    return [message for message in device.sent[start:] if message != "SYSTem:ERRor?"]


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (lambda g: g.set_shape(1, Shape.SQUARE), "SOURce1:FUNCtion:SHAPe SQUare"),
        (lambda g: g.set_frequency(1, 1e3), "SOURce1:FREQuency:FIXed 1000"),
        (lambda g: g.set_frequency(2, 12.5e6), "SOURce2:FREQuency:FIXed 12500000"),
        (lambda g: g.set_frequency(1, 0.001), "SOURce1:FREQuency:FIXed 0.001"),
        (
            lambda g: g.set_amplitude(2, 0.5),
            "SOURce2:VOLTage:LEVel:IMMediate:AMPLitude 0.5VPP",
        ),
        (lambda g: g.set_offset(1, -0.25), "SOURce1:VOLTage:LEVel:IMMediate:OFFSet -0.25"),
        (lambda g: g.set_load(1, math.inf), "OUTPut1:IMPedance INFinity"),
        (lambda g: g.set_load(2, 50), "OUTPut2:IMPedance 50"),
        (lambda g: g.set_output(1, True), "OUTPut1:STATe ON"),
        (lambda g: g.set_output(2, False), "OUTPut2:STATe OFF"),
        (lambda g: g.align_phase(), "SOURce1:PHASe:INITiate"),
    ],
)
def test_setters_send_verified_commands_then_check_errors(device, generator, action, expected):
    start = len(device.sent)
    action(generator)
    assert device.sent[start:] == [expected, "SYSTem:ERRor?"]


def test_values_read_back(generator):
    generator.set_shape(2, Shape.RAMP)
    generator.set_frequency(2, 2.5e3)
    generator.set_amplitude(2, 0.75)
    generator.set_offset(2, 0.1)
    generator.set_load(2, 1000)
    generator.set_output(2, True)
    assert generator.shape(2) is Shape.RAMP
    assert generator.frequency(2) == pytest.approx(2.5e3)
    assert generator.amplitude(2) == pytest.approx(0.75)
    assert generator.offset(2) == pytest.approx(0.1)
    assert generator.load(2) == pytest.approx(1000)
    assert generator.output(2) is True


def test_finite_load_reply_with_ohm_sign_is_parsed(device, generator):
    generator.set_load(1, 50)
    assert generator.query("OUTPut1:IMPedance?") == "50Ω"
    assert generator.load(1) == 50.0


def test_load_reply_without_a_number_is_unexpected(device, generator):
    device.add_handler("OUTPut#:IMPedance?", lambda command, suffixes: "HighZ")
    with pytest.raises(UnexpectedReplyError, match="HighZ"):
        generator.load(1)


def test_infinite_load_round_trips(generator):
    generator.set_load(1, math.inf)
    assert generator.load(1) == math.inf


def test_configure_orders_load_before_amplitude_and_output_last(device, generator):
    start = len(device.sent)
    generator.configure(
        1, shape=Shape.SINE, frequency=1e3, amplitude=1.0, offset=0.0, load=math.inf, output=True
    )
    assert commands_after(device, start) == [
        "OUTPut1:IMPedance INFinity",
        "SOURce1:FUNCtion:SHAPe SINusoid",
        "SOURce1:FREQuency:FIXed 1000",
        "SOURce1:VOLTage:LEVel:IMMediate:AMPLitude 1VPP",
        "SOURce1:VOLTage:LEVel:IMMediate:OFFSet 0",
        "OUTPut1:STATe ON",
    ]


def test_configure_switches_output_off_first(device, generator):
    start = len(device.sent)
    generator.configure(1, frequency=5e3, output=False)
    assert commands_after(device, start) == ["OUTPut1:STATe OFF", "SOURce1:FREQuency:FIXed 5000"]


@pytest.mark.parametrize(
    "action",
    [
        lambda g: g.set_frequency(3, 1e3),
        lambda g: g.set_frequency(1, 0),
        lambda g: g.set_amplitude(1, -1),
        lambda g: g.set_load(1, 0.5),
        lambda g: g.set_load(1, 20_000),
        lambda g: g.set_load(1, -math.inf),
    ],
)
def test_invalid_arguments_are_rejected_before_sending(device, generator, action):
    start = len(device.sent)
    with pytest.raises(ValueError):
        action(generator)
    assert device.sent[start:] == []


def test_instrument_error_after_write_is_raised(generator):
    with pytest.raises(InstrumentCommandError) as raised:
        generator.write("SOURce1:FREQuency:FIXed banana")
    assert raised.value.code == -224


def test_observe_mode_refuses_every_setter_without_sending(device):
    generator = Generator(device)
    start = len(device.sent)
    for action in (
        lambda: generator.set_shape(1, Shape.SINE),
        lambda: generator.set_frequency(1, 1e3),
        lambda: generator.set_amplitude(1, 1),
        lambda: generator.set_offset(1, 0),
        lambda: generator.set_load(1, math.inf),
        lambda: generator.set_output(1, True),
        generator.align_phase,
        lambda: generator.configure(1, output=True),
    ):
        with pytest.raises(ObserveModeError):
            action()
    assert device.sent[start:] == []


def test_observe_mode_reads_settings_with_allowed_queries_only(device):
    generator = Generator(device)
    settings = generator.settings()
    assert set(settings.channels) == {1, 2}
    assert settings.channels[1].shape == "SIN"
    assert settings.channels[1].load_ohms == 50.0
    for message in device.sent:
        assert generator.policy.refusal(message, observe=True) is None


def test_settings_as_dict_is_labelled_by_channel(generator):
    generator.set_load(1, math.inf)
    as_dict = generator.settings().as_dict()
    assert as_dict["instrument"].startswith("TEKTRONIX,AFG1062")
    assert as_dict["ch1"]["load_ohms"] == math.inf
    assert set(as_dict["ch2"]) == {
        "shape",
        "frequency_hz",
        "amplitude_vpp",
        "offset_v",
        "load_ohms",
        "output_on",
    }


def test_shape_reply_for_built_in_waveform_is_kept_as_text():
    assert Shape.from_reply("StairUp") == "StairUp"
    assert Shape.from_reply("PRN") is Shape.NOISE


def test_open_simulated():
    generator = Generator.open(simulated=True)
    assert generator.identity.model == "AFG1062"
    assert generator.mode is Mode.OBSERVE


def test_read_generator_settings_queries_then_releases(device):
    settings = read_generator_settings(transport=device)
    assert settings.channels[2].frequency_hz == pytest.approx(1000.0)
    assert device.go_to_local_calls == 1
    assert device.closed
    assert device.remote is False
    for message in device.sent:
        assert Generator.policy.refusal(message, observe=True) is None


def test_read_generator_settings_can_be_repeated_on_a_reconnected_device(device):
    read_generator_settings(transport=device)
    read_generator_settings(transport=device.connect())
    assert device.go_to_local_calls == 2


def test_read_generator_settings_simulated():
    assert read_generator_settings(simulated=True).identity.model == "AFG1062"


def test_simulated_output_follows_load_setting(device, generator):
    generator.configure(1, amplitude=1.0, load=50, output=True)
    assert device.channels[1].open_circuit_amplitude_vpp() == pytest.approx(2.0)
    generator.set_load(1, math.inf)
    assert device.channels[1].open_circuit_amplitude_vpp() == pytest.approx(1.0)
    generator.set_output(1, False)
    assert device.channels[1].open_circuit_amplitude_vpp() == 0.0
