import argparse
import json
import math
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import numpy as np
import pyvisa

from labrador.errors import (
    InstrumentNotFoundError,
    InstrumentNotRespondingError,
    LabradorError,
    ObserveModeError,
    SettingsChangedError,
    VisaNotFoundError,
)
from labrador.instruments import (
    Generator,
    Measurement,
    Mode,
    PointsMode,
    Scope,
    Shape,
    Waveform,
    WaveformFormat,
    read_generator_settings,
)
from labrador.instruments.base import list_resources, open_resource_manager
from labrador.instruments.simulated import (
    SimulatedGeneratorDevice,
    SimulatedScopeDevice,
    generator_signal,
)

OUTPUT_DIRECTORY = Path("data") / "hardware_check"
FRAME_RATE_READS = 20
KNOB_TURNING_SECONDS = 10.0
KNOBS_STILL_SECONDS = 5.0
RUN_STATE_SAMPLES = 20
RUN_STATE_INTERVAL_S = 0.1
AMPLITUDE_TOLERANCE = 0.05
FREQUENCY_TOLERANCE = 0.02
HYSTERESIS_FRACTION = 0.1


@dataclass
class Report:
    assume_yes: bool
    steps: list[dict[str, object]] = field(default_factory=list)
    answers: dict[str, bool] = field(default_factory=dict)
    recommendations: dict[str, str] = field(default_factory=dict)

    def check(self, name: str, passed: bool, detail: object = "") -> bool:
        self._record(name, "PASS" if passed else "FAIL", detail)
        return passed

    def info(self, name: str, detail: object = "") -> None:
        self._record(name, "INFO", detail)

    def skip(self, name: str, reason: str) -> None:
        self._record(name, "SKIP", reason)

    def ask(self, key: str, question: str) -> bool:
        if self.assume_yes:
            print(f"  ? {question} [assumed yes]")
            answer = True
        else:
            answer = input(f"  ? {question} [y/n] ").strip().lower().startswith("y")
        self.answers[key] = answer
        return answer

    def prompt(self, instruction: str) -> None:
        if self.assume_yes:
            print(f"  > {instruction} [skipped]")
        else:
            input(f"  > {instruction} Press Enter when done. ")

    def failed(self) -> bool:
        return any(step["result"] == "FAIL" for step in self.steps)

    def save(self, directory: Path) -> Path:
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f"report-{datetime.now():%Y%m%d-%H%M%S}.json"
        content = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "steps": self.steps,
            "answers": self.answers,
            "recommendations": self.recommendations,
        }
        path.write_text(json.dumps(content, indent=2, default=str), encoding="utf-8")
        return path

    def _record(self, name: str, result: str, detail: object) -> None:
        self.steps.append({"step": name, "result": result, "detail": detail})
        print(f"[{result}] {name}" + (f": {detail}" if detail != "" else ""))


class Connections:
    def __init__(self, simulated: bool) -> None:
        self.simulated = simulated
        self.resource_manager: pyvisa.ResourceManager | None = None
        self._generator_device = SimulatedGeneratorDevice()
        self._scope_device = SimulatedScopeDevice(generator_signal(self._generator_device))

    def open_scope(self, mode: Mode) -> Scope:
        if self.simulated:
            return Scope(self._scope_device.connect(), mode=mode, address="SIMULATED")
        return Scope.open(mode=mode, resource_manager=self.resource_manager)

    def open_generator(self, mode: Mode) -> Generator:
        if self.simulated:
            return Generator(self._generator_device.connect(), mode=mode, address="SIMULATED")
        return Generator.open(mode=mode, resource_manager=self.resource_manager)

    def read_generator_settings(self) -> object:
        if self.simulated:
            return read_generator_settings(transport=self._generator_device.connect())
        return read_generator_settings(resource_manager=self.resource_manager)


def peak_to_peak(waveform: Waveform) -> float:
    return float(np.nanmax(waveform.volts) - np.nanmin(waveform.volts))


def estimate_frequency(waveform: Waveform) -> float | None:
    volts = waveform.volts
    valid = ~np.isnan(volts)
    if valid.sum() < 4:
        return None
    middle = (np.nanmax(volts) + np.nanmin(volts)) / 2
    margin = HYSTERESIS_FRACTION * peak_to_peak(waveform)
    crossing_times = []
    high: bool | None = None
    for time_s, value in zip(waveform.time[valid], volts[valid], strict=True):
        if value > middle + margin and high is not True:
            if high is False:
                crossing_times.append(time_s)
            high = True
        elif value < middle - margin and high is not False:
            if high is True:
                crossing_times.append(time_s)
            high = False
    if len(crossing_times) < 2:
        return None
    return float(1.0 / (2.0 * np.mean(np.diff(crossing_times))))


