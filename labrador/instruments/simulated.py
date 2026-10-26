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
