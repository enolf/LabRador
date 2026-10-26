# LabRador

Instrument control and data logging for a Keysight EDUX1052A oscilloscope and a
Tektronix AFG1062 function generator. Python core, CLI, server, MATLAB front end.
Users are JCU engineering students. Not affiliated with the EspoTek Labrador.
Full design: docs/design.md. Read the relevant sections before planning a phase.

## Architecture
- labrador/instruments: Instrument base (pyvisa), Scope, Generator, simulated versions
- labrador/measurements: Capture type, capture, sweep, logging, capture-on-Single
- labrador/cli.py: the `labrador` command (also built as a PyInstaller .exe)
- labrador/server.py: `labrador serve`, JSON-lines over 127.0.0.1 TCP
- matlab/+labrador: wrapper calling the .exe (Option B) and a tcpclient client (Option C)
- The Python library API is public: keep it stable and documented.

## Rules
- Observe mode is the default. In observe mode only queries and :WAVeform transfer
  settings may be sent. Never send *RST, :AUToscale, :DIGitize or setting commands.
- Never send a front-panel lock command. Release instruments to local on close.
- Measurements are calculated in Python from waveform data, not queried, in observe mode.
- Check every SCPI command against the manuals before using it. Never guess a command.

## Manuals (local only, not in Git)
- docs/manuals/keysight-1000x-programmers-guide.pdf: EDUX1052A, InfiniiVision 1200 X-Series
  and EDUX1052A/G Programmer's Guide (file name kept from an older download)
- docs/manuals/tektronix-afg1000-programmer-manual.pdf: AFG1062, AFG1000 Series
- A .txt copy of each (pdftotext) sits beside it; search the .txt for a command, then read
  only the relevant pages of the PDF.
- If a manual is missing, stop and ask the developer instead of guessing.
- The .mat field names in docs/design.md Section 7 are frozen; changes need a version bump.
- CLI exit codes are fixed once released; the MATLAB wrapper depends on them.

## Code style
- No comments in code. Clear names instead.
- Ruff formatting and linting must pass.
- Type hints on public functions.

## Testing
- pytest against the simulated instruments must pass before every commit.
- Hardware behaviour is verified by the developer with scripts/hardware_check.py.

## Environment
- Windows, Python 3.12 venv, R&S VISA.
- Run Python tools through the venv: .venv\Scripts\python.exe -m pytest / -m ruff / -m pip.