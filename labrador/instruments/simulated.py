import math
import re
from collections.abc import Callable
from dataclasses import dataclass

import numpy as np

from labrador.instruments.scpi import ScpiCommand, ScpiPattern, parse_message
from labrador.instruments.transport import TransportError

GENERATOR_SOURCE_OHMS = 50.0
INFINITY_REPLY = "9.9E+37"

_QUANTITY = re.compile(r"^\s*([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([a-zA-Z]*)\s*$")
_FREQUENCY_UNITS = {"": 1.0, "HZ": 1.0, "KHZ": 1e3, "MHZ": 1e6}
_VOLTAGE_UNITS = {"": 1.0, "V": 1.0, "MV": 1e-3}
_AMPLITUDE_UNITS = {"": 1.0, "VPP": 1.0}
_OHM_UNITS = {"": 1.0, "OHM": 1.0}

Handler = Callable[[ScpiCommand, list[int | None]], str | None]


class ScpiArgumentError(ValueError):
    pass


def parse_quantity(text: str, units: dict[str, float]) -> float:
    found = _QUANTITY.match(text)
    if found is None:
        raise ScpiArgumentError(text)
    number, unit = found.groups()
    scale = units.get(unit.upper())
    if scale is None:
        raise ScpiArgumentError(text)
    return float(number) * scale


def parse_choice(text: str, options: list[str]) -> str:
    value = text.strip().upper()
    for option in options:
        short_form = "".join(character for character in option if not character.islower())
        if value in (option.upper(), short_form.upper()):
            return short_form.upper()
    raise ScpiArgumentError(text)


def parse_switch(text: str) -> bool:
    value = text.strip().upper()
    if value in ("ON", "1"):
        return True
    if value in ("OFF", "0"):
        return False
    raise ScpiArgumentError(text)


class SimulatedDevice:
    error_reply_format = '{code},"{message}"'

    def __init__(self, identity: str) -> None:
        self.identity = identity
        self.timeout_s = 5.0
        self.sent: list[str] = []
        self.error_queue: list[tuple[int, str]] = []
        self.remote = False
        self.go_to_local_calls = 0
        self.closed = False
        self._pending_block = b""
        self._handlers: list[tuple[ScpiPattern, Handler]] = []
        self.add_handler("*IDN?", lambda command, suffixes: self.identity)
        self.add_handler("*CLS", self._clear_status)
        self.add_handler("*OPC?", lambda command, suffixes: "1")

    def add_handler(self, notation: str, handler: Handler) -> None:
        self._handlers.append((ScpiPattern(notation), handler))

    def write(self, message: str) -> None:
        self._execute(message)

    def query(self, message: str) -> str:
        replies = self._execute(message)
        if not replies:
            raise TransportError(f"{message!r}: timed out waiting for a reply", timed_out=True)
        return ";".join(replies)

    def query_block(self, message: str) -> bytes:
        reply = self.query(message)
        if not reply.startswith("#"):
            raise TransportError(f"{message!r}: expected a binary block, got {reply[:20]!r}")
        return self._pending_block

    def go_to_local(self) -> bool:
        self.go_to_local_calls += 1
        self.remote = False
        return True

    def close(self) -> None:
        self.closed = True

    def connect(self) -> "SimulatedDevice":
        self.closed = False
        return self

    def push_error(self, code: int, message: str) -> None:
        self.error_queue.append((code, message))

    def next_error(self) -> str:
        code, message = self.error_queue.pop(0) if self.error_queue else (0, "No error")
        return self.error_reply_format.format(code=code, message=message)

    def _clear_status(self, command: ScpiCommand, suffixes: list[int | None]) -> None:
        self.error_queue.clear()

    def _execute(self, message: str) -> list[str]:
        if self.closed:
            raise TransportError(f"{message!r}: the simulated connection is closed")
        self.sent.append(message)
        self.remote = True
        self._pending_block = b""
        try:
            commands = parse_message(message)
        except ValueError:
            self.push_error(-102, "Syntax error")
            return []
        replies = []
        for command in commands:
            reply = self._dispatch(command)
            if reply is not None:
                replies.append(reply)
        return replies

    def _dispatch(self, command: ScpiCommand) -> str | None:
        for pattern, handler in self._handlers:
            suffixes = pattern.suffixes(command)
            if suffixes is None:
                continue
            try:
                return handler(command, suffixes)
            except ScpiArgumentError:
                self.push_error(-224, "Illegal parameter value")
                return None
        self.push_error(-113, "Undefined header")
        return None

    def _block_reply(self, data: bytes) -> str:
        self._pending_block = data
        return f"#8{len(data):08d}"


