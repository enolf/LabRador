import logging
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar, Self, TypeVar, cast

import pyvisa
from pyvisa import constants
from pyvisa.resources import MessageBasedResource

from labrador.errors import (
    InstrumentCommandError,
    InstrumentNotFoundError,
    InstrumentNotRespondingError,
    ObserveModeError,
    VisaNotFoundError,
)
from labrador.instruments.scpi import CommandPolicy
from labrador.instruments.transport import Transport, TransportError, VisaTransport

logger = logging.getLogger("labrador.instruments")

DEFAULT_TIMEOUT_S = 5.0
MAX_ERROR_QUEUE_READS = 64
SIMULATED_ADDRESS = "SIMULATED"

_NOT_FOUND_CODES = {
    constants.StatusCode.error_resource_not_found,
    constants.StatusCode.error_resource_busy,
    constants.StatusCode.error_resource_locked,
    constants.StatusCode.error_invalid_resource_name,
}

Result = TypeVar("Result")


class Mode(StrEnum):
    OBSERVE = "observe"
    CONTROL = "control"


@dataclass(frozen=True)
class Identity:
    manufacturer: str
    model: str
    serial: str
    firmware: str
    raw: str

    @classmethod
    def parse(cls, reply: str) -> "Identity":
        fields = [field.strip() for field in reply.split(",", 3)]
        fields += [""] * (4 - len(fields))
        return cls(fields[0], fields[1], fields[2], fields[3], reply.strip())


def open_resource_manager() -> pyvisa.ResourceManager:
    try:
        return pyvisa.ResourceManager()
    except (ValueError, OSError) as error:
        raise VisaNotFoundError(str(error)) from error


def list_resources(resource_manager: pyvisa.ResourceManager) -> tuple[str, ...]:
    try:
        return tuple(resource_manager.list_resources())
    except pyvisa.VisaIOError as error:
        if error.error_code == constants.StatusCode.error_resource_not_found:
            return ()
        raise


def usb_vendor_id(address: str) -> int | None:
    parts = address.split("::")
    if len(parts) < 3 or not parts[0].upper().startswith("USB"):
        return None
    text = parts[1].strip()
    try:
        return int(text, 16) if text.lower().startswith("0x") else int(text)
    except ValueError:
        return None


def parse_error_reply(reply: str) -> tuple[int, str]:
    code_text, _, message = reply.partition(",")
    return int(code_text.strip()), message.strip().strip('"')


