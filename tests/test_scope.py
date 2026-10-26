import math

import numpy as np
import pytest

from labrador.errors import (
    ChannelNotDisplayedError,
    ObserveModeError,
    SettingsChangedError,
    WaveformUnavailableError,
)
from labrador.instruments.base import Mode
from labrador.instruments.scope import (
    AcquireType,
    Coupling,
    Measurement,
    PointsMode,
    Preamble,
    Scope,
    Slope,
    Sweep,
    TriggerSource,
    WaveformFormat,
    decode_waveform,
)
from labrador.instruments.simulated import SimulatedScopeDevice

ERROR_QUERY = ":SYSTem:ERRor?"


@pytest.fixture
def device():
    return SimulatedScopeDevice(noise_v=0.0)


@pytest.fixture
def scope(device):
    return Scope(device)


@pytest.fixture
def control_scope(device):
    return Scope(device, mode=Mode.CONTROL)


def sent_since(device, start):
    return [message for message in device.sent[start:] if message != ERROR_QUERY]


def test_identity(scope):
    assert scope.identity.manufacturer == "KEYSIGHT TECHNOLOGIES"
    assert scope.identity.model == "EDUX1052A"


def test_byte_waveform_is_scaled_within_one_step(scope):
    waveform = scope.read_waveform(1)
    step = waveform.preamble.y_increment
    assert waveform.volts.size == 1000
    assert np.nanmax(waveform.volts) == pytest.approx(0.5, abs=step)
    assert np.nanmin(waveform.volts) == pytest.approx(-0.5, abs=step)
    assert not waveform.is_clipped
    assert not waveform.has_holes


def test_time_axis_from_preamble(scope):
    waveform = scope.read_waveform(1)
    assert waveform.time[0] == pytest.approx(-1e-3)
    assert np.diff(waveform.time) == pytest.approx(np.full(999, 2e-6))
    at_trigger = int(np.argmin(np.abs(waveform.time)))
    assert waveform.volts[at_trigger] == pytest.approx(0.0, abs=2 * waveform.preamble.y_increment)
    assert waveform.volts[at_trigger + 10] > waveform.volts[at_trigger - 10]


def test_word_waveform_requests_lsb_first_and_is_finer(device, scope):
    start = len(device.sent)
    waveform = scope.read_waveform(2, data_format=WaveformFormat.WORD, points=500)
    assert ":WAVeform:BYTeorder LSBFirst" in device.sent[start:]
    assert waveform.preamble.format == 1
    assert waveform.volts.size == 500
    expected = 0.25 * np.sin(2 * math.pi * 1e3 * waveform.time - math.pi / 4)
    assert np.max(np.abs(waveform.volts - expected)) < 0.002


def test_normal_read_sends_transfer_settings_then_preamble_data_preamble(device, scope):
    start = len(device.sent)
    scope.read_waveform(1, points=250)
    assert sent_since(device, start) == [
        ":CHANnel1:DISPlay?",
        ":WAVeform:SOURce CHANnel1",
        ":WAVeform:FORMat BYTE",
        ":WAVeform:UNSigned ON",
        ":WAVeform:POINts:MODE NORMal",
        ":WAVeform:POINts 250",
        ":WAVeform:PREamble?",
        ":WAVeform:DATA?",
        ":WAVeform:PREamble?",
    ]


@pytest.mark.parametrize(
    ("points", "mode"), [(2000, PointsMode.NORMAL), (3000, PointsMode.RAW), (0, PointsMode.NORMAL)]
)
def test_invalid_point_counts_are_rejected(scope, points, mode):
    with pytest.raises(ValueError):
        scope.read_waveform(1, points=points, points_mode=mode)


def test_invalid_channel_is_rejected(scope):
    with pytest.raises(ValueError):
        scope.read_waveform(3)


def test_channel_switched_off_cannot_be_read(device, scope):
    device.set_channel_displayed(2, False)
    start = len(device.sent)
    with pytest.raises(ChannelNotDisplayedError):
        scope.read_waveform(2)
    assert device.sent[start:] == [":CHANnel2:DISPlay?"]
    assert scope.displayed_channels() == (1,)
    assert set(scope.read_waveforms()) == {1}


def test_clipping_is_flagged(device, control_scope):
    control_scope.configure_channel(1, scale=0.05)
    waveform = control_scope.read_waveform(1)
    assert waveform.is_clipped
    assert waveform.clipped_high.any()
    assert waveform.clipped_low.any()


def test_holes_become_nan_when_nothing_was_acquired(device, control_scope):
    control_scope.configure_trigger(level=10.0, sweep=Sweep.NORMAL)
    waveform = control_scope.read_waveform(1)
    assert waveform.holes.all()
    assert np.isnan(waveform.volts).all()


def test_raw_needs_the_scope_stopped(device, scope):
    start = len(device.sent)
    with pytest.raises(WaveformUnavailableError, match="Stop"):
        scope.read_waveform(1, points_mode=PointsMode.RAW)
    assert not any(message.startswith(":WAVeform") for message in device.sent[start:])