@dataclass
class GeneratorChannelState:
    shape: str = "SIN"
    frequency_hz: float = 1000.0
    amplitude_vpp: float = 1.0
    offset_v: float = 0.0
    load_ohms: float = 50.0
    output_on: bool = False
    phase_rad: float = 0.0

    def source_scale(self) -> float:
        if math.isinf(self.load_ohms):
            return 1.0
        return (self.load_ohms + GENERATOR_SOURCE_OHMS) / self.load_ohms

    def open_circuit_amplitude_vpp(self) -> float:
        return self.amplitude_vpp * self.source_scale() if self.output_on else 0.0

    def open_circuit_offset_v(self) -> float:
        return self.offset_v * self.source_scale() if self.output_on else 0.0


_SHAPES = {"SIN": "SIN", "SINUSOID": "SIN", "SQU": "SQU", "SQUARE": "SQU", "PULS": "PULS"}
_SHAPES |= {"PULSE": "PULS", "RAMP": "RAMP", "PRN": "PRN", "PRNOISE": "PRN"}


class SimulatedGeneratorDevice(SimulatedDevice):
    def __init__(self, serial: str = "SIM0001") -> None:
        super().__init__(f"TEKTRONIX,AFG1062,{serial},SCPI:99.0 FV:simulated")
        self.channels = {1: GeneratorChannelState(), 2: GeneratorChannelState()}
        self.phase_alignments = 0
        self.add_handler("SYSTem:ERRor[:NEXT]?", lambda command, suffixes: self.next_error())
        self.add_handler("*RST", self._reset)
        self.add_handler("[SOURce#]:FUNCtion[:SHAPe]", self._set_shape)
        self.add_handler("[SOURce#]:FUNCtion[:SHAPe]?", self._get("shape"))
        self.add_handler("[SOURce#]:FREQuency[:CW|:FIXed]", self._set_frequency)
        self.add_handler("[SOURce#]:FREQuency[:CW|:FIXed]?", self._get("frequency_hz", "{:.10E}"))
        self.add_handler("[SOURce#]:VOLTage[:LEVel][:IMMediate]:OFFSet", self._set_offset)
        self.add_handler(
            "[SOURce#]:VOLTage[:LEVel][:IMMediate]:OFFSet?", self._get("offset_v", "{:.4E}")
        )
        self.add_handler("[SOURce#]:VOLTage[:LEVel][:IMMediate][:AMPLitude]", self._set_amplitude)
        self.add_handler(
            "[SOURce#]:VOLTage[:LEVel][:IMMediate][:AMPLitude]?",
            self._get("amplitude_vpp", "{:.4E}"),
        )
        self.add_handler("OUTPut#:IMPedance", self._set_load)
        self.add_handler("OUTPut#:IMPedance?", self._get_load)
        self.add_handler("OUTPut#[:STATe]", self._set_output)
        self.add_handler("OUTPut#[:STATe]?", self._get_output)
        self.add_handler("[SOURce#]:PHASe:INITiate", self._align_phase)

    def waveform(self, channel: int, time_s: np.ndarray) -> np.ndarray:
        state = self.channels[channel]
        amplitude = state.open_circuit_amplitude_vpp() / 2
        offset = state.open_circuit_offset_v()
        cycles = state.frequency_hz * time_s + state.phase_rad / (2 * math.pi)
        if state.shape in ("SQU", "PULS"):
            unit = np.where((cycles % 1.0) < 0.5, 1.0, -1.0)
        elif state.shape == "RAMP":
            unit = 2.0 * (cycles % 1.0) - 1.0
        elif state.shape == "PRN":
            unit = np.random.default_rng(0).uniform(-1.0, 1.0, time_s.shape)
        else:
            unit = np.sin(2 * math.pi * cycles)
        return offset + amplitude * unit

    def _channel_state(self, suffixes: list[int | None]) -> GeneratorChannelState:
        channel = suffixes[0] or 1
        if channel not in self.channels:
            raise ScpiArgumentError(str(channel))
        return self.channels[channel]

    def _get(self, field: str, form: str = "{}") -> Handler:
        def handler(command: ScpiCommand, suffixes: list[int | None]) -> str:
            return form.format(getattr(self._channel_state(suffixes), field))

        return handler

    def _reset(self, command: ScpiCommand, suffixes: list[int | None]) -> None:
        self.channels = {1: GeneratorChannelState(), 2: GeneratorChannelState()}

    def _set_shape(self, command: ScpiCommand, suffixes: list[int | None]) -> None:
        shape = _SHAPES.get(command.arguments.strip().upper())
        if shape is None:
            raise ScpiArgumentError(command.arguments)
        self._channel_state(suffixes).shape = shape

    def _set_frequency(self, command: ScpiCommand, suffixes: list[int | None]) -> None:
        self._channel_state(suffixes).frequency_hz = parse_quantity(
            command.arguments, _FREQUENCY_UNITS
        )

    def _set_amplitude(self, command: ScpiCommand, suffixes: list[int | None]) -> None:
        self._channel_state(suffixes).amplitude_vpp = parse_quantity(
            command.arguments, _AMPLITUDE_UNITS
        )

    def _set_offset(self, command: ScpiCommand, suffixes: list[int | None]) -> None:
        self._channel_state(suffixes).offset_v = parse_quantity(command.arguments, _VOLTAGE_UNITS)

    def _set_load(self, command: ScpiCommand, suffixes: list[int | None]) -> None:
        text = command.arguments.strip().upper()
        if text in ("INF", "INFINITY"):
            ohms = math.inf
        else:
            ohms = parse_quantity(text, _OHM_UNITS)
            if not 1.0 <= ohms <= 10_000.0:
                raise ScpiArgumentError(text)
        self._channel_state(suffixes).load_ohms = ohms

    def _get_load(self, command: ScpiCommand, suffixes: list[int | None]) -> str:
        ohms = self._channel_state(suffixes).load_ohms
        return INFINITY_REPLY if math.isinf(ohms) else f"{ohms:.4E}"

    def _set_output(self, command: ScpiCommand, suffixes: list[int | None]) -> None:
        self._channel_state(suffixes).output_on = parse_switch(command.arguments)

    def _get_output(self, command: ScpiCommand, suffixes: list[int | None]) -> str:
        return "1" if self._channel_state(suffixes).output_on else "0"

    def _align_phase(self, command: ScpiCommand, suffixes: list[int | None]) -> None:
        self.phase_alignments += 1
        for state in self.channels.values():
            state.phase_rad = 0.0


