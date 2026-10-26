from labrador.instruments.base import Mode
from labrador.instruments.generator import Generator
from labrador.instruments.scope import PointsMode, Scope, WaveformFormat
from labrador.instruments.scpi import ScpiPattern, parse_message
from labrador.instruments.simulated import SimulatedGeneratorDevice, SimulatedScopeDevice

TRANSFER_SETTINGS = [
    ScpiPattern(notation)
    for notation in (
        ":WAVeform:SOURce",
        ":WAVeform:FORMat",
        ":WAVeform:BYTeorder",
        ":WAVeform:UNSigned",
        ":WAVeform:POINts",
        ":WAVeform:POINts:MODE",
    )
]


def only_queries_and_transfer_settings(messages):
    for message in messages:
        for command in parse_message(message):
            if command.is_query:
                continue
            assert any(pattern.matches(command) for pattern in TRANSFER_SETTINGS), message


def test_observe_scope_session_sends_only_queries_and_transfer_settings():
    device = SimulatedScopeDevice()
    scope = Scope(device)
    assert scope.mode is Mode.OBSERVE
    scope.displayed_channels()
    scope.settings()
    scope.read_preamble(1)
    scope.read_waveforms()
    scope.read_waveform(2, data_format=WaveformFormat.WORD, points=100)
    scope.read_waveform(1, points_mode=PointsMode.MAXIMUM)
    scope.run_state()
    scope.trigger_occurred()
    scope.screenshot()
    device.press_stop()
    scope.read_waveform(1, points_mode=PointsMode.RAW)
    scope.close()
    only_queries_and_transfer_settings(device.sent)
    assert device.error_queue == []
    assert device.installed_measurements == []
    assert device.locked is False
    assert device.go_to_local_calls == 1


def test_observe_scope_session_leaves_every_setting_unchanged():
    device = SimulatedScopeDevice()
    scope = Scope(device)
    before = scope.settings()
    scope.read_waveforms(data_format=WaveformFormat.WORD)
    scope.screenshot()
    assert scope.settings() == before
    assert device.running


def test_observe_generator_session_sends_only_queries():
    device = SimulatedGeneratorDevice()
    generator = Generator(device)
    generator.settings()
    generator.close()
    only_queries_and_transfer_settings(device.sent)
    assert device.error_queue == []
    assert device.go_to_local_calls == 1