def fitted_phase_deg(waveform: Waveform, frequency_hz: float) -> float:
    valid = ~np.isnan(waveform.volts)
    angle = 2 * math.pi * frequency_hz * waveform.time[valid]
    basis = np.column_stack([np.sin(angle), np.cos(angle), np.ones(angle.size)])
    (sine, cosine, _), *_ = np.linalg.lstsq(basis, waveform.volts[valid], rcond=None)
    return math.degrees(math.atan2(cosine, sine))


def describe(waveform: Waveform) -> dict[str, object]:
    frequency = estimate_frequency(waveform)
    return {
        "points": int(waveform.volts.size),
        "vpp_v": round(peak_to_peak(waveform), 4),
        "mean_v": round(float(np.nanmean(waveform.volts)), 4),
        "frequency_hz": None if frequency is None else round(frequency, 3),
        "clipped": waveform.is_clipped,
        "holes": int(waveform.holes.sum()),
    }


def check_visa(report: Report, connections: Connections) -> bool:
    print("\n== 1. VISA library and resources")
    if connections.simulated:
        report.info("VISA", "simulated instruments, no VISA used")
        return True
    try:
        manager = open_resource_manager()
    except VisaNotFoundError as error:
        report.check("VISA library found", False, str(error))
        return False
    connections.resource_manager = manager
    report.check("VISA library found", True, str(manager.visalib))
    report.info("Resources", list(list_resources(manager)))
    return True


def check_scope_observe(report: Report, connections: Connections) -> Scope | None:
    print("\n== 2. Oscilloscope in observe mode")
    try:
        scope = connections.open_scope(Mode.OBSERVE)
    except (InstrumentNotFoundError, InstrumentNotRespondingError) as error:
        report.check("Oscilloscope connected", False, str(error))
        return None
    report.check("Oscilloscope connected", True, scope.identity.raw)
    displayed = scope.displayed_channels()
    report.info("Displayed channels", displayed)
    if not displayed:
        report.check("At least one channel switched on", False, "switch on CH1 and rerun")
        return scope
    before = scope.settings()
    report.info("Front-panel settings", before.as_dict())
    first = displayed[0]
    report.info(f"CH{first} preamble", scope.read_preamble(first))
    for channel in displayed:
        for data_format in (WaveformFormat.BYTE, WaveformFormat.WORD):
            waveform = scope.read_waveform(channel, data_format=data_format)
            report.info(f"CH{channel} {data_format.value}", describe(waveform))
    report.ask(
        "python_values_match_screen",
        "Do the Vpp, mean and frequency above agree with the scope's own display?",
    )
    started = time.perf_counter()
    for _ in range(FRAME_RATE_READS):
        scope.read_waveform(first)
    elapsed = time.perf_counter() - started
    report.info("Frame rate, 1000 points BYTE", f"{FRAME_RATE_READS / elapsed:.1f} frames/s")
    screenshot = scope.screenshot()
    OUTPUT_DIRECTORY.mkdir(parents=True, exist_ok=True)
    screenshot_path = OUTPUT_DIRECTORY / "screen.png"
    screenshot_path.write_bytes(screenshot)
    report.check("Screenshot is a PNG", screenshot.startswith(b"\x89PNG"), str(screenshot_path))
    try:
        scope.configure_channel(first, scale=1.0)
        report.check("Observe mode refuses a setting command", False, "the command was sent")
    except ObserveModeError as error:
        report.check("Observe mode refuses a setting command", True, str(error))
    report.check(
        "Front-panel settings unchanged by observe mode",
        scope.settings() == before,
        "compared before and after all observe-mode reads",
    )
    return scope


def sample_run_state(scope: Scope, interval_s: float) -> list[dict[str, object]]:
    samples = []
    for _ in range(RUN_STATE_SAMPLES):
        state = scope.run_state()
        samples.append({"raw": state.raw, "running": state.running, "armed": state.armed})
        time.sleep(interval_s)
    samples.append({"trigger_occurred": scope.trigger_occurred()})
    return samples