Signal = Callable[[int, np.ndarray], np.ndarray]
SignalPath = Callable[[float], complex]

HORIZONTAL_DIVISIONS = 10
REFERENCE_DIVISIONS = {"LEFT": 1.0, "CENT": 5.0, "RIGH": 9.0}
BYTE_REFERENCE = 128
BYTE_CODES_PER_DIVISION = 25.0
WORD_REFERENCE = 32768
WORD_STEP = 16
RAW_RECORD_POINTS = 20_000
NORMAL_RECORD_POINTS = 1000
DEFAULT_NOISE_V = 0.002
INVALID_MEASUREMENT_REPLY = "+9.9E+37"
PREAMBLE_TYPES = {"NORM": 0, "PEAK": 1, "AVER": 2, "HRES": 0}
RUN_BIT = 1 << 3
WAIT_TRIGGER_BIT = 1 << 5
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def through() -> SignalPath:
    return lambda frequency_hz: complex(1.0)


def transmission_line(
    z0_ohms: float,
    delay_s: float,
    *,
    source_ohms: float = GENERATOR_SOURCE_OHMS,
    input_capacitance_f: float = 15e-12,
    input_resistance_ohms: float = 1e6,
) -> SignalPath:
    def gain(frequency_hz: float) -> complex:
        omega = 2 * math.pi * frequency_hz
        load = input_resistance_ohms / (
            1 + 1j * omega * input_resistance_ohms * input_capacitance_f
        )
        source_reflection = (source_ohms - z0_ohms) / (source_ohms + z0_ohms)
        load_reflection = (load - z0_ohms) / (load + z0_ohms)
        transit = complex(math.cos(omega * delay_s), -math.sin(omega * delay_s))
        return (
            z0_ohms
            / (source_ohms + z0_ohms)
            * (1 + load_reflection)
            * transit
            / (1 - source_reflection * load_reflection * transit**2)
        )

    return gain


def default_signal(channel: int, time_s: np.ndarray) -> np.ndarray:
    if channel == 1:
        return 0.5 * np.sin(2 * math.pi * 1e3 * time_s)
    return 0.25 * np.sin(2 * math.pi * 1e3 * time_s - math.pi / 4)


