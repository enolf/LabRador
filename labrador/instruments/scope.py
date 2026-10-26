from collections.abc import Sequence
from dataclasses import asdict, dataclass
from enum import StrEnum

import numpy as np

from labrador.errors import (
    ChannelNotDisplayedError,
    SettingsChangedError,
    WaveformUnavailableError,
)
from labrador.instruments.base import Identity, Instrument
from labrador.instruments.scpi import CommandPolicy
from labrador.instruments.transport import Transport

INVALID_MEASUREMENT_THRESHOLD = 1e30
SCREENSHOT_TIMEOUT_S = 20.0
FULL_MEMORY_TIMEOUT_S = 20.0
COMMAND_TIMEOUT_S = 20.0
RUN_BIT = 1 << 3
PEAK_DETECT_TYPE = 1
NORMAL_POINTS = (100, 250, 500, 1000)
FULL_MEMORY_POINTS = (
    100,
    250,
    500,
    1000,
    2000,
    5000,
    10_000,
    20_000,
    50_000,
    100_000,
    200_000,
    500_000,
    1_000_000,
    2_000_000,
)
MIN_AVERAGE_COUNT = 2
MAX_AVERAGE_COUNT = 65536


class PointsMode(StrEnum):
    NORMAL = "NORMal"
    MAXIMUM = "MAXimum"
    RAW = "RAW"


class WaveformFormat(StrEnum):
    BYTE = "BYTE"
    WORD = "WORD"


class Coupling(StrEnum):
    AC = "AC"
    DC = "DC"


class TriggerSource(StrEnum):
    CHANNEL1 = "CHANnel1"
    CHANNEL2 = "CHANnel2"
    EXTERNAL = "EXTernal"
    LINE = "LINE"


class Slope(StrEnum):
    POSITIVE = "POSitive"
    NEGATIVE = "NEGative"
    EITHER = "EITHer"
    ALTERNATE = "ALTernate"


class Sweep(StrEnum):
    AUTO = "AUTO"
    NORMAL = "NORMal"


class AcquireType(StrEnum):
    NORMAL = "NORMal"
    AVERAGE = "AVERage"
    HIGH_RESOLUTION = "HRESolution"
    PEAK = "PEAK"


class Measurement(StrEnum):
    FREQUENCY = "FREQuency"
    VPP = "VPP"
    PHASE = "PHASe"
    DELAY = "DELay"

    @property
    def needs_two_sources(self) -> bool:
        return self in (Measurement.PHASE, Measurement.DELAY)


@dataclass(frozen=True)
class Preamble:
    format: int
    type: int
    points: int
    count: int
    x_increment: float
    x_origin: float
    x_reference: int
    y_increment: float
    y_origin: float
    y_reference: int

    @classmethod
    def parse(cls, reply: str) -> "Preamble":
        fields = [field.strip() for field in reply.split(",")]
        if len(fields) != 10:
            raise ValueError(f"Expected 10 preamble fields, got {len(fields)}: {reply!r}")
        return cls(
            format=int(float(fields[0])),
            type=int(float(fields[1])),
            points=int(float(fields[2])),
            count=int(float(fields[3])),
            x_increment=float(fields[4]),
            x_origin=float(fields[5]),
            x_reference=int(float(fields[6])),
            y_increment=float(fields[7]),
            y_origin=float(fields[8]),
            y_reference=int(float(fields[9])),
        )

    def same_scaling_as(self, other: "Preamble") -> bool:
        return (
            self.format == other.format
            and self.type == other.type
            and self.points == other.points
            and self.x_increment == other.x_increment
            and self.x_reference == other.x_reference
            and self.y_increment == other.y_increment
            and self.y_origin == other.y_origin
            and self.y_reference == other.y_reference
            and abs(self.x_origin - other.x_origin) <= abs(self.x_increment)
        )


@dataclass(frozen=True)
class Waveform:
    channel: int
    time: np.ndarray
    volts: np.ndarray
    preamble: Preamble
    holes: np.ndarray
    clipped_low: np.ndarray
    clipped_high: np.ndarray

    @property
    def is_clipped(self) -> bool:
        return bool(self.clipped_low.any() or self.clipped_high.any())

    @property
    def has_holes(self) -> bool:
        return bool(self.holes.any())


@dataclass(frozen=True)
class RunState:
    running: bool
    raw: int


@dataclass(frozen=True)
class ChannelSettings:
    displayed: bool
    scale_v_per_div: float
    offset_v: float
    coupling: str
    probe: float