def check_scope_front_panel(report: Report, scope: Scope, simulated: bool) -> None:
    print("\n== 3. Oscilloscope front panel while connected")
    interval_s = 0.0 if simulated else RUN_STATE_INTERVAL_S
    for key, instruction in (
        ("run", "Press Run on the oscilloscope."),
        ("stop", "Press Stop on the oscilloscope."),
        (
            "single_waiting",
            "Set the trigger Mode to Normal and the trigger level above the signal, "
            "then press Single (it should wait).",
        ),
        ("single_triggered", "Move the trigger level back into the signal so Single completes."),
    ):
        report.prompt(instruction)
        report.info(f"Run state after: {key}", sample_run_state(scope, interval_s))
    report.prompt("Restore your trigger settings and press Run.")
    first = (scope.displayed_channels() or (1,))[0]
    report.prompt(
        f"For the next {KNOB_TURNING_SECONDS:.0f} s, turn the CH{first} V/div knob a few times."
    )
    turning = count_discarded_frames(scope, first, 0.0 if simulated else KNOB_TURNING_SECONDS)
    report.info("Frames while turning the knob", turning)
    report.prompt(f"Leave the knobs alone for {KNOBS_STILL_SECONDS:.0f} s.")
    still = count_discarded_frames(scope, first, 0.0 if simulated else KNOBS_STILL_SECONDS)
    report.check(
        "No frames discarded with the knobs still",
        still["discarded"] == 0,
        still,
    )


def count_discarded_frames(scope: Scope, channel: int, seconds: float) -> dict[str, int]:
    counts = {"read": 0, "discarded": 0}
    deadline = time.perf_counter() + seconds
    while time.perf_counter() < deadline or counts["read"] + counts["discarded"] == 0:
        try:
            scope.read_waveform(channel, retries=1)
            counts["read"] += 1
        except SettingsChangedError:
            counts["discarded"] += 1
    return counts


def check_generator_remote_mode(report: Report, connections: Connections) -> None:
    print("\n== 4. Function generator remote-mode test (read-only)")
    try:
        generator = connections.open_generator(Mode.OBSERVE)
    except (InstrumentNotFoundError, InstrumentNotRespondingError) as error:
        report.skip("Generator remote-mode test", str(error))
        return
    report.info("Generator identity", generator.identity.raw)
    report.info("CH1 frequency query", generator.frequency(1))
    after_query = report.ask(
        "afg_panel_after_query",
        "Press a button or turn the knob on the AFG now. Does it respond immediately?",
    )
    report.info("Go-to-local supported", generator.go_to_local())
    after_local = report.ask(
        "afg_panel_after_go_to_local", "Try the AFG front panel again. Does it respond now?"
    )
    generator.close()
    settings = connections.read_generator_settings()
    report.info("read_generator_settings()", settings)
    after_brief_read = report.ask(
        "afg_panel_after_brief_read",
        "Try the AFG front panel once more. Does it respond after the brief settings read?",
    )
    if after_query:
        recommendation = "default on: queries do not lock the front panel"
    elif after_local or after_brief_read:
        recommendation = "default on: go-to-local releases the front panel"
    else:
        recommendation = "keep opt-in: the front panel stays locked after queries"
    report.recommendations["generator_metadata_default"] = recommendation
    report.info("Recommendation for --gen-metadata", recommendation)


def restore_generator(generator: Generator, snapshot: object) -> list[str]:
    notes = []
    for channel, values in snapshot.channels.items():
        shape = Shape.from_reply(values.shape)
        if not isinstance(shape, Shape):
            notes.append(f"CH{channel} shape {values.shape!r} not restored; set it by hand")
            shape = None
        generator.configure(
            channel,
            shape=shape,
            frequency=values.frequency_hz,
            amplitude=values.amplitude_vpp,
            offset=values.offset_v,
            load=values.load_ohms,
            output=values.output_on,
        )
    return notes


def check_generator_and_scope_control(
    report: Report, connections: Connections, scope: Scope | None
) -> None:
    print("\n== 5-7. Function generator and oscilloscope in control mode")
    report.prompt(
        "These steps switch generator CH1 on at 1 kHz, 1 Vpp. Disconnect anything that must "
        "not see this signal. The generator settings are restored at the end."
    )
    try:
        generator = connections.open_generator(Mode.CONTROL)
    except (InstrumentNotFoundError, InstrumentNotRespondingError) as error:
        report.skip("Generator control-mode steps", str(error))
        return
    snapshot = generator.settings()
    report.info("Generator settings before", snapshot.as_dict())
    try:
        generator.configure(
            1,
            shape=Shape.SINE,
            frequency=1e3,
            amplitude=1.0,
            offset=0.0,
            load=math.inf,
            output=True,
        )
        readback = generator.channel_settings(1)
        report.check(
            "Generator settings read back",
            readback.shape == "SIN"
            and math.isclose(readback.frequency_hz, 1e3, rel_tol=1e-6)
            and math.isclose(readback.amplitude_vpp, 1.0, rel_tol=1e-3)
            and readback.load_ohms == math.inf
            and readback.output_on,
            readback,
        )
        report.ask("afg_shows_1vpp", "Does the AFG display CH1 amplitude 1.000 Vpp?")
        if scope is not None:
            check_combined(report, scope)
            check_measurement_probes(report, scope)
        generator.align_phase()
        report.check("Align phase accepted", True, "SOURce1:PHASe:INITiate")
        report.info("*OPC? on the AFG", probe_opc(generator))
    finally:
        notes = restore_generator(generator, snapshot)
        report.info("Generator settings restored", notes or "all channels")
        generator.close()
    report.ask("afg_panel_after_control_close", "Is the AFG front panel usable now?")