def generator_signal(
    generator: SimulatedGeneratorDevice, paths: dict[int, SignalPath] | None = None
) -> Signal:
    chosen_paths = paths or {}

    def signal(channel: int, time_s: np.ndarray) -> np.ndarray:
        state = generator.channels[channel]
        path = chosen_paths.get(channel, through())
        response = path(state.frequency_hz)
        delay_s = -np.angle(response) / (2 * math.pi * state.frequency_hz)
        offset = state.open_circuit_offset_v() * path(0.0).real
        wave = generator.waveform(channel, time_s - delay_s)
        return offset + abs(response) * (wave - state.open_circuit_offset_v())

    return signal


@dataclass
class ScopeChannelState:
    displayed: bool = True
    scale_v_per_div: float = 0.2
    offset_v: float = 0.0
    coupling: str = "DC"
    probe: float = 1.0


@dataclass
class _Record:
    volts: dict[int, np.ndarray]
    x_origin: float
    x_increment: float


class SimulatedScopeDevice(SimulatedDevice):
    error_reply_format = '{code:+d},"{message}"'

    def __init__(
        self,
        signal: Signal = default_signal,
        *,
        noise_v: float = DEFAULT_NOISE_V,
        serial: str = "SIM0001",
        seed: int = 0,
    ) -> None:
        super().__init__(f"KEYSIGHT TECHNOLOGIES,EDUX1052A,{serial},simulated")
        self.signal = signal
        self.noise_v = noise_v
        self._rng = np.random.default_rng(seed)
        self.locked = False
        self.installed_measurements: list[str] = []
        self.acquisitions = 0
        self._knob_turns = 0
        self._reset_state()
        self._register_handlers()

    def press_run(self) -> None:
        self.running = True
        self.single_pending = False

    def press_stop(self) -> None:
        self.running = False
        self.single_pending = False

    def press_single(self) -> None:
        self.running = True
        self.single_pending = True

    def deliver_trigger(self) -> None:
        if self._acquire() and self.single_pending:
            self.running = False
            self.single_pending = False

    def set_channel_displayed(self, channel: int, displayed: bool) -> None:
        self.channels[channel].displayed = displayed

    def turn_knob_during_next_reads(self, count: int = 1) -> None:
        self._knob_turns = count

    def status_register(self) -> int:
        if not self.running:
            return 0
        waiting = self.single_pending or self._trigger_offset() is None
        return RUN_BIT | (WAIT_TRIGGER_BIT if waiting else 0)

    def _reset_state(self) -> None:
        self.channels = {1: ScopeChannelState(), 2: ScopeChannelState()}
        self.timebase_mode = "MAIN"
        self.time_per_div_s = 2e-4
        self.position_s = 0.0
        self.time_reference = "CENT"
        self.trigger_mode = "EDGE"
        self.trigger_source = "CHAN1"
        self.trigger_level_v = 0.0
        self.trigger_slope = "POS"
        self.trigger_sweep = "AUTO"
        self.acquire_type = "NORM"
        self.acquire_count = 8
        self.running = True
        self.single_pending = False
        self.trigger_event = False
        self.waveform_source = 1
        self.waveform_format = "BYTE"
        self.byte_order = "MSBF"
        self.unsigned = True
        self.waveform_points: int | None = NORMAL_RECORD_POINTS
        self.points_mode = "NORM"
        self._record: _Record | None = None

    def _factory_reset(self, command: ScpiCommand, suffixes: list[int | None]) -> None:
        self._reset_state()
        self.channels = {
            1: ScopeChannelState(scale_v_per_div=5.0, probe=10.0),
            2: ScopeChannelState(displayed=False, scale_v_per_div=5.0, probe=10.0),
        }

    def _register_handlers(self) -> None:
        self.add_handler(":SYSTem:ERRor?", lambda command, suffixes: self.next_error())
        self.add_handler(":SYSTem:LOCK", self._set_lock)
        self.add_handler(":SYSTem:LOCK?", lambda command, suffixes: "1" if self.locked else "0")
        self.add_handler("*RST", self._factory_reset)
        self.add_handler(":SYSTem:PRESet", self._factory_reset)
        self.add_handler("*TST?", lambda command, suffixes: "+0")
        self.add_handler(":AUToscale", self._autoscale)
        self.add_handler(":RUN", lambda command, suffixes: self.press_run())
        self.add_handler(":STOP", lambda command, suffixes: self.press_stop())
        self.add_handler(":SINGle", lambda command, suffixes: self.press_single())
        self.add_handler(":DIGitize", self._digitize)
        self.add_handler(
            ":OPERegister:CONDition?", lambda command, suffixes: f"{self.status_register():+d}"
        )
        self.add_handler(":TER?", self._read_trigger_event)
        self._add_channel_handlers()
        self._add_setting(":TIMebase:MODE", "timebase_mode", ["MAIN", "WINDow", "XY", "ROLL"])
        self._add_number(":TIMebase:SCALe", "time_per_div_s")
        self._add_number(":TIMebase:POSition", "position_s")
        self._add_setting(":TIMebase:REFerence", "time_reference", ["LEFT", "CENTer", "RIGHt"])
        self._add_setting(
            ":TRIGger:MODE", "trigger_mode", ["EDGE", "GLITch", "PATTern", "TV", "SBUS1"]
        )
        self._add_setting(
            ":TRIGger[:EDGE]:SOURce",
            "trigger_source",
            ["CHANnel1", "CHANnel2", "EXTernal", "LINE", "WGEN"],
        )
        self._add_number(":TRIGger[:EDGE]:LEVel", "trigger_level_v")
        self._add_setting(
            ":TRIGger[:EDGE]:SLOPe",
            "trigger_slope",
            ["NEGative", "POSitive", "EITHer", "ALTernate"],
        )
        self._add_setting(":TRIGger:SWEep", "trigger_sweep", ["AUTO", "NORMal"])
        self._add_setting(
            ":ACQuire:TYPE", "acquire_type", ["NORMal", "AVERage", "HRESolution", "PEAK"]
        )
        self.add_handler(":ACQuire:COUNt", self._set_average_count)
        self.add_handler(":ACQuire:COUNt?", lambda command, suffixes: f"{self.acquire_count:+d}")
        self.add_handler(":ACQuire:POINts?", lambda command, suffixes: f"{RAW_RECORD_POINTS:+d}")
        self.add_handler(":WAVeform:SOURce", self._set_waveform_source)
        self.add_handler(
            ":WAVeform:SOURce?", lambda command, suffixes: f"CHAN{self.waveform_source}"
        )
        self._add_setting(":WAVeform:FORMat", "waveform_format", ["WORD", "BYTE", "ASCii"])
        self._add_setting(":WAVeform:BYTeorder", "byte_order", ["LSBFirst", "MSBFirst"])
        self.add_handler(":WAVeform:UNSigned", self._set_unsigned)
        self.add_handler(":WAVeform:UNSigned?", lambda command, suffixes: "1")
        self.add_handler(":WAVeform:POINts", self._set_waveform_points)
        self.add_handler(":WAVeform:POINts?", lambda command, suffixes: f"{self._layout()[0]:+d}")
        self._add_setting(":WAVeform:POINts:MODE", "points_mode", ["NORMal", "MAXimum", "RAW"])
        self.add_handler(":WAVeform:PREamble?", self._preamble)
        self.add_handler(":WAVeform:DATA?", self._waveform_data)
        self.add_handler(":DISPlay:DATA?", self._screen_image)
        for quantity in ("FREQuency", "VPP", "PHASe", "DELay"):
            self.add_handler(f":MEASure:{quantity}?", self._measure)

    def _add_channel_handlers(self) -> None:
        self.add_handler(":CHANnel#:DISPlay", self._channel_setter("displayed", parse_switch))
        self.add_handler(
            ":CHANnel#:DISPlay?",
            self._channel_getter(lambda state: "1" if state.displayed else "0"),
        )
        volts = {"": 1.0, "V": 1.0, "MV": 1e-3}
        self.add_handler(
            ":CHANnel#:SCALe",
            self._channel_setter("scale_v_per_div", lambda text: parse_quantity(text, volts)),
        )
        self.add_handler(
            ":CHANnel#:SCALe?", self._channel_getter(lambda state: f"{state.scale_v_per_div:+.5E}")
        )
        self.add_handler(
            ":CHANnel#:OFFSet",
            self._channel_setter("offset_v", lambda text: parse_quantity(text, volts)),
        )
        self.add_handler(
            ":CHANnel#:OFFSet?", self._channel_getter(lambda state: f"{state.offset_v:+.5E}")
        )
        self.add_handler(
            ":CHANnel#:COUPling",
            self._channel_setter("coupling", lambda text: parse_choice(text, ["AC", "DC"])),
        )
        self.add_handler(":CHANnel#:COUPling?", self._channel_getter(lambda state: state.coupling))
        self.add_handler(":CHANnel#:PROBe", self._set_probe)
        self.add_handler(
            ":CHANnel#:PROBe?", self._channel_getter(lambda state: f"{state.probe:+.5E}")
        )

    def _channel_state(self, suffixes: list[int | None]) -> ScopeChannelState:
        channel = suffixes[0] or 1
        if channel not in self.channels:
            raise ScpiArgumentError(str(channel))
        return self.channels[channel]

    def _channel_setter(self, field: str, parse: Callable[[str], object]) -> Handler:
        def handler(command: ScpiCommand, suffixes: list[int | None]) -> None:
            setattr(self._channel_state(suffixes), field, parse(command.arguments))

        return handler

    def _channel_getter(self, render: Callable[[ScopeChannelState], str]) -> Handler:
        def handler(command: ScpiCommand, suffixes: list[int | None]) -> str:
            return render(self._channel_state(suffixes))

        return handler

    def _add_setting(self, notation: str, field: str, options: list[str]) -> None:
        def setter(command: ScpiCommand, suffixes: list[int | None]) -> None:
            setattr(self, field, parse_choice(command.arguments, options))

        self.add_handler(notation, setter)
        self.add_handler(f"{notation}?", lambda command, suffixes: getattr(self, field))

    def _add_number(self, notation: str, field: str) -> None:
        units = {"": 1.0, "S": 1.0, "V": 1.0, "MV": 1e-3, "MS": 1e-3, "US": 1e-6, "NS": 1e-9}

        def setter(command: ScpiCommand, suffixes: list[int | None]) -> None:
            setattr(self, field, parse_quantity(command.arguments.split(",")[0], units))

        self.add_handler(notation, setter)
        self.add_handler(f"{notation}?", lambda command, suffixes: f"{getattr(self, field):+.8E}")

    def _set_lock(self, command: ScpiCommand, suffixes: list[int | None]) -> None:
        self.locked = parse_switch(command.arguments)

    def _set_probe(self, command: ScpiCommand, suffixes: list[int | None]) -> None:
        state = self._channel_state(suffixes)
        probe = parse_quantity(command.arguments.upper().lstrip("X"), {"": 1.0})
        if not 0.1 <= probe <= 10_000:
            raise ScpiArgumentError(command.arguments)
        state.scale_v_per_div *= probe / state.probe
        state.probe = probe

    def _set_average_count(self, command: ScpiCommand, suffixes: list[int | None]) -> None:
        count = int(parse_quantity(command.arguments, {"": 1.0}))
        if not 2 <= count <= 65536:
            raise ScpiArgumentError(command.arguments)
        self.acquire_count = count

    def _set_waveform_source(self, command: ScpiCommand, suffixes: list[int | None]) -> None:
        source = parse_choice(command.arguments, ["CHANnel1", "CHANnel2"])
        self.waveform_source = int(source[-1])

    def _set_unsigned(self, command: ScpiCommand, suffixes: list[int | None]) -> None:
        if not parse_switch(command.arguments):
            raise ScpiArgumentError("the simulator only produces unsigned data")

    def _set_waveform_points(self, command: ScpiCommand, suffixes: list[int | None]) -> None:
        text = command.arguments.strip().upper()
        if text in ("MAX", "MAXIMUM"):
            self.points_mode = "MAX"
            self.waveform_points = None
            return
        points = int(parse_quantity(text, {"": 1.0}))
        allowed = (100, 250, 500, 1000) if self.points_mode == "NORM" else None
        if allowed is not None and points not in allowed:
            raise ScpiArgumentError(text)
        self.waveform_points = points

    def _read_trigger_event(self, command: ScpiCommand, suffixes: list[int | None]) -> str:
        occurred, self.trigger_event = self.trigger_event, False
        return "+1" if occurred else "+0"

    def _digitize(self, command: ScpiCommand, suffixes: list[int | None]) -> None:
        self._acquire()
        self.running = False
        self.single_pending = False

    def _autoscale(self, command: ScpiCommand, suffixes: list[int | None]) -> None:
        time_s = np.linspace(0, HORIZONTAL_DIVISIONS * self.time_per_div_s, RAW_RECORD_POINTS)
        for channel, state in self.channels.items():
            volts = self.signal(channel, time_s)
            span = float(volts.max() - volts.min())
            state.displayed = span > 0.01
            if state.displayed:
                state.scale_v_per_div = _one_two_five(span / 6)
                state.offset_v = float((volts.max() + volts.min()) / 2)
        self.trigger_mode = "EDGE"
        self.trigger_source = "CHAN1"
        self.trigger_level_v = self.channels[1].offset_v
        self.running = True

    def _reference_divisions(self) -> float:
        return REFERENCE_DIVISIONS.get(self.time_reference, 5.0)

    def _record_axis(self) -> tuple[float, float]:
        x_increment = HORIZONTAL_DIVISIONS * self.time_per_div_s / RAW_RECORD_POINTS
        x_origin = self.position_s - self._reference_divisions() * self.time_per_div_s
        return x_origin, x_increment

    def _trigger_offset(self) -> float | None:
        if not self.trigger_source.startswith("CHAN"):
            return 0.0
        channel = int(self.trigger_source[-1])
        width = HORIZONTAL_DIVISIONS * self.time_per_div_s
        time_s = np.linspace(0.0, width, RAW_RECORD_POINTS)
        volts = self.signal(channel, time_s)
        above = volts >= self.trigger_level_v
        rising = ~above[:-1] & above[1:]
        falling = above[:-1] & ~above[1:]
        if self.trigger_slope == "POS":
            crossings = np.flatnonzero(rising)
        elif self.trigger_slope == "NEG":
            crossings = np.flatnonzero(falling)
        else:
            crossings = np.flatnonzero(rising | falling)
        if crossings.size == 0:
            return None
        index = int(crossings[0])
        step = volts[index + 1] - volts[index]
        fraction = (self.trigger_level_v - volts[index]) / step if step else 0.0
        return float(time_s[index] + fraction * (time_s[1] - time_s[0]))

    def _acquire(self) -> bool:
        offset = self._trigger_offset()
        if offset is None:
            if self.trigger_sweep == "NORM":
                return False
            offset = self.acquisitions * 0.37 * self.time_per_div_s
        x_origin, x_increment = self._record_axis()
        time_s = x_origin + np.arange(RAW_RECORD_POINTS) * x_increment + offset
        noise_v = self.noise_v
        if self.acquire_type == "AVER":
            noise_v /= math.sqrt(self.acquire_count)
        volts = {}
        for channel, state in self.channels.items():
            clean = self.signal(channel, time_s)
            if state.coupling == "AC":
                clean = clean - clean.mean()
            volts[channel] = clean + self._rng.normal(0.0, noise_v, RAW_RECORD_POINTS)
        self._record = _Record(volts, x_origin, x_increment)
        self.acquisitions += 1
        self.trigger_event = True
        return True

    def _full_memory_available(self) -> bool:
        return (
            not self.running
            and self.timebase_mode == "MAIN"
            and self.acquire_type in ("NORM", "HRES")
        )

    def _layout(self) -> tuple[int, int]:
        if self.points_mode == "NORM" or (
            self.points_mode == "MAX" and not self._full_memory_available()
        ):
            requested = self.waveform_points or NORMAL_RECORD_POINTS
            points = min(requested, NORMAL_RECORD_POINTS)
        elif self.points_mode == "RAW" and not self._full_memory_available():
            return 0, 1
        else:
            points = min(self.waveform_points or RAW_RECORD_POINTS, RAW_RECORD_POINTS)
        step = max(RAW_RECORD_POINTS // points, 1)
        return RAW_RECORD_POINTS // step, step

    def _vertical(self, channel: int) -> tuple[float, float, int]:
        state = self.channels[channel]
        byte_increment = state.scale_v_per_div / BYTE_CODES_PER_DIVISION
        if self.waveform_format == "WORD":
            return byte_increment / 256, state.offset_v, WORD_REFERENCE
        return byte_increment, state.offset_v, BYTE_REFERENCE

    def _preamble(self, command: ScpiCommand, suffixes: list[int | None]) -> str | None:
        if not self.channels[self.waveform_source].displayed:
            self.push_error(-221, "Settings conflict")
            return None
        points, step = self._layout()
        record_origin, record_increment = (
            (self._record.x_origin, self._record.x_increment)
            if self._record is not None and not self.running
            else self._record_axis()
        )
        y_increment, y_origin, y_reference = self._vertical(self.waveform_source)
        data_format = 1 if self.waveform_format == "WORD" else 0
        count = self.acquire_count if self.acquire_type == "AVER" else 1
        return (
            f"{data_format:+d},{PREAMBLE_TYPES[self.acquire_type]:+d},{points:+d},{count:+d},"
            f"{record_increment * step:+.10E},{record_origin:+.10E},+0,"
            f"{y_increment:+.8E},{y_origin:+.8E},{y_reference:+d}"
        )

    def _waveform_data(self, command: ScpiCommand, suffixes: list[int | None]) -> str | None:
        channel = self.waveform_source
        if not self.channels[channel].displayed:
            self.push_error(-221, "Settings conflict")
            return None
        if self.running:
            self._acquire()
        points, step = self._layout()
        if points == 0:
            return self._block_reply(b"")
        codes = self._encode(channel, points, step)
        if self._knob_turns > 0:
            self._knob_turns -= 1
            state = self.channels[channel]
            state.scale_v_per_div *= 2 if self._knob_turns % 2 == 0 else 0.5
        return self._block_reply(codes)

    def _encode(self, channel: int, points: int, step: int) -> bytes:
        y_increment, y_origin, y_reference = self._vertical(channel)
        word = self.waveform_format == "WORD"
        if self._record is None:
            codes = np.zeros(points * (2 if self.acquire_type == "PEAK" else 1), dtype=np.int64)
        else:
            volts = self._record.volts[channel][: points * step : step]
            if self.acquire_type == "PEAK":
                spread = abs(self.noise_v) * 2
                volts = np.column_stack([volts - spread, volts + spread]).ravel()
            scaled = (volts - y_origin) / y_increment + y_reference
            if word:
                codes = (np.rint(scaled / WORD_STEP) * WORD_STEP).astype(np.int64)
                codes = np.where(
                    codes < WORD_STEP, 1, np.where(codes > 0xFFFF - WORD_STEP, 0xFFFF, codes)
                )
            else:
                codes = np.rint(scaled).astype(np.int64)
                codes = np.where(codes <= 1, 1, np.where(codes >= 0xFF, 0xFF, codes))
        if not word:
            return codes.astype(np.uint8).tobytes()
        dtype = "<u2" if self.byte_order == "LSBF" else ">u2"
        return codes.astype(dtype).tobytes()

    def _screen_image(self, command: ScpiCommand, suffixes: list[int | None]) -> str:
        image_format = command.arguments.split(",")[0].strip().upper() or "BMP"
        if image_format == "PNG":
            return self._block_reply(PNG_SIGNATURE + b"simulated screen")
        return self._block_reply(b"BMsimulated screen")

    def _measure(self, command: ScpiCommand, suffixes: list[int | None]) -> str:
        quantity = command.nodes[-1]
        self.installed_measurements.append(quantity)
        sources = [
            int(parse_choice(text, ["CHANnel1", "CHANnel2"])[-1])
            for text in command.arguments.split(",")
            if text.strip()
        ] or [1]
        offset = self._trigger_offset() or 0.0
        x_origin, x_increment = self._record_axis()
        time_s = x_origin + np.arange(RAW_RECORD_POINTS) * x_increment
        volts = {channel: self.signal(channel, time_s + offset) for channel in set(sources)}
        value = _measurement(quantity, time_s, [volts[channel] for channel in sources])
        return INVALID_MEASUREMENT_REPLY if value is None else f"{value:+.8E}"


def _one_two_five(value: float) -> float:
    exponent = math.floor(math.log10(value))
    for mantissa in (1.0, 2.0, 5.0, 10.0):
        if mantissa * 10**exponent >= value:
            return mantissa * 10**exponent
    return 10 ** (exponent + 1)


def _rising_crossings(time_s: np.ndarray, volts: np.ndarray) -> np.ndarray:
    level = (volts.max() + volts.min()) / 2
    above = volts >= level
    indices = np.flatnonzero(~above[:-1] & above[1:])
    fractions = (level - volts[indices]) / (volts[indices + 1] - volts[indices])
    return time_s[indices] + fractions * (time_s[1] - time_s[0])


def _measurement(quantity: str, time_s: np.ndarray, volts: list[np.ndarray]) -> float | None:
    first = volts[0]
    if quantity.startswith("VPP"):
        return float(first.max() - first.min())
    crossings = _rising_crossings(time_s, first)
    if crossings.size < 2:
        return None
    period = float(np.mean(np.diff(crossings)))
    if quantity.startswith("FREQ"):
        return 1.0 / period
    if len(volts) < 2:
        return None
    later = _rising_crossings(time_s, volts[1])
    later = later[later >= crossings[0] - period / 2]
    if later.size == 0:
        return None
    delay = float(later[0] - crossings[0])
    if quantity.startswith("PHAS"):
        return delay / period * 360.0
    return delay
