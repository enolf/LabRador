import math
from dataclasses import asdict, dataclass
from enum import StrEnum

import pyvisa

from labrador.instruments.base import DEFAULT_TIMEOUT_S, Identity, Instrument, Mode
from labrador.instruments.scpi import CommandPolicy
from labrador.instruments.transport import Transport

INFINITE_LOAD_THRESHOLD = 1e30
MIN_LOAD_OHMS = 1.0
MAX_LOAD_OHMS = 10_000.0


class Shape(StrEnum):
    SINE = "SINusoid"
    SQUARE = "SQUare"
    PULSE = "PULSe"
    RAMP = "RAMP"
    NOISE = "PRNoise"

    @property
    def short_form(self) -> str:
        return "".join(character for character in self.value if not character.islower())

    @classmethod
    def from_reply(cls, reply: str) -> "Shape | str":
        text = reply.strip().strip('"').upper()
        for shape in cls:
            if text in (shape.value.upper(), shape.short_form):
                return shape
        return reply.strip()


@dataclass(frozen=True)
class GeneratorChannelSettings:
    shape: str
    frequency_hz: float
    amplitude_vpp: float
    offset_v: float
    load_ohms: float
    output_on: bool


@dataclass(frozen=True)
class GeneratorSettings:
    identity: Identity
    channels: dict[int, GeneratorChannelSettings]

    def as_dict(self) -> dict[str, object]:
        settings: dict[str, object] = {"instrument": self.identity.raw}
        for channel, values in self.channels.items():
            settings[f"ch{channel}"] = asdict(values)
        return settings


class Generator(Instrument):
    model = "AFG1062"
    vendor_id = 0x0699
    description = "function generator"
    channels = (1, 2)
    error_query = "SYSTem:ERRor?"
    policy = CommandPolicy.from_notation(
        [
            "*IDN?",
            "SYSTem:ERRor[:NEXT]?",
            "[SOURce#]:FUNCtion[:SHAPe]?",
            "[SOURce#]:FREQuency[:CW|:FIXed]?",
            "[SOURce#]:VOLTage[:LEVel][:IMMediate][:AMPLitude]?",
            "[SOURce#]:VOLTage[:LEVel][:IMMediate]:OFFSet?",
            "OUTPut#:IMPedance?",
            "OUTPut#[:STATe]?",
        ]
    )

    @classmethod
    def simulated_transport(cls) -> Transport:
        from labrador.instruments.simulated import SimulatedGeneratorDevice

        return SimulatedGeneratorDevice()

    def shape(self, channel: int) -> Shape | str:
        return Shape.from_reply(self.query(f"SOURce{self._channel(channel)}:FUNCtion:SHAPe?"))

    def set_shape(self, channel: int, shape: Shape) -> None:
        self.write(f"SOURce{self._channel(channel)}:FUNCtion:SHAPe {Shape(shape).value}")

    def frequency(self, channel: int) -> float:
        return self.query_float(f"SOURce{self._channel(channel)}:FREQuency:FIXed?")

    def set_frequency(self, channel: int, hz: float) -> None:
        if not hz > 0:
            raise ValueError(f"Frequency must be positive, got {hz}")
        self.write(f"SOURce{self._channel(channel)}:FREQuency:FIXed {hz:.12G}")

    def amplitude(self, channel: int) -> float:
        return self.query_float(
            f"SOURce{self._channel(channel)}:VOLTage:LEVel:IMMediate:AMPLitude?"
        )

    def set_amplitude(self, channel: int, vpp: float) -> None:
        if not vpp > 0:
            raise ValueError(f"Amplitude must be positive, got {vpp}")
        self.write(f"SOURce{self._channel(channel)}:VOLTage:LEVel:IMMediate:AMPLitude {vpp:.6G}VPP")

    def offset(self, channel: int) -> float:
        return self.query_float(f"SOURce{self._channel(channel)}:VOLTage:LEVel:IMMediate:OFFSet?")

    def set_offset(self, channel: int, volts: float) -> None:
        self.write(f"SOURce{self._channel(channel)}:VOLTage:LEVel:IMMediate:OFFSet {volts:.6G}")

    def load(self, channel: int) -> float:
        ohms = self.query_float(f"OUTPut{self._channel(channel)}:IMPedance?")
        return math.inf if ohms > INFINITE_LOAD_THRESHOLD else ohms

    def set_load(self, channel: int, ohms: float) -> None:
        if math.isinf(ohms) and ohms > 0:
            value = "INFinity"
        elif MIN_LOAD_OHMS <= ohms <= MAX_LOAD_OHMS:
            value = f"{ohms:.6G}"
        else:
            raise ValueError(
                f"Load must be between {MIN_LOAD_OHMS:g} and {MAX_LOAD_OHMS:g} ohms or math.inf, "
                f"got {ohms}"
            )
        self.write(f"OUTPut{self._channel(channel)}:IMPedance {value}")

    def output(self, channel: int) -> bool:
        return self.query_int(f"OUTPut{self._channel(channel)}:STATe?") != 0

    def set_output(self, channel: int, on: bool) -> None:
        self.write(f"OUTPut{self._channel(channel)}:STATe {'ON' if on else 'OFF'}")

    def align_phase(self) -> None:
        self.write("SOURce1:PHASe:INITiate")

    def configure(
        self,
        channel: int,
        *,
        shape: Shape | None = None,
        frequency: float | None = None,
        amplitude: float | None = None,
        offset: float | None = None,
        load: float | None = None,
        output: bool | None = None,
    ) -> None:
        if output is False:
            self.set_output(channel, False)
        if load is not None:
            self.set_load(channel, load)
        if shape is not None:
            self.set_shape(channel, shape)
        if frequency is not None:
            self.set_frequency(channel, frequency)
        if amplitude is not None:
            self.set_amplitude(channel, amplitude)
        if offset is not None:
            self.set_offset(channel, offset)
        if output is True:
            self.set_output(channel, True)

    def channel_settings(self, channel: int) -> GeneratorChannelSettings:
        shape = self.shape(channel)
        return GeneratorChannelSettings(
            shape=shape.short_form if isinstance(shape, Shape) else shape,
            frequency_hz=self.frequency(channel),
            amplitude_vpp=self.amplitude(channel),
            offset_v=self.offset(channel),
            load_ohms=self.load(channel),
            output_on=self.output(channel),
        )

    def settings(self) -> GeneratorSettings:
        return GeneratorSettings(
            self.identity, {channel: self.channel_settings(channel) for channel in self.channels}
        )

    def _channel(self, channel: int) -> int:
        if channel not in self.channels:
            raise ValueError(f"The {self.description} has channels {self.channels}, not {channel}")
        return channel


def read_generator_settings(
    address: str | None = None,
    *,
    resource_manager: pyvisa.ResourceManager | None = None,
    simulated: bool = False,
    transport: Transport | None = None,
    timeout_s: float = DEFAULT_TIMEOUT_S,
) -> GeneratorSettings:
    if transport is not None:
        generator = Generator(transport, mode=Mode.OBSERVE, address=address or "")
    else:
        generator = Generator.open(
            address,
            mode=Mode.OBSERVE,
            resource_manager=resource_manager,
            simulated=simulated,
            timeout_s=timeout_s,
        )
    try:
        return generator.settings()
    finally:
        generator.close()
