import pytest

from labrador.instruments.scpi import (
    ALWAYS_DENIED_REASON,
    OBSERVE_REASON,
    CommandPolicy,
    ScpiPattern,
    parse_message,
)


def command(text: str):
    (parsed,) = parse_message(text)
    return parsed


@pytest.mark.parametrize(
    ("notation", "message", "expected"),
    [
        (":WAVeform:SOURce", ":WAVeform:SOURce CHANnel1", True),
        (":WAVeform:SOURce", ":WAV:SOUR CHAN1", True),
        (":WAVeform:SOURce", ":wav:sour chan1", True),
        (":WAVeform:SOURce", "WAV:SOUR CHAN1", True),
        (":WAVeform:SOURce", ":WAVE:SOUR CHAN1", False),
        (":WAVeform:SOURce", ":WAV:SOUR? ", False),
        (":WAVeform:SOURce?", ":WAV:SOUR?", True),
        (":WAVeform:POINts", ":WAV:POIN:MODE RAW", False),
        (":WAVeform:POINts:MODE", ":WAV:POIN:MODE RAW", True),
        (":CHANnel#:SCALe?", ":CHAN1:SCAL?", True),
        (":CHANnel#:SCALe?", ":CHANNEL2:SCALE?", True),
        (":CHANnel#:SCALe?", ":CHAN:SCAL?", True),
        (":CHANnel#:SCALe?", ":CHAN1:SCAL 1", False),
        (":WAVeform:SOURce", ":WAV1:SOUR CHAN1", False),
        (":TRIGger[:EDGE]:SOURce?", ":TRIG:SOUR?", True),
        (":TRIGger[:EDGE]:SOURce?", ":TRIG:EDGE:SOUR?", True),
        (":TRIGger[:EDGE]:SOURce?", ":TRIG:EDGE?", False),
        ("[SOURce#]:FREQuency[:CW|:FIXed]?", "FREQ?", True),
        ("[SOURce#]:FREQuency[:CW|:FIXed]?", "SOURce2:FREQuency:FIXed?", True),
        ("[SOURce#]:FREQuency[:CW|:FIXed]?", "SOUR1:FREQ:CW?", True),
        ("[SOURce#]:FREQuency[:CW|:FIXed]?", "SOUR1:FREQ:STAR?", False),
        ("OUTPut#[:STATe]?", "OUTP2?", True),
        ("OUTPut#[:STATe]?", "OUTPut1:STATe?", True),
        ("*IDN?", "*idn?", True),
        ("*IDN?", "*IDN", False),
        ("*IDN?", ":IDN?", False),
        ("*RST", "*RST", True),
    ],
)
def test_pattern_matching(notation, message, expected):
    assert ScpiPattern(notation).matches(command(message)) is expected


def test_suffixes_report_numeric_suffix_per_suffix_node():
    pattern = ScpiPattern("[SOURce#]:VOLTage[:LEVel][:IMMediate]:OFFSet")
    assert pattern.suffixes(command("SOUR2:VOLT:LEV:IMM:OFFS 0.5")) == [2]
    assert pattern.suffixes(command("VOLT:OFFS 0.5")) == [None]
    assert ScpiPattern(":CHANnel#:DISPlay?").suffixes(command(":CHAN2:DISP?")) == [2]


def test_parse_message_splits_arguments_and_queries():
    parsed = command(":DISPlay:DATA? PNG, COLor")
    assert parsed.nodes == ("DISPLAY", "DATA")
    assert parsed.is_query
    assert parsed.arguments == "PNG, COLor"


def test_parse_message_resolves_relative_headers_in_compound_messages():
    first, second, third = parse_message(":WAV:SOUR CHAN1;FORM BYTE;*CLS")
    assert first.nodes == ("WAV", "SOUR")
    assert second.nodes == ("WAV", "FORM")
    assert third.is_common


def test_parse_message_ignores_semicolons_inside_quotes():
    parsed = parse_message(':SYST:TEXT "a;b";:WAV:SOUR CHAN1')
    assert [item.nodes for item in parsed] == [("SYST", "TEXT"), ("WAV", "SOUR")]


@pytest.mark.parametrize("message", ["", ";", ":WAV::SOUR CHAN1", ":WAV:SOUR CHAN1;"])
def test_parse_message_rejects_malformed_messages(message):
    with pytest.raises(ValueError):
        parse_message(message)


POLICY = CommandPolicy.from_notation(
    ["*IDN?", ":SYSTem:ERRor?", ":SYSTem:LOCK?", ":WAVeform:SOURce", ":WAVeform:DATA?"],
    [":SYSTem:LOCK"],
)


@pytest.mark.parametrize(
    "message",
    ["*IDN?", ":SYST:ERR?", ":SYSTem:LOCK?", ":WAV:SOUR CHAN2", ":WAV:SOUR CHAN1;DATA?"],
)
def test_observe_policy_allows_listed_commands(message):
    assert POLICY.refusal(message, observe=True) is None


@pytest.mark.parametrize(
    "message",
    [
        "*RST",
        ":CHAN1:SCAL 1",
        ":CHAN1:SCAL?",
        ":MEAS:FREQ? CHAN1",
        ":WAV:SOUR CHAN1;:AUT",
        ":WAV:SOUR CHAN1;FORM BYTE",
        "*TST?",
    ],
)
def test_observe_policy_refuses_everything_else(message):
    assert POLICY.refusal(message, observe=True) == OBSERVE_REASON


@pytest.mark.parametrize(
    "message",
    [":SYSTem:LOCK ON", ":syst:lock 1", ":SYST:ERR?;LOCK 1", ":WAV:SOUR CHAN1;:SYST:LOCK 1"],
)
def test_lock_commands_are_refused_in_both_modes(message):
    assert POLICY.refusal(message, observe=False) == ALWAYS_DENIED_REASON
    assert POLICY.refusal(message, observe=True) == ALWAYS_DENIED_REASON


def test_control_mode_allows_unlisted_commands():
    assert POLICY.refusal(":CHAN1:SCAL 1;*RST", observe=False) is None


def test_malformed_message_is_refused_with_reason():
    assert POLICY.refusal(":WAV::SOUR", observe=False) is not None