def test_raw_needs_normal_or_high_resolution_acquisition(device, control_scope):
    control_scope.configure_acquisition(type=AcquireType.AVERAGE, count=16)
    device.press_stop()
    with pytest.raises(WaveformUnavailableError, match="Normal or High Resolution"):
        control_scope.read_waveform(1, points_mode=PointsMode.RAW)


def test_raw_full_memory_when_stopped(device, scope):
    scope.read_waveform(1)
    device.press_stop()
    start = len(device.sent)
    waveform = scope.read_waveform(1, points_mode=PointsMode.RAW)
    assert waveform.volts.size == 20_000
    assert ":WAVeform:POINts MAXimum" in device.sent[start:]
    assert sent_since(device, start).index(":WAVeform:POINts MAXimum") < sent_since(
        device, start
    ).index(":WAVeform:POINts:MODE RAW")


def test_raw_with_explicit_points(device, scope):
    device.press_stop()
    scope.read_waveform(1)
    assert scope.read_waveform(1, points=5000, points_mode=PointsMode.RAW).volts.size == 5000


def test_maximum_while_running_falls_back_to_screen_record(scope):
    assert scope.read_waveform(1, points_mode=PointsMode.MAXIMUM).volts.size == 1000


def test_knob_turned_once_during_read_is_retried(device, scope):
    device.turn_knob_during_next_reads(1)
    waveform = scope.read_waveform(1)
    assert waveform.preamble.y_increment == pytest.approx(0.4 / 25)
    assert device.sent.count(":WAVeform:DATA?") == 2


def test_knob_turning_throughout_raises_settings_changed(device, scope):
    device.turn_knob_during_next_reads(10)
    with pytest.raises(SettingsChangedError):
        scope.read_waveform(1, retries=3)
    assert device.sent.count(":WAVeform:DATA?") == 3


def test_preamble_x_origin_jitter_within_one_sample_is_the_same_scaling():
    base = Preamble.parse("+0,+0,+1000,+1,+2.0E-06,-1.0E-03,+0,+8.0E-03,+0.0E+00,+128")
    jittered = Preamble.parse("+0,+0,+1000,+1,+2.0E-06,-1.0000015E-03,+0,+8.0E-03,+0.0E+00,+128")
    moved = Preamble.parse("+0,+0,+1000,+1,+2.0E-06,-0.9E-03,+0,+8.0E-03,+0.0E+00,+128")
    rescaled = Preamble.parse("+0,+0,+1000,+1,+2.0E-06,-1.0E-03,+0,+1.6E-02,+0.0E+00,+128")
    assert base.same_scaling_as(jittered)
    assert not base.same_scaling_as(moved)
    assert not base.same_scaling_as(rescaled)


def test_preamble_with_wrong_field_count_is_rejected():
    with pytest.raises(ValueError):
        Preamble.parse("+0,+0,+1000")


def test_decode_word_special_codes_and_peak_pairs():
    preamble = Preamble(1, 1, 2, 1, 1e-3, 0.0, 0, 1.0 / 256, 0.0, 32768)
    block = np.array([0, 1, 32768, 0xFFFF], dtype="<u2").tobytes()
    waveform = decode_waveform(1, block, preamble)
    assert waveform.holes.tolist() == [True, False, False, False]
    assert waveform.clipped_low.tolist() == [False, True, False, False]
    assert waveform.clipped_high.tolist() == [False, False, False, True]
    assert waveform.volts[2] == 0.0
    assert waveform.time.tolist() == [0.0, 0.0, 2e-3, 2e-3]


def test_run_state_and_trigger_event(device, scope):
    assert scope.run_state().running
    scope.read_waveform(1)
    assert scope.trigger_occurred() is True
    assert scope.trigger_occurred() is False
    device.press_stop()
    assert scope.run_state().raw == 32
    assert scope.run_state().running is False
    device.press_single()
    state = scope.run_state()
    assert state.running
    assert state.raw == 8 | 32
    device.deliver_trigger()
    assert scope.run_state().running is False
    assert scope.trigger_occurred() is True


def test_screenshot_is_png(scope):
    assert scope.screenshot().startswith(b"\x89PNG\r\n\x1a\n")


def test_settings_snapshot(scope):
    settings = scope.settings()
    assert settings.channels[1].scale_v_per_div == pytest.approx(0.2)
    assert settings.channels[2].coupling == "DC"
    assert settings.time_per_div_s == pytest.approx(2e-4)
    assert settings.trigger_mode == "EDGE"
    assert settings.trigger_source == "CHAN1"
    assert settings.trigger_level_v == pytest.approx(0.0)
    assert settings.acquire_type == "NORM"
    as_dict = settings.as_dict()
    assert as_dict["instrument"].startswith("KEYSIGHT")
    assert as_dict["ch1"]["probe"] == 1.0
    assert as_dict["trigger_sweep"] == "AUTO"