def probe_opc(generator: Generator) -> str:
    try:
        reply = generator.query("*OPC?", timeout_s=2.0)
    except InstrumentNotRespondingError as error:
        generator.read_errors()
        return f"no reply ({error.detail})"
    return f"replied {reply!r}"


def check_combined(report: Report, scope: Scope) -> None:
    report.prompt(
        "Connect generator CH1 to oscilloscope CH1 with a BNC cable, set the oscilloscope "
        "to 500 us/div and CH1 to 200 mV/div, and trigger on CH1."
    )
    if not scope.channel_displayed(1):
        report.check("Scope CH1 switched on", False, "switch on CH1 and rerun")
        return
    waveform = scope.read_waveform(1, data_format=WaveformFormat.WORD)
    vpp = peak_to_peak(waveform)
    frequency = estimate_frequency(waveform)
    report.check(
        "Scope measures 1.0 Vpp from the generator",
        abs(vpp - 1.0) <= AMPLITUDE_TOLERANCE,
        f"{vpp:.4f} V",
    )
    report.check(
        "Scope measures 1 kHz from the generator",
        frequency is not None and abs(frequency - 1e3) <= FREQUENCY_TOLERANCE * 1e3,
        frequency,
    )


def check_measurement_probes(report: Report, scope: Scope) -> None:
    if not report.ask(
        "both_channels_connected",
        "Is the same generator signal also on scope CH2 (for example through a BNC tee)?",
    ):
        report.skip("Two-source measurement probes", "CH2 not connected")
        return
    previous_mode = scope.mode
    scope.mode = Mode.CONTROL
    try:
        waveforms = scope.read_waveforms([1, 2], data_format=WaveformFormat.WORD)
        python_phase = fitted_phase_deg(waveforms[2], 1e3) - fitted_phase_deg(waveforms[1], 1e3)
        report.info(
            ":MEASure:PHASe? CHANnel1,CHANnel2 (deg)",
            {
                "scope": scope.measure(Measurement.PHASE, 1, 2),
                "python_sine_fit": round(python_phase, 2),
            },
        )
        report.info(":MEASure:DELay? CHANnel1,CHANnel2 (s)", scope.measure(Measurement.DELAY, 1, 2))
        scope.stop()
        raw = scope.read_waveform(1, points_mode=PointsMode.RAW)
        report.check("RAW read after :STOP", raw.volts.size > 1000, f"{raw.volts.size} points")
        scope.run()
        report.prompt("Clear the measurements LabRador added on the scope screen, if any.")
    finally:
        scope.mode = previous_mode


def run(arguments: argparse.Namespace) -> int:
    report = Report(assume_yes=arguments.assume_yes)
    connections = Connections(arguments.simulated)
    scope = None
    try:
        if not check_visa(report, connections):
            return finish(report)
        scope = check_scope_observe(report, connections)
        if scope is not None:
            check_scope_front_panel(report, scope, arguments.simulated)
        check_generator_remote_mode(report, connections)
        if arguments.control:
            check_generator_and_scope_control(report, connections, scope)
        else:
            report.skip("Control-mode steps 5-7", "run with --control to include them")
    except LabradorError as error:
        report.check("Unexpected error", False, str(error))
    finally:
        if scope is not None:
            scope.close()
            report.ask("scope_panel_after_close", "Is the oscilloscope front panel usable?")
    return finish(report)


def finish(report: Report) -> int:
    path = report.save(OUTPUT_DIRECTORY)
    print(f"\nReport saved to {path}")
    return 1 if report.failed() else 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Check LabRador against the real instruments. Read-only unless --control."
    )
    parser.add_argument(
        "--control",
        action="store_true",
        help="also run the steps that change generator and oscilloscope settings",
    )
    parser.add_argument(
        "--simulated", action="store_true", help="run against the simulators (dry run)"
    )
    parser.add_argument(
        "--assume-yes", action="store_true", help="answer every question yes (dry run)"
    )
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
