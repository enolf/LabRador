import math

import numpy as np
import pytest

from labrador import Bench, Mode, ObserveModeError, Shape


def test_generator_sets_signal_and_observe_scope_reads_it_scaled():
    with Bench.connect(generator=True, mode=Mode.CONTROL, simulated=True) as bench:
        bench.generator.configure(
            1,
            shape=Shape.SINE,
            frequency=1e3,
            amplitude=1.0,
            offset=0.0,
            load=math.inf,
            output=True,
        )
        scope = bench.scope
        scope.mode = Mode.OBSERVE
        before = scope.settings()
        waveform = scope.read_waveform(1)
        assert np.nanmax(waveform.volts) - np.nanmin(waveform.volts) == pytest.approx(1.0, abs=0.03)
        rising = np.flatnonzero((waveform.volts[:-1] < 0) & (waveform.volts[1:] >= 0))
        assert 1 / np.mean(np.diff(waveform.time[rising])) == pytest.approx(1e3, rel=0.02)
        sent_before = len(bench.simulation.scope_device.sent)
        with pytest.raises(ObserveModeError):
            scope.configure_channel(1, scale=1.0)
        assert len(bench.simulation.scope_device.sent) == sent_before
        assert scope.settings() == before