def test_trigger_level_not_queried_for_line_trigger(device, control_scope):
    control_scope.configure_trigger(source=TriggerSource.LINE)
    start = len(device.sent)
    assert control_scope.settings().trigger_level_v is None
    assert not any("LEV" in message for message in device.sent[start:])


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        (
            lambda s: s.configure_channel(
                1, probe=10, display=True, scale=0.5, offset=0.1, coupling=Coupling.AC
            ),
            [
                ":CHANnel1:PROBe 10",
                ":CHANnel1:DISPlay 1",
                ":CHANnel1:SCALe 0.5",
                ":CHANnel1:OFFSet 0.1",
                ":CHANnel1:COUPling AC",
            ],
        ),
        (
            lambda s: s.configure_timebase(scale=1e-6, position=-2e-6),
            [":TIMebase:SCALe 1E-06", ":TIMebase:POSition -2E-06"],
        ),
        (
            lambda s: s.configure_trigger(
                source=TriggerSource.CHANNEL2, level=0.25, slope=Slope.NEGATIVE, sweep=Sweep.NORMAL
            ),
            [
                ":TRIGger:MODE EDGE",
                ":TRIGger:EDGE:SOURce CHANnel2",
                ":TRIGger:EDGE:LEVel 0.25",
                ":TRIGger:EDGE:SLOPe NEGative",
                ":TRIGger:SWEep NORMal",
            ],
        ),
        (lambda s: s.configure_trigger(sweep=Sweep.AUTO), [":TRIGger:SWEep AUTO"]),
        (
            lambda s: s.configure_acquisition(type=AcquireType.AVERAGE, count=16),
            [":ACQuire:TYPE AVERage", ":ACQuire:COUNt 16"],
        ),
        (lambda s: s.run(), [":RUN"]),
        (lambda s: s.stop(), [":STOP"]),
        (lambda s: s.single(), [":SINGle"]),
        (lambda s: s.digitize([1, 2]), [":DIGitize CHANnel1,CHANnel2", "*OPC?"]),
        (lambda s: s.digitize(), [":DIGitize", "*OPC?"]),
        (lambda s: s.reset(), ["*RST", "*OPC?"]),
        (lambda s: s.preset(), [":SYSTem:PRESet", "*OPC?"]),
        (lambda s: s.autoscale(), [":AUToscale", "*OPC?"]),
    ],
)
def test_control_commands_are_verified_strings(device, control_scope, action, expected):
    start = len(device.sent)
    action(control_scope)
    assert sent_since(device, start) == expected
    assert device.error_queue == []


def test_digitize_acquires_and_stops(device, control_scope):
    control_scope.digitize([1])
    assert control_scope.run_state().running is False


def test_average_count_is_validated(control_scope):
    with pytest.raises(ValueError):
        control_scope.configure_acquisition(count=1)


def test_measurements(control_scope):
    control_scope.configure_timebase(scale=5e-4)
    assert control_scope.measure(Measurement.FREQUENCY, 1) == pytest.approx(1000, rel=1e-3)
    assert control_scope.measure(Measurement.VPP, 1) == pytest.approx(1.0, rel=1e-3)
    assert control_scope.measure(Measurement.PHASE, 1, 2) == pytest.approx(45, abs=0.5)
    assert control_scope.measure(Measurement.DELAY, 1, 2) == pytest.approx(1.25e-4, rel=1e-2)


def test_measurement_sends_two_sources(device, control_scope):
    start = len(device.sent)
    control_scope.measure(Measurement.PHASE, 1, 2)
    assert device.sent[start:] == [":MEASure:PHASe? CHANnel1,CHANnel2"]


def test_invalid_measurement_is_none(control_scope):
    control_scope.configure_timebase(scale=1e-8)
    assert control_scope.measure(Measurement.FREQUENCY, 1) is None


def test_measurement_source_count_is_checked(control_scope):
    with pytest.raises(ValueError):
        control_scope.measure(Measurement.PHASE, 1)
    with pytest.raises(ValueError):
        control_scope.measure(Measurement.VPP, 1, 2)


OBSERVE_REFUSED = [
    lambda s: s.configure_channel(1, scale=1),
    lambda s: s.configure_timebase(scale=1e-3),
    lambda s: s.configure_trigger(level=0),
    lambda s: s.configure_acquisition(type=AcquireType.AVERAGE),
    lambda s: s.run(),
    lambda s: s.stop(),
    lambda s: s.single(),
    lambda s: s.digitize(),
    lambda s: s.reset(),
    lambda s: s.preset(),
    lambda s: s.autoscale(),
    lambda s: s.measure(Measurement.FREQUENCY, 1),
    lambda s: s.write(":SYSTem:LOCK ON"),
]


@pytest.mark.parametrize("action", OBSERVE_REFUSED)
def test_observe_mode_refuses_control_actions_without_sending(device, scope, action):
    start = len(device.sent)
    with pytest.raises(ObserveModeError):
        action(scope)
    assert device.sent[start:] == []
    assert device.installed_measurements == []
    assert device.locked is False


def test_open_simulated():
    scope = Scope.open(simulated=True)
    assert scope.mode is Mode.OBSERVE
    assert scope.read_waveform(1).volts.size == 1000
