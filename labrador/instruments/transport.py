from collections.abc import Iterator
from contextlib import contextmanager
from typing import Protocol

import pyvisa
from pyvisa import constants
from pyvisa.resources import MessageBasedResource

TERMINATION = "\n"


class TransportError(Exception):
    def __init__(self, message: str, *, timed_out: bool = False) -> None:
        self.timed_out = timed_out
        super().__init__(message)


class Transport(Protocol):
    @property
    def timeout_s(self) -> float: ...

    @timeout_s.setter
    def timeout_s(self, value: float) -> None: ...

    def write(self, message: str) -> None: ...

    def query(self, message: str) -> str: ...

    def query_block(self, message: str) -> bytes: ...

    def go_to_local(self) -> bool: ...

    def close(self) -> None: ...


class VisaTransport:
    def __init__(
        self, resource: MessageBasedResource, timeout_s: float, encoding: str = "ascii"
    ) -> None:
        self._resource = resource
        self.encoding = encoding
        resource.read_termination = TERMINATION
        resource.write_termination = TERMINATION
        self.timeout_s = timeout_s

    @property
    def address(self) -> str:
        return str(self._resource.resource_name)

    @property
    def timeout_s(self) -> float:
        return float(self._resource.timeout) / 1000.0

    @timeout_s.setter
    def timeout_s(self, value: float) -> None:
        self._resource.timeout = round(value * 1000)

    def write(self, message: str) -> None:
        with _translated_errors(message):
            self._resource.write(message)

    def query(self, message: str) -> str:
        with _translated_errors(message):
            self._resource.write(message)
            reply = self._resource.read_raw()
        return reply.decode(self.encoding, errors="backslashreplace").strip()

    def query_block(self, message: str) -> bytes:
        with _translated_errors(message):
            return self._resource.query_binary_values(
                message, datatype="B", container=bytes, expect_termination=True
            )

    def go_to_local(self) -> bool:
        try:
            self._resource.control_ren(constants.RENLineOperation.address_gtl)
        except (pyvisa.Error, NotImplementedError, AttributeError):
            return False
        return True

    def close(self) -> None:
        self._resource.close()


@contextmanager
def _translated_errors(message: str) -> Iterator[None]:
    try:
        yield
    except pyvisa.VisaIOError as error:
        timed_out = error.error_code == constants.StatusCode.error_timeout
        raise TransportError(f"{message!r}: {error}", timed_out=timed_out) from error
    except pyvisa.Error as error:
        raise TransportError(f"{message!r}: {error}") from error
