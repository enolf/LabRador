DOCTOR_ADVICE = "Run `labrador doctor` to check the setup."


class LabradorError(Exception):
    pass


class VisaNotFoundError(LabradorError):
    def __init__(self, detail: str = "", download_page: str | None = None) -> None:
        self.detail = detail
        self.download_page = download_page
        where = (
            f"Download it from {download_page}"
            if download_page
            else "Download it from the Rohde & Schwarz website (search for 'R&S VISA')"
        )
        message = (
            "No VISA library was found, so LabRador cannot talk to the instruments. "
            f"Install R&S VISA (it needs administrator rights). {where}, "
            "then restart this program."
        )
        if detail:
            message = f"{message}\nDetails: {detail}"
        super().__init__(message)


class InstrumentNotFoundError(LabradorError):
    def __init__(self, instrument: str, detail: str = "") -> None:
        self.instrument = instrument
        self.detail = detail
        message = (
            f"The {instrument} was not found. Check that it is switched on, that the USB cable "
            "is in the rear USB device port, and that no other program has it open. "
            f"{DOCTOR_ADVICE}"
        )
        if detail:
            message = f"{message}\nDetails: {detail}"
        super().__init__(message)


class InstrumentNotRespondingError(LabradorError):
    def __init__(self, instrument: str, address: str = "", detail: str = "") -> None:
        self.instrument = instrument
        self.address = address
        self.detail = detail
        at = f" at {address}" if address else ""
        message = (
            f"The {instrument}{at} was found but is not responding. Switch it off and on "
            f"again, then reconnect. {DOCTOR_ADVICE}"
        )
        if detail:
            message = f"{message}\nDetails: {detail}"
        super().__init__(message)


class UnexpectedReplyError(LabradorError):
    def __init__(self, instrument: str, command: str, reply: str) -> None:
        self.instrument = instrument
        self.command = command
        self.reply = reply
        super().__init__(
            f"The {instrument} gave an unexpected reply to {command!r}: {reply!r}. "
            "Please report this, with the instrument's firmware version."
        )


class InstrumentNotConnectedError(LabradorError):
    def __init__(self, instrument: str, reason: str = "") -> None:
        self.instrument = instrument
        self.reason = reason
        message = f"This needs the {instrument}, which isn't connected. {DOCTOR_ADVICE}"
        if reason:
            message = f"{message}\nReason: {reason}"
        super().__init__(message)


class ObserveModeError(LabradorError):
    def __init__(self, command: str, reason: str) -> None:
        self.command = command
        self.reason = reason
        super().__init__(f"Refused to send {command!r}: {reason}" if command else reason)


class InstrumentCommandError(LabradorError):
    def __init__(self, instrument: str, command: str, code: int, error_message: str) -> None:
        self.instrument = instrument
        self.command = command
        self.code = code
        self.error_message = error_message
        super().__init__(
            f"The {instrument} reported error {code} ({error_message}) after {command!r}"
        )


class ChannelNotDisplayedError(LabradorError):
    def __init__(self, channel: int) -> None:
        self.channel = channel
        super().__init__(
            f"Channel {channel} is switched off on the oscilloscope, so its waveform cannot be "
            "read. Switch it on with its channel button, or choose another channel."
        )


class WaveformUnavailableError(LabradorError):
    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"The full-memory waveform is not available: {reason}")


class SettingsChangedError(LabradorError):
    def __init__(self, channel: int, attempts: int) -> None:
        self.channel = channel
        self.attempts = attempts
        super().__init__(
            f"The oscilloscope settings changed while channel {channel} was being read "
            f"({attempts} attempts). Leave the controls still and try again."
        )