@dataclass(frozen=True)
class ScopeSettings:
    identity: Identity
    channels: dict[int, ChannelSettings]
    timebase_mode: str
    time_per_div_s: float
    position_s: float
    time_reference: str
    trigger_mode: str
    trigger_source: str
    trigger_level_v: float | None
    trigger_slope: str
    trigger_sweep: str
    acquire_type: str
    acquire_count: int
    acquire_points: int

    def as_dict(self) -> dict[str, object]:
        settings: dict[str, object] = {"instrument": self.identity.raw}
        for channel, values in self.channels.items():
            settings[f"ch{channel}"] = asdict(values)
        for name, value in asdict(self).items():
            if name not in ("identity", "channels"):
                settings[name] = value
        return settings


def decode_waveform(channel: int, block: bytes, preamble: Preamble) -> Waveform:
    if preamble.format == 1:
        raw = np.frombuffer(block, dtype="<u2").astype(np.int64)
        clip_high_code = 0xFFFF
    else:
        raw = np.frombuffer(block, dtype=np.uint8).astype(np.int64)
        clip_high_code = 0xFF
    holes = raw == 0
    clipped_low = raw == 1
    clipped_high = raw == clip_high_code
    volts = (raw - preamble.y_reference) * preamble.y_increment + preamble.y_origin
    volts[holes] = np.nan
    index = np.arange(raw.size)
    if preamble.type == PEAK_DETECT_TYPE:
        time = (index // 2 - preamble.x_reference) * preamble.x_increment * 2 + preamble.x_origin
    else:
        time = (index - preamble.x_reference) * preamble.x_increment + preamble.x_origin
    return Waveform(channel, time, volts, preamble, holes, clipped_low, clipped_high)


class Scope(Instrument):
    model = "EDUX1052A"
    vendor_id = 0x2A8D
    description = "oscilloscope"
    channels = (1, 2)
    error_query = ":SYSTem:ERRor?"
    policy = CommandPolicy.from_notation(
        [
            "*IDN?",
            "*OPC?",
            ":SYSTem:ERRor?",
            ":SYSTem:LOCK?",
            ":OPERegister:CONDition?",
            ":TER?",
            ":CHANnel#:DISPlay?",
            ":CHANnel#:SCALe?",
            ":CHANnel#:OFFSet?",
            ":CHANnel#:COUPling?",
            ":CHANnel#:PROBe?",
            ":TIMebase:MODE?",
            ":TIMebase:SCALe?",
            ":TIMebase:POSition?",
            ":TIMebase:REFerence?",
            ":TRIGger:MODE?",
            ":TRIGger[:EDGE]:SOURce?",
            ":TRIGger[:EDGE]:LEVel?",
            ":TRIGger[:EDGE]:SLOPe?",
            ":TRIGger:SWEep?",
            ":ACQuire:TYPE?",
            ":ACQuire:COUNt?",
            ":ACQuire:POINts?",
            ":WAVeform:PREamble?",
            ":WAVeform:DATA?",
            ":WAVeform:SOURce?",
            ":WAVeform:FORMat?",
            ":WAVeform:BYTeorder?",
            ":WAVeform:UNSigned?",
            ":WAVeform:POINts?",
            ":WAVeform:POINts:MODE?",
            ":DISPlay:DATA?",
            ":WAVeform:SOURce",
            ":WAVeform:FORMat",
            ":WAVeform:BYTeorder",
            ":WAVeform:UNSigned",
            ":WAVeform:POINts",
            ":WAVeform:POINts:MODE",
        ],
        [":SYSTem:LOCK"],
    )

    @classmethod
    def simulated_transport(cls) -> Transport:
        from labrador.instruments.simulated import SimulatedScopeDevice

        return SimulatedScopeDevice()

    def channel_displayed(self, channel: int) -> bool:
        return self.query_int(f":CHANnel{self._channel(channel)}:DISPlay?") != 0

    def displayed_channels(self) -> tuple[int, ...]:
        return tuple(channel for channel in self.channels if self.channel_displayed(channel))

    def read_preamble(self, channel: int) -> Preamble:
        self.write(f":WAVeform:SOURce CHANnel{self._channel(channel)}")
        return Preamble.parse(self.query(":WAVeform:PREamble?"))

    def read_waveform(
        self,
        channel: int,
        *,
        points: int | None = None,
        points_mode: PointsMode = PointsMode.NORMAL,
        data_format: WaveformFormat = WaveformFormat.BYTE,
        retries: int = 3,
    ) -> Waveform:
        channel = self._channel(channel)
        points_mode = PointsMode(points_mode)
        data_format = WaveformFormat(data_format)
        self._check_points(points, points_mode)
        if not self.channel_displayed(channel):
            raise ChannelNotDisplayedError(channel)
        if points_mode is PointsMode.RAW:
            self._check_full_memory_available()
        with self._lock:
            self._set_transfer(channel, points, points_mode, data_format)
            timeout_s = None if points_mode is PointsMode.NORMAL else FULL_MEMORY_TIMEOUT_S
            for _ in range(max(retries, 1)):
                before = Preamble.parse(self.query(":WAVeform:PREamble?"))
                block = self.query_block(":WAVeform:DATA?", timeout_s=timeout_s)
                after = Preamble.parse(self.query(":WAVeform:PREamble?"))
                if before.same_scaling_as(after):
                    break
            else:
                raise SettingsChangedError(channel, max(retries, 1))
        if not block:
            raise WaveformUnavailableError(
                f"channel {channel} returned no data; check that the oscilloscope has triggered"
            )
        return decode_waveform(channel, block, after)

    def read_waveforms(
        self,
        channels: Sequence[int] | None = None,
        *,
        points: int | None = None,
        points_mode: PointsMode = PointsMode.NORMAL,
        data_format: WaveformFormat = WaveformFormat.BYTE,
        retries: int = 3,
    ) -> dict[int, Waveform]:
        selected = self.displayed_channels() if channels is None else tuple(channels)
        return {
            channel: self.read_waveform(
                channel,
                points=points,
                points_mode=points_mode,
                data_format=data_format,
                retries=retries,
            )
            for channel in selected
        }

    def channel_settings(self, channel: int) -> ChannelSettings:
        prefix = f":CHANnel{self._channel(channel)}"
        return ChannelSettings(
            displayed=self.query_int(f"{prefix}:DISPlay?") != 0,
            scale_v_per_div=self.query_float(f"{prefix}:SCALe?"),
            offset_v=self.query_float(f"{prefix}:OFFSet?"),
            coupling=self.query(f"{prefix}:COUPling?"),
            probe=self.query_float(f"{prefix}:PROBe?"),
        )

    def settings(self) -> ScopeSettings:
        trigger_source = self.query(":TRIGger:EDGE:SOURce?")
        has_level = trigger_source.upper().startswith(("CHAN", "EXT"))
        return ScopeSettings(
            identity=self.identity,
            channels={channel: self.channel_settings(channel) for channel in self.channels},
            timebase_mode=self.query(":TIMebase:MODE?"),
            time_per_div_s=self.query_float(":TIMebase:SCALe?"),
            position_s=self.query_float(":TIMebase:POSition?"),
            time_reference=self.query(":TIMebase:REFerence?"),
            trigger_mode=self.query(":TRIGger:MODE?"),
            trigger_source=trigger_source,
            trigger_level_v=self.query_float(":TRIGger:EDGE:LEVel?") if has_level else None,
            trigger_slope=self.query(":TRIGger:EDGE:SLOPe?"),
            trigger_sweep=self.query(":TRIGger:SWEep?"),
            acquire_type=self.query(":ACQuire:TYPE?"),
            acquire_count=self.query_int(":ACQuire:COUNt?"),
            acquire_points=self.query_int(":ACQuire:POINts?"),
        )

    def run_state(self) -> RunState:
        raw = self.query_int(":OPERegister:CONDition?")
        return RunState(running=bool(raw & RUN_BIT), raw=raw)

    def trigger_occurred(self) -> bool:
        return self.query_int(":TER?") == 1

    def screenshot(self) -> bytes:
        return self.query_block(":DISPlay:DATA? PNG, COLor", timeout_s=SCREENSHOT_TIMEOUT_S)

    def reset(self) -> None:
        self._write_and_wait("*RST")

    def preset(self) -> None:
        self._write_and_wait(":SYSTem:PRESet")

    def autoscale(self) -> None:
        self._write_and_wait(":AUToscale")

    def run(self) -> None:
        self.write(":RUN")

    def stop(self) -> None:
        self.write(":STOP")

    def single(self) -> None:
        self.write(":SINGle")

    def digitize(
        self, channels: Sequence[int] | None = None, *, timeout_s: float = COMMAND_TIMEOUT_S
    ) -> None:
        sources = ",".join(f"CHANnel{self._channel(channel)}" for channel in channels or ())
        self._write_and_wait(f":DIGitize {sources}".rstrip(), timeout_s=timeout_s)

    def configure_channel(
        self,
        channel: int,
        *,
        display: bool | None = None,
        scale: float | None = None,
        offset: float | None = None,
        coupling: Coupling | None = None,
        probe: float | None = None,
    ) -> None:
        prefix = f":CHANnel{self._channel(channel)}"
        if probe is not None:
            self.write(f"{prefix}:PROBe {probe:.6G}")
        if display is not None:
            self.write(f"{prefix}:DISPlay {1 if display else 0}")
        if scale is not None:
            self.write(f"{prefix}:SCALe {scale:.6G}")
        if offset is not None:
            self.write(f"{prefix}:OFFSet {offset:.6G}")
        if coupling is not None:
            self.write(f"{prefix}:COUPling {Coupling(coupling).value}")

    def configure_timebase(
        self, *, scale: float | None = None, position: float | None = None
    ) -> None:
        if scale is not None:
            self.write(f":TIMebase:SCALe {scale:.6G}")
        if position is not None:
            self.write(f":TIMebase:POSition {position:.6G}")

    def configure_trigger(
        self,
        *,
        source: TriggerSource | None = None,
        level: float | None = None,
        slope: Slope | None = None,
        sweep: Sweep | None = None,
    ) -> None:
        if source is not None or level is not None or slope is not None:
            self.write(":TRIGger:MODE EDGE")
        if source is not None:
            self.write(f":TRIGger:EDGE:SOURce {TriggerSource(source).value}")
        if level is not None:
            self.write(f":TRIGger:EDGE:LEVel {level:.6G}")
        if slope is not None:
            self.write(f":TRIGger:EDGE:SLOPe {Slope(slope).value}")
        if sweep is not None:
            self.write(f":TRIGger:SWEep {Sweep(sweep).value}")

    def configure_acquisition(
        self, *, type: AcquireType | None = None, count: int | None = None
    ) -> None:
        if count is not None and not MIN_AVERAGE_COUNT <= count <= MAX_AVERAGE_COUNT:
            raise ValueError(
                f"Average count must be {MIN_AVERAGE_COUNT} to {MAX_AVERAGE_COUNT}, got {count}"
            )
        if type is not None:
            self.write(f":ACQuire:TYPE {AcquireType(type).value}")
        if count is not None:
            self.write(f":ACQuire:COUNt {count}")

    def measure(
        self, quantity: Measurement, source: int, second_source: int | None = None
    ) -> float | None:
        quantity = Measurement(quantity)
        sources = f"CHANnel{self._channel(source)}"
        if quantity.needs_two_sources:
            if second_source is None:
                raise ValueError(f"{quantity.name} needs two sources")
            sources += f",CHANnel{self._channel(second_source)}"
        elif second_source is not None:
            raise ValueError(f"{quantity.name} takes one source")
        value = self.query_float(f":MEASure:{quantity.value}? {sources}")
        return None if abs(value) > INVALID_MEASUREMENT_THRESHOLD else value

    def _set_transfer(
        self,
        channel: int,
        points: int | None,
        points_mode: PointsMode,
        data_format: WaveformFormat,
    ) -> None:
        self.write(f":WAVeform:SOURce CHANnel{channel}")
        self.write(f":WAVeform:FORMat {data_format.value}")
        if data_format is WaveformFormat.WORD:
            self.write(":WAVeform:BYTeorder LSBFirst")
        self.write(":WAVeform:UNSigned ON")
        if points is None and points_mode is not PointsMode.NORMAL:
            self.write(":WAVeform:POINts MAXimum")
            self.write(f":WAVeform:POINts:MODE {points_mode.value}")
        else:
            self.write(f":WAVeform:POINts:MODE {points_mode.value}")
            self.write(f":WAVeform:POINts {points or NORMAL_POINTS[-1]}")

    def _check_points(self, points: int | None, points_mode: PointsMode) -> None:
        if points is None:
            return
        allowed = NORMAL_POINTS if points_mode is PointsMode.NORMAL else FULL_MEMORY_POINTS
        if points not in allowed:
            raise ValueError(f"{points_mode.name} points must be one of {allowed}, got {points}")

    def _check_full_memory_available(self) -> None:
        problems = []
        if self.run_state().running:
            problems.append("press Stop (or Single) on the oscilloscope first")
        if not self.query(":TIMebase:MODE?").upper().startswith("MAIN"):
            problems.append("set the horizontal mode to normal (not zoom, XY or roll)")
        if not self.query(":ACQuire:TYPE?").upper().startswith(("NORM", "HRES")):
            problems.append("set the acquisition mode to Normal or High Resolution")
        if problems:
            raise WaveformUnavailableError("; ".join(problems))

    def _write_and_wait(self, command: str, *, timeout_s: float = COMMAND_TIMEOUT_S) -> None:
        with self._lock, self._timeout(timeout_s):
            self.write(command)
            self.query("*OPC?")