class Instrument:
    model: ClassVar[str]
    vendor_id: ClassVar[int]
    description: ClassVar[str]
    policy: ClassVar[CommandPolicy]
    channels: ClassVar[tuple[int, ...]] = ()
    error_query: ClassVar[str] = "SYSTem:ERRor?"

    def __init__(
        self,
        transport: Transport,
        *,
        mode: Mode = Mode.OBSERVE,
        check_errors: bool = True,
        address: str = "",
    ) -> None:
        self._transport = transport
        self._mode = Mode(mode)
        self._lock = threading.RLock()
        self._closed = False
        self.check_errors = check_errors
        self.address = address
        self.identity = Identity.parse(self.query("*IDN?"))
        if self.identity.model.upper() != self.model.upper():
            raise InstrumentNotFoundError(
                self.description,
                f"{address or 'the instrument'} identifies as {self.identity.raw!r}, "
                f"not a {self.model}",
            )
        discarded = self.read_errors()
        if discarded:
            logger.info("%s: discarded old errors %s", self.description, discarded)

    def __repr__(self) -> str:
        return f"{type(self).__name__}({self.address!r}, mode={self._mode.value!r})"

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exception_info: object) -> None:
        self.close()

    @property
    def mode(self) -> Mode:
        return self._mode

    @mode.setter
    def mode(self, value: Mode) -> None:
        new_mode = Mode(value)
        if new_mode is not self._mode:
            logger.info("%s: switching from %s to %s mode", self.description, self._mode, new_mode)
        self._mode = new_mode

    @property
    def closed(self) -> bool:
        return self._closed

    @classmethod
    def open(
        cls,
        address: str | None = None,
        *,
        mode: Mode = Mode.OBSERVE,
        timeout_s: float = DEFAULT_TIMEOUT_S,
        resource_manager: pyvisa.ResourceManager | None = None,
        simulated: bool = False,
        check_errors: bool = True,
    ) -> Self:
        if simulated:
            return cls(
                cls.simulated_transport(),
                mode=mode,
                check_errors=check_errors,
                address=SIMULATED_ADDRESS,
            )
        manager = resource_manager or open_resource_manager()
        if address:
            return cls._open_address(manager, address, mode, timeout_s, check_errors)
        resources = list_resources(manager)
        candidates = [found for found in resources if usb_vendor_id(found) == cls.vendor_id]
        if not candidates:
            seen = ", ".join(resources) if resources else "none"
            raise InstrumentNotFoundError(
                cls.description,
                f"no USB instrument with vendor ID 0x{cls.vendor_id:04X}; resources seen: {seen}",
            )
        failures: list[InstrumentNotFoundError | InstrumentNotRespondingError] = []
        for candidate in candidates:
            try:
                return cls._open_address(manager, candidate, mode, timeout_s, check_errors)
            except (InstrumentNotFoundError, InstrumentNotRespondingError) as failure:
                failures.append(failure)
        for failure in failures:
            if isinstance(failure, InstrumentNotRespondingError):
                raise failure
        raise InstrumentNotFoundError(
            cls.description, "; ".join(failure.detail for failure in failures)
        )

    @classmethod
    def find(cls, resource_manager: pyvisa.ResourceManager | None = None) -> list[str]:
        manager = resource_manager or open_resource_manager()
        confirmed = []
        for candidate in list_resources(manager):
            if usb_vendor_id(candidate) != cls.vendor_id:
                continue
            try:
                instrument = cls._open_address(
                    manager, candidate, Mode.OBSERVE, DEFAULT_TIMEOUT_S, True
                )
            except (InstrumentNotFoundError, InstrumentNotRespondingError):
                continue
            instrument.close()
            confirmed.append(candidate)
        return confirmed

    @classmethod
    def simulated_transport(cls) -> Transport:
        raise NotImplementedError(f"{cls.__name__} has no simulator")

    @classmethod
    def _open_address(
        cls,
        manager: pyvisa.ResourceManager,
        address: str,
        mode: Mode,
        timeout_s: float,
        check_errors: bool,
    ) -> Self:
        try:
            resource = manager.open_resource(address)
        except pyvisa.VisaIOError as error:
            if error.error_code in _NOT_FOUND_CODES:
                raise InstrumentNotFoundError(
                    cls.description, f"{address}: {error.description}"
                ) from error
            raise InstrumentNotRespondingError(cls.description, address, str(error)) from error
        except ValueError as error:
            raise InstrumentNotFoundError(cls.description, f"{address}: {error}") from error
        if not hasattr(resource, "query_binary_values"):
            resource.close()
            raise InstrumentNotFoundError(
                cls.description, f"{address} is not a message-based instrument"
            )
        transport = VisaTransport(cast(MessageBasedResource, resource), timeout_s)
        try:
            return cls(transport, mode=mode, check_errors=check_errors, address=address)
        except BaseException:
            _close_quietly(transport)
            raise

    def write(self, command: str) -> None:
        self._ensure_allowed(command)
        with self._lock:
            logger.debug("%s <- %s", self.description, command)
            self._io(lambda: self._transport.write(command))
            if self.check_errors:
                self._raise_for_errors(command)

    def query(self, command: str, *, timeout_s: float | None = None) -> str:
        self._ensure_allowed(command)
        with self._lock, self._timeout(timeout_s):
            reply = self._io(lambda: self._transport.query(command))
            logger.debug("%s <- %s -> %s", self.description, command, reply)
            return reply

    def query_float(self, command: str) -> float:
        return float(self.query(command))

    def query_int(self, command: str) -> int:
        return int(float(self.query(command)))

    def query_block(self, command: str, *, timeout_s: float | None = None) -> bytes:
        self._ensure_allowed(command)
        with self._lock, self._timeout(timeout_s):
            block = self._io(lambda: self._transport.query_block(command))
            logger.debug("%s <- %s -> %d bytes", self.description, command, len(block))
            return block

    def read_errors(self) -> list[tuple[int, str]]:
        errors = []
        with self._lock:
            for _ in range(MAX_ERROR_QUEUE_READS):
                reply = self.query(self.error_query)
                try:
                    code, message = parse_error_reply(reply)
                except ValueError as error:
                    raise InstrumentNotRespondingError(
                        self.description,
                        self.address,
                        f"unexpected reply to {self.error_query}: {reply!r}",
                    ) from error
                if code == 0:
                    break
                errors.append((code, message))
        return errors

    def go_to_local(self) -> bool:
        with self._lock:
            try:
                supported = self._transport.go_to_local()
            except Exception as error:
                logger.warning("%s: go-to-local failed: %s", self.description, error)
                return False
        if not supported:
            logger.info("%s: go-to-local is not supported", self.description)
        return supported

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with self._lock:
            self.go_to_local()
            _close_quietly(self._transport)

    def _ensure_allowed(self, command: str) -> None:
        if self._closed:
            raise InstrumentNotRespondingError(
                self.description, self.address, "the connection has been closed"
            )
        reason = self.policy.refusal(command, observe=self._mode is Mode.OBSERVE)
        if reason is not None:
            raise ObserveModeError(command, reason)

    def _channel(self, channel: int) -> int:
        if channel not in self.channels:
            raise ValueError(f"The {self.description} has channels {self.channels}, not {channel}")
        return channel

    def _raise_for_errors(self, command: str) -> None:
        errors = self.read_errors()
        if errors:
            code, message = errors[0]
            raise InstrumentCommandError(self.description, command, code, message)

    def _io(self, action: Callable[[], Result]) -> Result:
        try:
            return action()
        except TransportError as error:
            raise InstrumentNotRespondingError(
                self.description, self.address, str(error)
            ) from error

    @contextmanager
    def _timeout(self, timeout_s: float | None) -> Iterator[None]:
        if timeout_s is None:
            yield
            return
        previous = self._transport.timeout_s
        self._transport.timeout_s = timeout_s
        try:
            yield
        finally:
            self._transport.timeout_s = previous


def _close_quietly(transport: Transport) -> None:
    try:
        transport.close()
    except Exception as error:
        logger.warning("closing the connection failed: %s", error)
