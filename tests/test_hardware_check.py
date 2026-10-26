import importlib.util
import json
import math
from pathlib import Path

import numpy as np
import pytest

from labrador.instruments.scope import Preamble, Waveform

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "hardware_check.py"


@pytest.fixture
def hardware_check(tmp_path, monkeypatch):
    specification = importlib.util.spec_from_file_location("hardware_check", SCRIPT)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    monkeypatch.setattr(module, "OUTPUT_DIRECTORY", tmp_path)
    return module


def sine_waveform(frequency_hz, cycles, phase_rad=0.0, noise_v=0.0):
    time_s = np.linspace(0, cycles / frequency_hz, 1000, endpoint=False)
    volts = 0.5 * np.sin(2 * math.pi * frequency_hz * time_s + phase_rad)
    volts += np.random.default_rng(1).normal(0, noise_v, time_s.size)
    preamble = Preamble(0, 0, 1000, 1, time_s[1], 0.0, 0, 0.01, 0.0, 128)
    flags = np.zeros(time_s.size, dtype=bool)
    return Waveform(1, time_s, volts, preamble, flags, flags, flags)


@pytest.mark.parametrize("cycles", [1.2, 2, 7])
def test_frequency_estimate_works_with_few_cycles_and_noise(hardware_check, cycles):
    waveform = sine_waveform(1e3, cycles, noise_v=0.02)
    assert hardware_check.estimate_frequency(waveform) == pytest.approx(1e3, rel=0.03)


def test_frequency_estimate_is_none_for_less_than_half_a_cycle(hardware_check):
    assert hardware_check.estimate_frequency(sine_waveform(1e3, 0.3)) is None


def test_fitted_phase(hardware_check):
    lagging = sine_waveform(1e3, 3, phase_rad=-math.pi / 4)
    assert hardware_check.fitted_phase_deg(lagging, 1e3) == pytest.approx(-45, abs=0.1)


def test_simulated_dry_run_passes_and_writes_report(hardware_check, tmp_path, capsys):
    exit_code = hardware_check.main(["--simulated", "--assume-yes"])
    output = capsys.readouterr().out
    assert exit_code == 0, output
    (report_path,) = tmp_path.glob("report-*.json")
    report = json.loads(report_path.read_text(encoding="utf-8"))
    results = {step["step"]: step["result"] for step in report["steps"]}
    assert results["Observe mode refuses a setting command"] == "PASS"
    assert results["Front-panel settings unchanged by observe mode"] == "PASS"
    assert results["Control-mode steps 5-7"] == "SKIP"
    assert "generator_metadata_default" in report["recommendations"]
    assert (tmp_path / "screen.png").exists()


def test_simulated_dry_run_with_control_steps(hardware_check, tmp_path, capsys):
    exit_code = hardware_check.main(["--simulated", "--assume-yes", "--control"])
    output = capsys.readouterr().out
    assert "[PASS] Generator settings read back" in output
    assert "[PASS] Scope measures 1.0 Vpp from the generator" in output
    assert "[PASS] Scope measures 1 kHz from the generator" in output
    assert exit_code == 0, output
