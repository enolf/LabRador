import math

import numpy as np
import pytest

from labrador.instruments.base import Mode
from labrador.instruments.bench import Bench
from labrador.instruments.simulated import through, transmission_line

LENGTH_DELAY_S = 10e-9
QUARTER_WAVE_HZ = 1 / (4 * LENGTH_DELAY_S)


def peak_to_peak(waveform):
    return float(np.nanmax(waveform.volts) - np.nanmin(waveform.volts))


def test_through_path_has_unit_gain():
    assert through()(1e6) == 1


def test_matched_cable_is_flat_and_only_delays():
    cable = transmission_line(50, LENGTH_DELAY_S, input_capacitance_f=0.0)
    for frequency in (1e3, 1e6, QUARTER_WAVE_HZ, 40e6):
        assert abs(cable(frequency)) == pytest.approx(1.0, rel=1e-3)
    assert np.angle(cable(QUARTER_WAVE_HZ)) == pytest.approx(-math.pi / 2, abs=1e-3)


def test_mismatched_cable_peaks_at_quarter_wave_by_z0_over_zs():
    cable = transmission_line(75, LENGTH_DELAY_S, input_capacitance_f=0.0)
    assert abs(cable(1e3)) == pytest.approx(1.0, rel=1e-3)
    assert abs(cable(QUARTER_WAVE_HZ)) == pytest.approx(75 / 50, rel=1e-3)


def test_generator_settings_appear_at_the_scope():
    bench = Bench.connect(generator=True, mode=Mode.CONTROL, simulated=True)
    bench.generator.configure(
        1, frequency=5e3, amplitude=0.8, offset=0.1, load=math.inf, output=True
    )
    bench.scope.configure_channel(1, scale=0.2, offset=0.1)
    waveform = bench.scope.read_waveform(1)
    assert peak_to_peak(waveform) == pytest.approx(0.8, abs=0.03)
    assert np.nanmean(waveform.volts) == pytest.approx(0.1, abs=0.02)
    rising = np.flatnonzero((waveform.volts[:-1] < 0.1) & (waveform.volts[1:] >= 0.1))
    period = np.mean(np.diff(waveform.time[rising]))
    assert 1 / period == pytest.approx(5e3, rel=0.02)


def test_fifty_ohm_load_setting_doubles_the_voltage_at_a_high_impedance_input():
    bench = Bench.connect(generator=True, mode=Mode.CONTROL, simulated=True)
    bench.generator.configure(1, amplitude=0.5, load=50, output=True)
    bench.scope.configure_channel(1, scale=0.2)
    assert peak_to_peak(bench.scope.read_waveform(1)) == pytest.approx(1.0, abs=0.03)


def test_cable_ratio_at_the_scope_matches_the_model():
    bench = Bench.connect(
        generator=True,
        mode=Mode.CONTROL,
        simulated=True,
        signal_paths={2: transmission_line(75, LENGTH_DELAY_S, input_capacitance_f=0.0)},
    )
    for channel in (1, 2):
        bench.generator.configure(
            channel, frequency=QUARTER_WAVE_HZ, amplitude=1.0, load=math.inf, output=True
        )
        bench.scope.configure_channel(channel, scale=0.5)
    bench.scope.configure_timebase(scale=20e-9)
    ratio = peak_to_peak(bench.scope.read_waveform(2)) / peak_to_peak(bench.scope.read_waveform(1))
    assert ratio == pytest.approx(1.5, rel=0.03)
