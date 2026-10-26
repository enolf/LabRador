from collections.abc import Callable

import pyvisa
from pyvisa import constants

from labrador.instruments.base import Instrument
from labrador.instruments.scpi import CommandPolicy
from labrador.instruments.transport import TransportError

FAKE_IDN = "ACME,FAKE1000,SN42,1.0"


class ScriptedTransport:
    def __init__(
        self,
        replies: dict[str, str | Callable[[], str]] | None = None,
        idn: str = FAKE_IDN,
        errors: list[str] | None = None,
    ) -> None:
        self.replies: dict[str, str | Callable[[], str]] = {"*IDN?": idn}
        self.replies.update(replies or {})
        self.error_queue = list(errors or [])
        self.errors_after_write: dict[str, str] = {}
        self.blocks: dict[str, bytes] = {}
        self.sent: list[str] = []
        self.timeout_s = 5.0
        self.timeout_history: list[float] = []
        self.failing: dict[str, TransportError] = {}
        self.go_to_local_result: bool | Exception = True
        self.calls: list[str] = []

    def write(self, message: str) -> None:
        self._record(message)
        if message in self.errors_after_write:
            self.error_queue.append(self.errors_after_write[message])

    def query(self, message: str) -> str:
        self._record(message)
        self.timeout_history.append(self.timeout_s)
        if message.upper().lstrip(":").startswith("SYST") and message.endswith("?"):
            return self.error_queue.pop(0) if self.error_queue else '0,"No error"'
        reply = self.replies[message]
        return reply() if callable(reply) else reply

    def query_block(self, message: str) -> bytes:
        self._record(message)
        self.timeout_history.append(self.timeout_s)
        return self.blocks[message]

    def go_to_local(self) -> bool:
        self.calls.append("go_to_local")
        if isinstance(self.go_to_local_result, Exception):
            raise self.go_to_local_result
        return self.go_to_local_result

    def close(self) -> None:
        self.calls.append("close")

    def _record(self, message: str) -> None:
        if message in self.failing:
            raise self.failing[message]
        self.sent.append(message)


class FakeInstrument(Instrument):
    model = "FAKE1000"
    vendor_id = 0x1234
    description = "fake instrument"
    policy = CommandPolicy.from_notation(
        ["*IDN?", "SYSTem:ERRor?", ":LEVel?", ":WAVeform:SOURce"],
        [":SYSTem:LOCK"],
    )


class FakeVisaResource:
    def __init__(self, name: str, replies: dict[str, str] | None = None) -> None:
        self.resource_name = name
        self.read_termination: str | None = None
        self.write_termination: str | None = None
        self.timeout = 2000
        self.replies = {"*IDN?": FAKE_IDN, "SYSTem:ERRor?": '+0,"No error"'}
        self.replies.update(replies or {})
        self.written: list[str] = []
        self.binary_calls: list[tuple[str, dict]] = []
        self.block = b""
        self.query_error: Exception | None = None
        self.ren_calls: list[object] = []
        self.ren_error: Exception | None = None
        self.closed = False

    def write(self, message: str) -> None:
        self.written.append(message)

    def query(self, message: str) -> str:
        if self.query_error is not None:
            raise self.query_error
        return self.replies[message] + "\n"

    def query_binary_values(self, message: str, **options: object) -> bytes:
        self.binary_calls.append((message, options))
        return self.block

    def control_ren(self, mode: object) -> None:
        self.ren_calls.append(mode)
        if self.ren_error is not None:
            raise self.ren_error

    def close(self) -> None:
        self.closed = True


class FakeResourceManager:
    def __init__(self, resources: dict[str, FakeVisaResource | Exception]) -> None:
        self.resources = resources
        self.opened: list[str] = []

    def list_resources(self) -> tuple[str, ...]:
        return tuple(self.resources)

    def open_resource(self, address: str) -> FakeVisaResource:
        self.opened.append(address)
        if address not in self.resources:
            raise pyvisa.VisaIOError(constants.StatusCode.error_resource_not_found)
        found = self.resources[address]
        if isinstance(found, Exception):
            raise found
        return found


def visa_timeout() -> pyvisa.VisaIOError:
    return pyvisa.VisaIOError(constants.StatusCode.error_timeout)
