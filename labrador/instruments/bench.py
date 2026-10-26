import logging
from dataclasses import dataclass
from typing import Self

import pyvisa

from labrador.errors import (
    InstrumentNotConnectedError,
    InstrumentNotFoundError,
    InstrumentNotRespondingError,
    ObserveModeError,
)
from labrador.instruments.base import (
    DEFAULT_TIMEOUT_S,
    SIMULATED_ADDRESS,
    Mode,
    open_resource_manager,
)
from labrador.instruments.generator import Generator, GeneratorSettings, read_generator_settings
from labrador.instruments.scope import Scope
from labrador.instruments.simulated import (
    SignalPath,
    SimulatedGeneratorDevice,
    SimulatedScopeDevice,
    generator_signal,
)

logger = logging.getLogger("labrador.instruments")

CONNECTED = "connected"
NOT_REQUESTED = "not requested"
NOT_FOUND = "not found"
NOT_RESPONDING = "not responding"
OBSERVE_GENERATOR_METADATA = "queried briefly for settings (observe mode)"
OBSERVE_GENERATOR_UNUSED = "not used (observe mode)"


@dataclass(frozen=True)
class Simulation:
    scope_device: SimulatedScopeDevice
    generator_device: SimulatedGeneratorDevice


class Bench:
    def __init__(
        self,
        *,
        mode: Mode,
        scope: Scope | None,
        generator: Generator | None,
        failures: dict[str, InstrumentNotFoundError | InstrumentNotRespondingError],
        requested: frozenset[str],
        generator_metadata: bool = True,
        generator_address: str | None = None,
        resource_manager: pyvisa.ResourceManager | None = None,
        simulation: Simulation | None = None,
    ) -> None:
        self.mode = Mode(mode)
        self.generator_metadata = generator_metadata
        self.simulation = simulation
        self._scope = scope
        self._generator = generator
        self._failures = failures
        self._requested = requested
        self._generator_address = generator_address
        self._resource_manager = resource_manager

    @classmethod
    def connect(
        cls,
        *,
        scope: bool = True,
        generator: bool = False,
        mode: Mode = Mode.OBSERVE,
        scope_address: str | None = None,
        generator_address: str | None = None,
        simulated: bool = False,
        generator_metadata: bool = True,
        resource_manager: pyvisa.ResourceManager | None = None,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        signal_paths: dict[int, SignalPath] | None = None,
    ) -> Self:
        mode = Mode(mode)
        hold_generator = generator and mode is Mode.CONTROL
        requested = frozenset(
            name for name, wanted in (("scope", scope), ("generator", generator)) if wanted
        )
        if simulated:
            return cls._connect_simulated(
                scope, hold_generator, mode, requested, generator_metadata, signal_paths
            )
        manager = None
        if scope or hold_generator or generator_metadata:
            manager = resource_manager or open_resource_manager()
        failures: dict[str, InstrumentNotFoundError | InstrumentNotRespondingError] = {}
        opened_scope = None
        opened_generator = None
        if scope:
            try:
                opened_scope = Scope.open(
                    scope_address or None,
                    mode=mode,
                    resource_manager=manager,
                    timeout_s=timeout_s,
                )
            except (InstrumentNotFoundError, InstrumentNotRespondingError) as failure:
                failures["scope"] = failure
        if hold_generator:
            try:
                opened_generator = Generator.open(
                    generator_address or None,
                    mode=mode,
                    resource_manager=manager,
                    timeout_s=timeout_s,
                )
            except (InstrumentNotFoundError, InstrumentNotRespondingError) as failure:
                failures["generator"] = failure
        return cls(
            mode=mode,
            scope=opened_scope,
            generator=opened_generator,
            failures=failures,
            requested=requested,
            generator_metadata=generator_metadata,
            generator_address=generator_address or None,
            resource_manager=manager,
        )

    @classmethod
    def _connect_simulated(
        cls,
        scope: bool,
        hold_generator: bool,
        mode: Mode,
        requested: frozenset[str],
        generator_metadata: bool,
        signal_paths: dict[int, SignalPath] | None,
    ) -> Self:
        generator_device = SimulatedGeneratorDevice()
        scope_device = SimulatedScopeDevice(generator_signal(generator_device, signal_paths))
        return cls(
            mode=mode,
            scope=Scope(scope_device, mode=mode, address=SIMULATED_ADDRESS) if scope else None,
            generator=(
                Generator(generator_device, mode=mode, address=SIMULATED_ADDRESS)
                if hold_generator
                else None
            ),
            failures={},
            requested=requested,
            generator_metadata=generator_metadata,
            simulation=Simulation(scope_device, generator_device),
        )

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exception_info: object) -> None:
        self.close()

    @property
    def scope(self) -> Scope:
        if self._scope is None:
            raise self._not_connected("scope", Scope.description)
        return self._scope

    @property
    def generator(self) -> Generator:
        if self.mode is Mode.OBSERVE:
            raise ObserveModeError(
                "",
                "In observe mode the function generator is not held open, so that its front "
                "panel stays usable. Use generator_settings() to read its settings, or connect "
                "in control mode to change them.",
            )
        if self._generator is None:
            raise self._not_connected("generator", Generator.description)
        return self._generator

    def has_scope(self) -> bool:
        return self._scope is not None

    def has_generator(self) -> bool:
        return self._generator is not None

    def generator_settings(self) -> GeneratorSettings | None:
        if self._generator is not None:
            return self._generator.settings()
        if self.mode is not Mode.OBSERVE or not self.generator_metadata:
            return None
        try:
            if self.simulation is not None:
                return read_generator_settings(
                    SIMULATED_ADDRESS, transport=self.simulation.generator_device.connect()
                )
            return read_generator_settings(
                self._generator_address, resource_manager=self._resource_manager
            )
        except (InstrumentNotFoundError, InstrumentNotRespondingError) as failure:
            logger.info("generator settings not recorded: %s", failure)
            return None

    def status(self) -> dict[str, str]:
        return {
            "scope": self._status("scope", self._scope),
            "generator": self._generator_status(),
        }

    def close(self) -> None:
        for instrument in (self._scope, self._generator):
            if instrument is not None:
                instrument.close()

    def _status(self, name: str, instrument: Scope | Generator | None) -> str:
        if instrument is not None:
            return CONNECTED
        failure = self._failures.get(name)
        if isinstance(failure, InstrumentNotRespondingError):
            return NOT_RESPONDING
        if failure is not None:
            return NOT_FOUND
        return NOT_REQUESTED

    def _generator_status(self) -> str:
        if self.mode is Mode.OBSERVE:
            return (
                OBSERVE_GENERATOR_METADATA if self.generator_metadata else OBSERVE_GENERATOR_UNUSED
            )
        return self._status("generator", self._generator)

    def _not_connected(self, name: str, description: str) -> InstrumentNotConnectedError:
        failure = self._failures.get(name)
        if failure is not None:
            error = InstrumentNotConnectedError(description, str(failure))
            error.__cause__ = failure
            return error
        if name not in self._requested:
            return InstrumentNotConnectedError(
                description, "it was not requested when connecting the bench"
            )
        return InstrumentNotConnectedError(description)
