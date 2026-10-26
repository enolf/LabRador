# LabRador — Project Plan and Design Specification

Oct 22, 2026 · @enolf

## 1. Overview

LabRador is a toolkit for controlling and logging data from a Keysight EDUX1052A oscilloscope and a Tektronix AFG1062 function generator, built as a Python core with a MATLAB front end. It is aimed at JCU engineering students who want to capture, sweep and log measurements without writing instrument code themselves.

### Goals

- Let students capture waveforms, run frequency sweeps and log measurements from MATLAB, Python or the command line.
- Never get in the way of a student who prefers to operate the instruments by hand and only wants the data (see Section 3, Operating modes).
- Need as little installation as possible: one VISA driver plus an unzipped folder for non-Python users.
- Be structured well enough to be maintained, extended to more instruments, and handed on.

### Scope

| In scope | Out of scope (for now) |
| --- | --- |
| EDUX1052A oscilloscope (2 channels, USB) | Other instrument models (the driver layer is designed so they can be added later) |
| AFG1062 function generator (2 channels, USB) | LAN/Ethernet connections |
| Waveform capture, frequency sweeps, timed logging | Gap-free continuous streaming (not possible with a benchtop scope; see Section 12) |
| MATLAB, Python and CLI/Excel users | Publishing to PyPI or wide public distribution |
| Windows lab PCs and student laptops | macOS/Linux support (possible later; `pyvisa-py` works well on Linux) |

### Audience

The primary users are fellow JCU students. The repository is public on GitHub, so others may find it, but discoverability is not a goal.

### Name

The project is called **LabRador** (display name). The package, Python import and command are lowercase `labrador`, following Python conventions; a short alias `lbr` may be added. A Labrador icon is planned for the `.exe` and the MATLAB GUI.

The name is shared with the unrelated **EspoTek Labrador**, an open-source USB lab-on-a-board whose software is also launched with the command `labrador`. The README must state that LabRador is not affiliated with it. Python users install from GitHub, not PyPI, so a PyPI name clash does not matter.

### Status

Design is complete; no LabRador code exists yet beyond the two MATLAB prototypes (`ScopeControl.m`, `CableSweep.m`, Section 12). The next step is Phase 0 (Section 9), and the open questions in Section 13 should be resolved during Phases 0 to 3.

## 2. Architecture

Everything is built on one Python library; every other route (CLI, `.exe`, server, MATLAB wrapper, GUI) is a thin layer over it. The library is the product, not an internal detail: Python users call it directly, so its API must be stable and documented.

&#91;embedded content: LabRador architecture · users, entry layers, core, VISA, instruments\]

Read top to bottom: each kind of user enters through its own thin layer, and all of them share the one core library, which alone talks to VISA and the instruments.

### Layers

| Layer | What it is | Who uses it |
| --- | --- | --- |
| Instrument drivers | `Instrument` base class over `pyvisa`; `Scope` (EDUX1052A) and `Generator` (AFG1062) classes; simulated versions of both | Everything above it |
| Measurements | `Capture` data type; single capture, frequency sweep, timed logging, capture-on-Single; export to `.mat`, CSV, `.xlsx` | Library users, CLI, server |
| Python API | `from labrador import Scope, Generator, sweep, ...` | Python users (scripts, Jupyter) |
| CLI | `labrador capture`, `labrador sweep`, `labrador log`, `labrador doctor`, ... | Excel/CLI users, MATLAB wrapper (Option B) |
| Server | `labrador serve`: long-running process holding the instrument connections, JSON-lines over `127.0.0.1` TCP | MATLAB client (Option C) |
| MATLAB wrapper | `+labrador` package: functions calling the `.exe` (Option B) and a client class using `tcpclient` (Option C) | MATLAB users |
| MATLAB GUI | Live view and controls built on the Option C client | MATLAB users |

### How MATLAB talks to the core

Three integration options were considered:

- **Option A: MATLAB calls the Python package in-process** (`py.labrador...`). Rejected as the main route: every user needs a Python version matching their MATLAB release, a Python or driver crash can take MATLAB down, and Python cannot push events to MATLAB.
- **Option B: MATLAB runs the CLI** (`system("labrador sweep ...")`, then `load` the `.mat`). Stateless: each call starts Python, reconnects, works and exits (about one second of overhead), so it suits batch jobs, not live control. Packaged as an `.exe`, it needs no Python on the user's machine. **Built first.**
- **Option C: MATLAB connects to a running server.** The server starts once and keeps the instrument connections, current settings, acquisition threads and frame buffers. MATLAB sends short commands over `tcpclient` (base MATLAB) and each takes milliseconds. Supports live streaming and control. Also needs no Python on the user's machine. **Built second**, once batch features work.

### Option C server responsibilities

- **Start-up:** the MATLAB wrapper checks whether the server is running and launches it if not, invisibly to the user.
- **Crash or close:** server state is lost. The wrapper detects the dropped connection and reconnects; the server sets instruments to a known state on start-up (respecting observe mode, Section 3).
- **Where state lives:** instrument state lives in the server. MATLAB queries it and never keeps its own copy, which could drift.
- **Shutdown:** release the instruments (return to local control, close the VISA session) so the front panel and other tools are free.
- **Single instance:** only one server may own the instruments; a second launch must detect the first and exit cleanly.

### Instrument Control Toolbox is not required

Because Python talks to the instruments, MATLAB never does. All three options use base MATLAB only (Python interface, `system()`/`load()`, `tcpclient`). The Instrument Control Toolbox is only needed for the pure-MATLAB fallback scripts (`visadev`), which JCU students have under their licence anyway. A VISA library is still required either way (Section 6).

### Working with one instrument

LabRador must work with only the scope or only the generator connected; missing an instrument is a normal state, not an error, until a task needs it.

| Task | Scope | Generator |
| --- | --- | --- |
| Capture, logging, capture-on-Single | Required | Not used |
| Set generator waveform, frequency, amplitude, output | Not used | Required |
| Automated sweep, Phase 9 analysis modules | Required | Required |
| Manual-stimulus sweep | Required | Not used (any generator, set by hand) |

- **Connect only what a task needs.** Each command declares its required instruments, and only those are opened.
- **Fail clearly when one is missing.** A task needing an absent instrument stops with a plain message ("this needs the function generator, which isn't connected") plus the `labrador doctor` advice, and a dedicated "required instrument missing" exit code (fixed in Phase 3).
- **Blank addresses are allowed** in the config file.
- **`labrador doctor` reports each instrument separately**, so it is obvious which tasks will work.
- **The server and GUI adapt.** The Option C server holds whichever instruments are present; the MATLAB GUI greys out controls for a missing instrument instead of refusing to start, and ideally picks up an instrument plugged in later without a restart.

### Manual-stimulus sweep

For scope-only setups, LabRador can run a frequency sweep in which the user sets each frequency by hand on any generator. It is slower than the automated sweep, but students without a controllable generator still get a frequency response from the same analysis code.

1. LabRador shows the next frequency in the list (e.g. "Set the generator to 1.00 MHz, then press Enter").
2. The user sets it on the generator and confirms.
3. LabRador captures both channels, with the stimulus on CH1 as the reference, and computes gain and phase by sine fitting at the **measured** frequency (the user's setting may differ slightly from the requested one).
4. It shows the result for that point and moves on; the user may skip, repeat or end early.
5. The result is saved in the same sweep format as the automated sweep (Section 7), marked `stimulus = manual` and holding the measured frequencies.

The scope's mode stays observe-compatible throughout: it only reads waveforms, so the user's front-panel setup is respected. If the signal is clipped or too small for a reliable fit, LabRador says so for that point and suggests a V/div change instead of making it.

## 3. Operating modes

LabRador has two modes, enforced in the driver layer: **observe** (the default, for logging) never changes anything on the instruments, and **control** (for experiments) may. A student who prefers to set up the instruments by hand must be able to log data without the software interfering.

|  | Observe (default) | Control |
| --- | --- | --- |
| Changes instrument settings | Never | Yes |
| Stops or starts acquisition | Never | Yes (`:DIGitize`, Single, Run) |
| Connects to the generator | Read-only and briefly, opt-in (see Generator in observe mode) | When the task needs it |
| Measurements | Calculated in Python from waveform data | Calculated in Python, or queried from the scope |
| Typical use | Logging what the user set up on the front panel | Automated sweeps, scripted experiments |

### Enforcement

- The driver keeps a mode flag. In observe mode, only queries (`...?`) and waveform-transfer commands are allowed; any other command raises an error. Neither the GUI nor a student's script can take over the scope by accident.
- **Exception:** waveform-transfer settings (`:WAVeform:SOURce`, `:WAVeform:FORMat`, `:WAVeform:BYTeorder`, `:WAVeform:POINts`, `:WAVeform:POINts:MODE`, `:WAVeform:UNSigned`) are allowed in observe mode. They affect only how data is sent, not the screen or acquisition.
- The allow-list is explicit and fails closed: only the listed queries and waveform-transfer settings pass. Side-effecting queries such as `*TST?` (self-test) and every `:MEASure` query are refused.
- `:SYSTem:LOCK` (front-panel lock, equivalent to VISA local lockout) is refused in both modes.
- Observe mode lives in the library, so it protects the Python, CLI, server and MATLAB routes equally.

### Generator in observe mode

Observe-mode logs are more useful when each capture records the stimulus, but many instruments switch to a remote state on any USB command, including a query, and may then ignore their front panel until returned to local. Polling the generator must never lock out the student turning its knobs.

- **Read-only:** only `*IDN?`, `SYSTem:ERRor?` and the `SOURce`/`OUTPut` setting queries. Never a setting command.
- **Query briefly, then release:** read the generator's settings once per capture (or once per logging session), then return it to local control and close the connection. It is never held open between captures.
- **On by default, tested:** `scripts/hardware_check.py` (2026-10-26, firmware FV:V1.0.2) showed the AFG1062's front panel keeps responding after it is queried, and after return to local. Because the brief read also returns the generator to local, generator metadata is on by default and can be switched off (`generator_metadata=False`, `--no-gen-metadata` in Phase 3).
- **Labelled honestly:** stored as `generator_settings`, which records what the generator was set to, not what reached the circuit.

### Behaviour that would interfere, and is banned in observe mode

- `:DIGitize` stops the scope after each capture, overriding the user's Run/Stop choice. The original `ScopeControl.m` used it every frame.
- `*RST`, `:AUToscale`, and setting V/div, timebase, trigger or averaging override the user's setup. `CableSweep.m` does all of these, which is correct in control mode only.
- `:MEASure` commands install a measurement in the scope's on-screen list, and a `:MEASure` query given a source changes the current measurement source. Observe mode calculates measurements from the downloaded waveform instead.

### Data capture without interfering

- **Continuous logging while running:** periodically read the latest waveform with `:WAVeform:DATA?` while the scope runs normally. The on-screen record (`NORMal` points mode) is available while running; full-memory `RAW` reads generally need the scope stopped.
- **Capture on Single:** poll the run state with `:OPERegister:CONDition?` (bit 3 = running, bit 5 = trigger armed) and `:TER?` (a trigger occurred since the last read). When the user presses Single on the front panel and the acquisition completes, save it automatically. The user controls everything physically and simply gets files. The EDUX1052A has no `:RSTate?` and no query that tells Single from Run, so the detection rule is settled in Phase 2 from the bit patterns recorded by the hardware check.
- **Only displayed channels can be read:** the `:WAVeform` queries work only when the selected channel is switched on, and observe mode cannot switch it on.
- **Settings changed mid-transfer:** if the user turns a knob between reading the preamble and reading the data, the scaling will not match the data. Re-read the preamble after the data; if it changed, discard the frame.
- **Settings as metadata:** each saved capture records the front-panel settings at the time (V/div, timebase, trigger, coupling, probe), queried read-only. Useful for lab reports.

### Releasing the instruments

On exit, the software returns the instruments to local control and closes the VISA session. The Keysight scope's front panel stays usable while connected over USB unless a lock command is sent, which LabRador never does. The AFG1062 may show a remote indicator and ignore its buttons once it has received commands; check the return-to-local behaviour in the AFG1000 programmer manual.

Only one program can hold an instrument's USB connection at a time. This is not a concern in practice: LabRador is meant to provide every feature students need, so they will not be running BenchVue or other tools alongside it.

## 4. Distribution routes

The same code ships in three forms, and each user picks the one that matches where they analyse data. Every route needs one VISA driver (Section 6); none needs the Instrument Control Toolbox.

| User | Gets | Needs installed | Typical use |
| --- | --- | --- | --- |
| MATLAB user | `.exe` folder + `+labrador` MATLAB package | Base MATLAB, VISA | `data = labrador.sweep(...)`, live GUI (Option C) |
| Excel or non-programmer | `.exe` folder | VISA only | `labrador capture --out run1.csv`, open in Excel |
| Python user | `pip install git+https://github.com/<user>/labrador` | Their own Python, VISA | `from labrador import Scope` in scripts or Jupyter; same `labrador` command |

### Rules that follow

- **Identical CLI everywhere.** The `.exe` and the pip-installed `labrador` command behave the same, so one set of instructions works for both.
- **The library API is public.** Classes and functions must be stable and documented; breaking changes need a version bump and a note in the changelog.
- **Observe mode lives in the library**, so it protects every route (Section 3).

### Excel users

- Keep metadata out of the data columns. Excel handles CSVs with mixed header lines poorly. Either write metadata to a small sidecar file next to the CSV (`run1.csv` + `run1.meta.json`), or offer `.xlsx` export with data on one sheet and metadata on a second.
- If Excel users turn out to be common, consider a simple standalone GUI (PySide6) later. Otherwise the live interface only exists for MATLAB users, and a command line puts off non-programmers.

### Python users

- Installing from GitHub gives them both the library and the `labrador` command.
- They manage their own Python environment, so its dependency issues are theirs by choice; the pinned versions in `requirements.txt` / `pyproject.toml` are the reference.

## 5. Instruments and SCPI reference

Both instruments speak SCPI over USB (USBTMC), so the same commands work from MATLAB, Python or any VISA tool. Every command below was checked against the programmer manuals on 2026-10-26; page numbers refer to those manuals (for the Keysight guide the PDF page equals the printed page; for the AFG1000 manual the PDF page is the printed page + 9). Behaviour the manuals leave open is checked on the hardware with `scripts/hardware_check.py`.

| Instrument | Model | Connection | USB vendor ID | Reference manual |
| --- | --- | --- | --- | --- |
| Oscilloscope | Keysight EDUX1052A (InfiniiVision 1200 X-Series family), 2 ch, 50 MHz | Rear USB device port (USB-B); the front port is a host port for flash drives | `0x2A8D` | InfiniiVision 1200 X-Series and EDUX1052A/G Oscilloscopes Programmer's Guide (Keysight, free PDF) |
| Function generator | Tektronix AFG1062, 2 ch, 60 MHz sine, 50 Ω output | Rear USB device port | `0x0699` | AFG1000 Series Programmer Manual 077-1129-02 (Tektronix, free PDF) |

Addresses look like `USB0::0x2A8D::0x....::<serial>::0::INSTR`. Use `\n` (LF) as the terminator for both.

### Oscilloscope: connection and setup

| Purpose | Command | Page |
| --- | --- | --- |
| Identify | `*IDN?` returns `KEYSIGHT TECHNOLOGIES,<model>,<serial>,X.XX.XX` | 126 |
| Clear status (control mode only: it also empties the error queue) | `*CLS` | 121 |
| Next error (`0` or `+0` = none; 30-deep queue) | `:SYSTem:ERRor?` returns `<code>,"<message>"` | 581 |
| Wait for operations to finish | `*OPC?` returns `1` | 128 |
| Factory reset (control mode only) | `*RST` (probe 10:1, CH2 off, 5 V/div); `:SYSTem:PRESet` is the gentler [Default Setup] | 131, 586 |
| Autoscale (control mode only) | `:AUToscale`; also turns off measurements, cursors and math | 147 |
| Front-panel lock (never sent, in either mode) | `:SYSTem:LOCK ON` is local lockout; `:SYSTem:LOCK?` may be queried | 582 |

### Oscilloscope: channel, timebase, trigger (control mode to set; queries allowed in observe)

| Purpose | Command | Page |
| --- | --- | --- |
| Display on/off | `:CHANnel<n>:DISPlay 1` or `0` | 217 |
| Vertical scale (V/div) | `:CHANnel<n>:SCALe <v>` | 229 |
| Offset (V at screen centre) | `:CHANnel<n>:OFFSet <v>` (out-of-range values are clamped silently) | 221 |
| Coupling | `:CHANnel<n>:COUPling DC` or `AC` | 216 |
| Probe attenuation | `:CHANnel<n>:PROBe 1` or `10` (any ratio 0.1 to 10000) | 222 |
| Time/div | `:TIMebase:SCALe <s>` | 609 |
| Horizontal position | `:TIMebase:POSition <s>` (alias of `:TIMebase:DELay`) | 606 |
| Timebase mode, reference (queried for metadata) | `:TIMebase:MODE?` returns `MAIN`, `WIND`, `XY` or `ROLL`; `:TIMebase:REFerence?` | 605, 608 |
| Trigger type (queried for metadata) | `:TRIGger:MODE?` returns `EDGE`, `GLIT`, `TV`, ... or `NONE` in ROLL/XY | 624 |
| Trigger source | `:TRIGger[:EDGE]:SOURce CHANnel1`, `CHANnel2` or `EXTernal`; the query may return `NONE` | 632 |
| Trigger level | `:TRIGger[:EDGE]:LEVel <v>` | 629 |
| Trigger slope | `:TRIGger[:EDGE]:SLOPe POSitive`, `NEGative`, `EITHer` or `ALTernate` | 631 |
| Trigger sweep ("Mode" on the front panel) | `:TRIGger:SWEep AUTO` or `NORMal` | 626 |
| Averaging | `:ACQuire:TYPE AVERage`, `:ACQuire:COUNt 16` (2 to 65536); back with `:ACQuire:TYPE NORMal`. Read AVERage and HRESolution data as WORD. | 199, 190 |
| Run / Stop / Single | `:RUN`, `:STOP`, `:SINGle` | 170, 174, 172 |
| Run state | `:OPERegister:CONDition?` returns an integer: bit 3 (value 8) = running, bit 5 (value 32) = trigger armed. There is no `:RSTate?`, and no query distinguishes Single from Run. **Observed** (firmware 02.12.2021071625): 40 while running and while Single waits for a trigger, 32 when stopped (after Stop or a completed Single), so bit 5 carries no information on this firmware. | 161 |
| Trigger occurred | `:TER?` returns `1` if a trigger occurred since the last read, then clears. Observed: `0` while Single waits, `1` once it completes, and also `1` after Stop if the scope was triggering while running. | 175 |

### Oscilloscope: waveform transfer (allowed in observe mode)

| Purpose | Command | Page |
| --- | --- | --- |
| Select channel | `:WAVeform:SOURce CHANnel<n>`; the `:WAVeform` queries only work when that channel is displayed | 684, 665 |
| 8-bit data / 16-bit data | `:WAVeform:FORMat BYTE` / `WORD` + `:WAVeform:BYTeorder LSBFirst` (the default is MSBFirst, so send it explicitly) | 674, 670 |
| Unsigned values | `:WAVeform:UNSigned ON` | 690 |
| Screen record / full memory | `:WAVeform:POINts:MODE NORMal` / `RAW` / `MAXimum`. RAW needs the scope stopped, `:TIMebase:MODE MAIN` and `:ACQuire:TYPE NORMal` or `HRESolution`. | 677 |
| Number of points | `:WAVeform:POINts <n>`: 100, 250, 500 or 1000 in NORMal mode; up to 100 000 in RAW/MAXimum; AVERage is capped at 8192 | 675, 666–667 |
| Acquire once and stop (control mode only) | `:DIGitize CHANnel1,CHANnel2` (only in MAIN or WINDow timebase mode; abort with a device clear) | 153 |
| Scaling preamble | `:WAVeform:PREamble?` (10 comma-separated values) | 679 |
| Data | `:WAVeform:DATA?` returns an IEEE 488.2 definite-length block (`#8` + 8 digits), then a trailing LF that must be flushed | 672, 668 |
| Screenshot | `:DISPlay:DATA? PNG, COLor` (binary block; the manual's example allows 15 s, use 20 s) | 244 |

Preamble fields (1-based): 1 = format (0 BYTE, 1 WORD, 4 ASCii), 2 = type, 3 = points, 4 = count, 5 = x increment, 6 = x origin, 7 = x reference, 8 = y increment, 9 = y origin, 10 = y reference. For sample index k = 0, 1, ..., N−1 and raw value D:

```latex
t_k = (k - x_{\mathrm{ref}})\,\Delta x + x_{\mathrm{orig}}, \qquad V_k = (D_k - y_{\mathrm{ref}})\,\Delta y + y_{\mathrm{orig}}
```

Special raw values (unsigned): `0` = hole (no data acquired; stored as NaN), `1` = clipped at the bottom of the screen, `255` (BYTE) or `65535` (WORD) = clipped at the top. WORD data is left-shifted to 16 bits, so the lowest 4 bits are zero (page 669).

### Oscilloscope: measurements (control mode; observe mode calculates them in Python)

Each `:MEASure` command installs a measurement on screen, and a query given a source changes the current measurement source (page 399), so none is allowed in observe mode.

| Purpose | Command | Page |
| --- | --- | --- |
| Frequency | `:MEASure:FREQuency? CHANnel<n>` | 380 |
| Peak-to-peak | `:MEASure:VPP? CHANnel<n>` | 416 |
| Phase between channels (degrees) | `:MEASure:PHASe? CHANnel1,CHANnel2` = delay / period of source 1 × 360 | 389 |
| Delay between channels (s) | `:MEASure:DELay? CHANnel1,CHANnel2` = t(source 2) − t(source 1), using the edges set by `:MEASure:DEFine DELay` (the command form uses automatic edge selection instead). Observed with one 1 kHz signal on both channels: delay 6.5 µs (2.3°) while `:MEASure:PHASe?` gave −0.66° and a Python sine fit 0.27°, so prefer the sine fit (Section 9). | 376–377 |

A returned value of 9.9E+37 means the measurement could not be made (page 367); treat any value above 1E30 as missing.

### Function generator

| Purpose | Command | Page |
| --- | --- | --- |
| Identify | `*IDN?` returns `TEKTRONIX,AFG1062,<serial>,SCPI:99.0 FV:<version>` | 15 |
| Next error (`0` = none; 64-deep queue) | `SYSTem:ERRor?` (`SYSTem:ERRor[:NEXT]?`) returns `<code>,"<message>"` | 62, 67 |
| Clear status / reset (control mode only) | `*CLS` / `*RST` | 14, 20 |
| Wait for operations | Only `*WAI` is documented; `*OPC?` is not. Observed: `*OPC?` replies `0`, not the standard `1`, so do not use it. Synchronise by querying `SYSTem:ERRor?`. | 66 |
| Waveform shape | `SOURce<n>:FUNCtion:SHAPe SINusoid` (query returns `SIN`) | 43–44 |
| Frequency | `SOURce<n>:FREQuency:FIXed <Hz>` (not available in Sweep run mode) | 36 |
| Amplitude (Vpp) | `SOURce<n>:VOLTage:LEVel:IMMediate:AMPLitude <v>VPP`. The amplitude unit follows the front-panel menu, so always send the `VPP` suffix (confirmed on hardware: `1VPP` shows 1.000 Vpp). | 61 |
| Offset | `SOURce<n>:VOLTage:LEVel:IMMediate:OFFSet <v>` | 61 |
| Load setting | `OUTPut<n>:IMPedance INFinity` or `50` (1 Ω to 10 kΩ); the manual says the query returns `9.9E+37` for INFinity. **Deviation:** firmware FV:V1.0.2 replies `50Ω` with the Ω encoded in GBK (bytes `A6 B8`), not a plain number (observed 2026-10-26); LabRador decodes AFG replies as GBK and reads the leading number. At INFinity it replies `9.9E+37Ω`. | 18 |
| Output on/off | `OUTPut<n>:STATe ON` or `OFF` (query returns `0` or `1`) | 18–19 |
| Align the two channels' phase | `SOURce1:PHASe:INITiate` (either suffix aligns both channels; no query form) | 46 |
| Return to local | No SCPI command in the manual (error −201 "Invalid while in local" exists). LabRador sends the VISA go-to-local control on close; the effect is checked with the hardware check. | 62 |
| Sync or trigger output | Not in the programmer manual. Burst and sweep accept an `EXTernal` trigger source, which implies a trigger input only. Check the rear panel and user manual. | 29, 59 |

The `OUTPut:IMPedance` setting only changes the amplitude the generator displays. The physical source impedance is always 50 Ω. With the scope's 1 MΩ inputs, use `INFinity` so the set amplitude matches what the scope sees.

## 6. VISA driver: setup, detection and `labrador doctor`

Every route needs exactly one VISA library installed; the chosen one is **R&S VISA** (Rohde & Schwarz), because it is a single small installer that adds no suite of extra applications. `pyvisa` loads whatever VISA is installed, and one VISA serves both the Keysight scope and the Tektronix generator because both use standard USBTMC.

### Choice of VISA

| Option | Footprint | Notes |
| --- | --- | --- |
| **R&S VISA** (chosen) | Single small installer | Free; supported by `pyvisa` and by MATLAB's Instrument Control Toolbox. Check the actual download size on the R&S site before quoting it to students. |
| Keysight IO Libraries Suite | One heavier suite, includes Connection Expert | Also installable through MATLAB's "Keysight IO Libraries and VISA Interface" support package |
| NI-VISA (incl. Runtime) | Installs through NI Package Manager and pulls in many NI components | Avoid: this is what installed 10+ NI applications during testing |
| `pyvisa-py` (pure Python) | No vendor driver | On Windows, USB needs libusb and often a driver swap (Zadig) that breaks vendor VISA access. Fine on Linux or a Raspberry Pi. |

- Keep **one** VISA installed. Several can coexist, but then instruments may appear twice or fail to open depending on which library answers first.
- Installing VISA needs admin rights. Students can do it on their own laptops; lab PCs need the technical staff (Section 10).

### Removing the NI stack (if already installed)

1. MATLAB: Home → Add-Ons → Manage Add-Ons → uninstall the NI-VISA support package.
2. NI Package Manager → Installed → select all NI items → Remove. Then uninstall NI Package Manager itself from Windows Apps & features.
3. Restart, install R&S VISA from the Rohde & Schwarz website, and check with `visadevlist` (MATLAB) or `labrador doctor`.

### Detecting a missing or broken setup

When `pyvisa` finds no VISA library it raises a specific "could not locate a VISA implementation" error. LabRador catches this and distinguishes three cases, each with its own advice:

| Situation | Exit code (CLI) | Message to the user |
| --- | --- | --- |
| No VISA installed | Dedicated code, e.g. 3 | Warning, install steps, and a link to the R&S VISA download page |
| VISA installed, no instruments found | Dedicated code, e.g. 4 | Check the USB cable is in the rear device port, the instrument is on, and no other program has it open |
| Instrument found but not responding | Dedicated code, e.g. 5 | Power-cycle the instrument, then reconnect |

Exit-code numbers are placeholders until Phase 3 fixes them; once fixed they must not change, because the MATLAB wrapper depends on them.

### How each route presents it

- **Python library:** raises `VisaNotFoundError` (and sibling error types for the other two cases) whose message contains the instructions and link.
- **CLI / `.exe`:** prints the instructions and exits with the dedicated code.
- **MATLAB wrapper:** recognises the exit code and prints a warning with a clickable link (`<a href="matlab:web('<url>','-browser')">`) that opens the download page.
- **Standalone GUI (if built):** a dialog with an "Open download page" button.

### `labrador doctor`

One diagnostic command students run before asking for help. It prints a tick or cross per step, with a fix next to each failure:

1. VISA library found (vendor, version, path).
2. Instruments visible (resource strings).
3. Each instrument answers `*IDN?` (model, serial, firmware).
4. Config file found and addresses match what is connected.

`labrador doctor --install-visa` opens the download page in the browser.

### Constraints

- **Link to the download page, not the installer file.** Direct installer URLs change between versions. Store the URL in the config file so it can be updated without rebuilding the `.exe`.
- **Do not bundle or silently run the installer.** It needs admin rights anyway, and bundling depends on R&S's redistribution terms (unchecked). Pointing to the official page avoids both.

## 7. Data formats

The `.mat` layout must be fixed in Phase 2 and then kept stable, because the MATLAB wrapper, the pure-MATLAB fallback scripts and students' own analysis code all depend on it. The fields below are the starting proposal; finalise names before the first release.

### `Capture` (Python data type)

| Field | Type | Content |
| --- | --- | --- |
| `time` | float array, s | Sample times from the preamble |
| `channels` | dict of float arrays, V | e.g. `{"ch1": ..., "ch2": ...}`; only enabled channels |
| `settings` | dict | Per channel: V/div, offset, coupling, probe; timebase scale and position; trigger source, level, slope, sweep; acquisition type and count |
| `instrument` | dict | `*IDN?` strings for each instrument used |
| `generator_settings` | dict or empty | What the generator was set to (shape, frequency, amplitude, offset, load, output per channel), read once per capture; empty when not read (Section 3, Generator in observe mode). Not a measurement of what reached the circuit. |
| `timestamp` | ISO 8601 string | When the capture completed |
| `mode` | string | `observe` or `control` |
| `labrador_version` | string | Toolkit version that wrote the file |

### Sweep result

| Field | Units | Content |
| --- | --- | --- |
| `freq` | Hz | Frequencies stepped |
| `vpp_ch1`, `vpp_ch2` | V | Peak-to-peak per channel |
| `ratio` | - | `vpp_ch2 / vpp_ch1` |
| `phase` | degrees | Channel 1 to channel 2 |
| `delay` | s | Channel 1 to channel 2 |
| `settings`, `instrument`, `timestamp`, `labrador_version` | - | As for `Capture` |

### File formats

| Format | Writer | For | Notes |
| --- | --- | --- | --- |
| `.mat` | `scipy.io.savemat` | MATLAB users | One struct per file; field names as above; loads with `load()` |
| CSV | standard library / NumPy | Excel, anyone | Data columns only, units in headers (`time_s`, `ch1_V`); metadata in a sidecar `.meta.json` |
| `.xlsx` | `openpyxl` (optional) | Excel users | Data sheet + metadata sheet |
| PNG | scope screenshot | Lab reports | From `:DISPlay:DATA? PNG, COLor` |

SciPy reads `.mat` files too (`scipy.io.loadmat`), so data moves freely between MATLAB and Python users.

## 8. Development environment

VS Code is the editor, because it handles Python and MATLAB in one window and has an official Claude Code extension. PyCharm (with the Claude Code JetBrains plugin) has somewhat stronger Python tooling but weak MATLAB support, so it is the fallback only. Claude Code also runs from any terminal or the Claude desktop app.

### VS Code extensions

| Extension | Publisher | Purpose |
| --- | --- | --- |
| Claude Code | Anthropic | Claude in a side panel: sees the repo, runs terminal commands, changes shown as diffs |
| Python + Pylance | Microsoft | Running, debugging, completion, type checking |
| MATLAB | MathWorks | Edit and run `.m` files using the installed MATLAB |
| Ruff | Astral | Formatting and linting on save; keeps code neat |

### Tooling

| Item | Choice |
| --- | --- |
| Python | 3.12 in a virtual environment (only the developer needs it; students get the `.exe`) |
| Instrument I/O | `pyvisa` |
| Numerics and export | `numpy`, `scipy` (`savemat`), `openpyxl` (optional `.xlsx`) |
| CLI | `typer` or `argparse` |
| Config | TOML file (`tomllib` in the standard library reads it) |
| GUI (later, if built) | `PySide6` + `pyqtgraph` |
| Tests | `pytest` |
| Packaging | `PyInstaller`, one-folder mode |
| Versions | Pinned in `pyproject.toml` / `requirements.txt` |
| Version control | Git, hosted on GitHub; releases as zipped `.exe` folders on GitHub Releases |

### Repository layout

```
labrador/
├── CLAUDE.md
├── README.md
├── pyproject.toml
├── labrador/
│   ├── __init__.py
│   ├── instruments/
│   │   ├── base.py
│   │   ├── transport.py
│   │   ├── scpi.py
│   │   ├── scope.py
│   │   ├── generator.py
│   │   ├── bench.py
│   │   └── simulated.py
│   ├── measurements/
│   │   ├── capture.py
│   │   ├── sweep.py
│   │   └── logging.py
│   ├── export.py
│   ├── errors.py
│   ├── config.py
│   ├── doctor.py
│   ├── cli.py
│   └── server.py
├── matlab/
│   ├── +labrador/
│   └── examples/
├── tests/
├── scripts/
│   └── hardware_check.py
├── prototypes/
│   ├── ScopeControl.m
│   └── CableSweep.m
├── docs/
│   ├── manuals/
│   ├── quickstart.md
│   └── matlab-reference.md
└── packaging/
    └── labrador.spec
```

Put the two programmer manuals (PDF) in `docs/manuals/` so Claude Code can check every SCPI command against them instead of guessing. They are copyrighted by Keysight and Tektronix and the repository is public, so keep them local: `.gitignore` excludes `docs/manuals/*.pdf`, and the README links to where they can be downloaded.

This plan lives in the repository as `docs/design.md` (exported as Markdown), so Claude Code can read it. `CLAUDE.md` points to it. When the plan changes, update `docs/design.md` in the same commit as the code it affects.

## 9. Build plan

The build runs in ten phases, each ending with something that works on its own; Phase 5 is the first release to classmates. Work one phase at a time, and do not start a phase until the previous one's "done when" holds.

### Phase 0: Project setup

- [ ] Create the GitHub repository with the layout in Section 8.
- [ ] Install Python 3.12, create a virtual environment, install `pyvisa`, `numpy`, `scipy`, `pytest`, `ruff`.
- [ ] Install VS Code and the four extensions.
- [ ] Install R&S VISA; confirm both instruments appear in `pyvisa.ResourceManager().list_resources()`.
- [ ] Export this plan to docs/design.md and commit it. Download both programmer manuals (kept out of Git) into `docs/manuals/`.
- [ ] Write `CLAUDE.md` (draft in Section 11).
- [ ] README with the EspoTek non-affiliation note.

**Done when:** a two-line Python script prints both instruments' `*IDN?` replies.

### Phase 1: Instrument drivers

- [x] `Instrument` base class: connect, write, query, binary block read with flush, timeouts, `SYSTem:ERRor?` check after commands, release to local on close.
- [x] Mode flag (observe/control) with the allow-list from Section 3; setting commands raise in observe mode.
- [x] Error types: `VisaNotFoundError`, `InstrumentNotFoundError`, `InstrumentNotRespondingError`.
- [x] `Scope` class (EDUX1052A): waveform read with preamble scaling, preamble re-check, settings query, run state, screenshot; control-mode setters.
- [x] `Generator` class (AFG1062): shape, frequency, amplitude, offset, output, load setting. Verify every command against the AFG1000 manual.
- [x] Simulated `Scope` and `Generator` returning synthetic waveforms (e.g. sine plus noise; for the cable experiment, the transmission-line model in Section 12).
- [x] `pytest` suite against the simulators; a hand-run hardware test script.

* [x] Instruments connect independently; a missing instrument raises `InstrumentNotConnectedError` only when a task needs it (Section 2, Working with one instrument).

**Done when:** a short script sets the generator and pulls a correctly scaled waveform from the scope, and observe mode demonstrably refuses a setting command.

### Phase 2: Measurements and data format

- [ ] `Capture` and sweep result types (Section 7).
- [ ] Single capture, frequency sweep (port of `CableSweep.m`), timed logging, capture-on-Single.
- [ ] Measurements calculated in Python: frequency (hysteresis edge counting, as in `ScopeControl.m`), period, Vpp, max, min, mean, RMS.
- [ ] Export to `.mat`, CSV + sidecar metadata, optional `.xlsx`.
- [ ] **Freeze the `.mat` field names.**

* [ ] Manual-stimulus sweep (scope only): prompt per frequency, sine fit at the measured frequency, skip/repeat/end, saved in the sweep format with `stimulus = manual`.

**Done when:** a sweep run from Python produces a `.mat` that loads in MATLAB with the agreed fields.

### Phase 3: Command-line tool

- [ ] Commands: `list`, `capture`, `sweep`, `log`, `set` (control mode), `doctor`.
- [ ] TOML config for instrument addresses and the VISA download URL.
- [ ] Fixed exit codes, including the three VISA/instrument cases in Section 6.
- [ ] `labrador doctor` and `--install-visa`.

* [ ] Each command declares its required instruments; "required instrument missing" exit code; blank addresses accepted in the config; `doctor` reports each instrument separately.
* [ ] `labrador sweep --manual` for the manual-stimulus sweep.

**Done when:** `labrador sweep --start 10k --stop 40M --out sweep.mat` works and `labrador doctor` diagnoses an unplugged instrument and a missing VISA correctly.

### Phase 4: Packaging and lab-PC test

- [ ] PyInstaller build in one-folder mode.
- [ ] Test on a clean machine without Python.
- [ ] Test on a JCU lab PC: is the `.exe` blocked, flagged by antivirus, or missing VISA? (Section 10.)

**Done when:** the unzipped folder runs `labrador doctor` on a clean PC and on a lab PC, or the blockers are known and raised with lab staff.

### Phase 5: MATLAB wrapper (Option B) and first release

- [ ] `+labrador` package: `labrador.capture`, `labrador.sweep`, `labrador.log`, `labrador.doctor`, each calling the `.exe` and loading the `.mat`.
- [ ] Exit codes turned into readable MATLAB errors; the no-VISA warning with a clickable link.
- [ ] Example script: the cable sweep end-to-end.
- [ ] Student quickstart (install VISA, unzip, first sweep).

**Done when:** a classmate installs R&S VISA, unzips the release, and runs a sweep from MATLAB without help. Tag v0.1 on GitHub Releases.

### Phase 6: Server mode (Option C)

- [ ] `labrador serve`: holds connections, acquisition on its own thread, JSON-lines protocol on `127.0.0.1`.
- [ ] Single-instance guard; clean shutdown releasing the instruments.
- [ ] MATLAB client class (`tcpclient`): auto-start, reconnect on drop, state always queried from the server.
- [ ] Protocol documented in `docs/`.

**Done when:** MATLAB can stream frames for several minutes, survive a server restart, and leave the instruments free on exit.

### Phase 7: MATLAB GUI

- [ ] Rebuild `ScopeControl` on the Option C client: the server streams, the GUI only displays.
- [ ] Observe mode as default; control widgets enabled only in control mode.
- [ ] Labrador icon.

**Done when:** the GUI stays responsive during streaming and never changes scope settings in observe mode.

### Phase 8: Documentation and releases

- [ ] Quickstart, MATLAB function reference, Python API reference, CLI reference, troubleshooting (`labrador doctor` output explained).
- [ ] Version numbers and a changelog; zipped `.exe` folders on GitHub Releases.
- [ ] Optional: standalone PySide6 GUI for non-MATLAB users if demand exists.

### Phase 9: Analysis modules

The generator supplies a known stimulus, the scope captures the response, and Python does the analysis, which turns the pair into instruments neither is on its own. All modules run in control mode and live in `labrador.analysis`.

| Order | Module | Method | Main limits |
| --- | --- | --- | --- |
| 1 | Frequency response analyser (Bode) | Sweep the generator; stimulus on CH1, device output on CH2; gain and phase from fitted sines | 50 MHz scope bandwidth; 1 MΩ / 16 pF inputs load the device |
| 2 | Impedance analyser | Drive the device through a known resistor; the channels give voltage and current; Z(f) = V/I. Cable Z0 = √(Z\_open · Z\_short) | Accuracy set by the reference resistor and the 8-bit ADC |
| 3 | Spectrum and distortion analyser | FFT with windowing and averaging; THD and SINAD of amplifiers against level | 8-bit ADC: roughly 40–48 dB dynamic range at best |
| 4 | Software lock-in amplifier | Correlate the capture with the known stimulus frequency | Shares the sine-fit core with modules 1 and 2 |
| 5 | Component curve tracer | Slow generator sweep; current through a sense resistor; I-V plot | Generator limited to 10 Vpp into high-Z and modest current |
| 6 | Simplified vector signal analyser (stretch) | Generator makes AM, FM, PM, ASK, FSK or PSK; Python mixes to I/Q and plots constellations, eye diagrams, EVM | Carriers up to about 10–20 MHz, 8-bit dynamic range, gaps between records, carrier recovered in software |
| 7 | Time-domain reflectometry (optional) | Fast square-wave edge into a cable; locate reflections | Generator edges limit resolution to about a metre of coax |

#### Accuracy techniques shared by every module

- **Always capture a reference.** Stimulus on CH1, response on CH2, and work with ratios. This cancels the generator's amplitude error and flatness, and removes the need for a shared clock, because both channels use the scope's own sample clock.
- **Fit sines instead of using `:MEASure`.** A least-squares sine fit at the known frequency to each record gives amplitude and phase with far better noise rejection than Vpp or the scope's phase measurement. Implement it once as a core function.
- **Calibrate fixed errors.** A "through" run with no device (or two identical short cables) records channel skew and gain mismatch to subtract.

#### No shared reference clock

Neither instrument appears to have a 10 MHz reference input or output, so they cannot be locked to a common external clock; confirm by checking both rear panels and manuals. This matters less than it seems: the reference-channel method measures CH1 against CH2 on the same sample clock, so ratios and phase differences are exact. The two internal oscillators differ by only a few parts per million (tens of hertz at 10 MHz), which the VSA module estimates and removes from each record in software. Capture timing can still be aligned by triggering the scope from the generator's signal (CH1, or the scope's external trigger input).

- [ ] Core sine-fit and calibration functions, tested against the simulators.
- [ ] Modules in the order above; each with a CLI command, a MATLAB wrapper function, and an example.
- [ ] Re-analyse the cable experiment with the Bode and impedance modules as the first validation.

**Done when:** the Bode module reproduces the cable ratio from Section 12 with less scatter than the `:MEASure`-based sweep, and the impedance module measures a known resistor and capacitor within a few percent.

## 10. Deployment on university PCs

Options B and C work standalone in principle, but whether the `.exe` runs on a managed JCU lab PC depends on IT policy, not the code. Test early (Phase 4) and plan for the lab-image route.

| Obstacle | Why | Mitigation |
| --- | --- | --- |
| Unsigned `.exe` blocked | Managed images often block programs launched from Downloads, USB drives or network home folders (AppLocker or similar); SmartScreen may warn | Ask lab technical staff to whitelist or install LabRador |
| Antivirus false positive | PyInstaller executables are sometimes flagged as malware | Same as above; code signing removes it but costs money |
| No VISA on the PC | The `.exe` does not contain VISA; installing needs admin rights | Lab PCs used with instruments may already have one; otherwise lab staff install R&S VISA |
| Slow or blocked start | One-file PyInstaller builds unpack to a temp folder on every launch | Build in one-folder mode |
| Firewall prompt (Option C) | Listening sockets can trigger Windows Firewall; students cannot approve it | Bind the server to `127.0.0.1` only; test on a lab PC |
| Toolbox not installed (fallback scripts only) | The licence covers it, but the lab image must have it installed | Check with `ver` in MATLAB on a lab PC |

### Recommended route

1. **Test on a real lab PC early:** run `visadevlist` in MATLAB and launch the `.exe` from a home folder to see what is blocked.
2. **Ask the College of Science and Engineering lab technical staff** to add LabRador and R&S VISA to the lab image. That removes every obstacle above for every student.
3. **Keep a pure-MATLAB fallback:** a few `.m` functions using `visadev` directly (like `CableSweep.m`) for basic capture and sweeps if the `.exe` is ever blocked. They must write the same `.mat` layout as the main toolkit so students' analysis code works with either.
4. **Code signing** only if LabRador becomes widely used.

## 11. Working with Claude Code

Claude Code does the bulk of the implementation; your job is to plan each phase with it, check the hardware yourself, and keep `CLAUDE.md` current. It reads `CLAUDE.md` at the start of every session, so anything written there never needs repeating.

### Practices

- **Plan first, one phase at a time.** Ask Claude Code to propose a plan for the phase (plan mode), review it, then let it implement.
- **Give it a way to check its work.** The simulators and `pytest` let it run tests and confirm changes without the hardware.
- **Commit after each working step.** Claude Code can write the commits; each one is a rollback point.
- **Point it at the manuals.** Every SCPI command must be checked against `docs/manuals/`, never guessed.
- **Hardware checks are yours.** Claude Code cannot see the instruments. After each driver change, run the hardware test script yourself.
- **Effort setting:** the default is fine for most work. Raise it for designing the server protocol (Phase 6), the driver layer and observe-mode enforcement (Phase 1), and debugging problems that span Python, VISA and hardware. Higher effort is slower and uses more of the usage allowance.

### Draft `CLAUDE.md`

The repository's `CLAUDE.md` is now the master copy and has moved on from this draft (for example its Manuals section); the draft is kept for reference.

```markdown
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
- Check every SCPI command against docs/manuals before using it.
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
```

Update it whenever a decision changes, and add a short note for anything you find yourself telling Claude Code twice.

## 12. Background: prototypes and lessons learned

Two MATLAB prototypes already exist and contain verified working code; commit both to the repository under `prototypes/` as the starting reference for the drivers.

| File | What it does | Becomes |
| --- | --- | --- |
| `ScopeControl.m` | `uifigure` GUI: connect, both channels, timebase, trigger, Run/Stop/Single, Autoscale, live streaming via a timer, measurements, CSV/MAT/PNG export | Reference for the `Scope` driver and the Phase 7 GUI |
| `CableSweep.m` | AFG1062 drives two cables from separate outputs; EDUX1052A measures Vpp, phase and delay over 80 log-spaced points, 10 kHz to 40 MHz, 16× averaging; saves CSV and plots | Reference for the `Generator` driver and the sweep routine |

### Lessons from the prototypes

- **MATLAB is single-threaded for callbacks, timers, plotting and I/O.** A blocking `:DIGitize` freezes the whole GUI, and a button press waits for the current acquisition. `ScopeControl.m` needed an `ioBusy` guard. This is the main reason the server holds acquisition in Option C.
- **`uifigure` plots redraw slower than classic figures.** Fine for a few frames per second; update `XData`/`YData` and call `drawnow limitrate` rather than replotting.
- **Speed is set by the instruments, not the language.** Trigger, acquisition, averaging, generator settling and USB transfer take tens to hundreds of milliseconds per cycle in any language.
- **After `readbinblock` / a binary read, flush the trailing LF**, or the next query reads it.
- **About 1000 points over USB gives a few frames per second;** 250–500 points is faster.
- **Invalid measurements return about 9.9E37.** Treat anything above 1E30 as missing.

### Limits of a benchtop scope

- A scope acquires in records: it fills memory, then the record is transferred. There are always gaps between frames, so gap-free continuous streaming is impossible.
- **Live view:** read `:WAVeform:DATA?` while running (observe-mode logging). Gaps remain, and the two channels may come from slightly different acquisitions.
- **One long gap-free record:** single capture, slow timebase, `RAW` points mode. Record length = points ÷ sample rate; a slower timebase lowers the sample rate, so keep it well above twice the highest frequency of interest.
- **Truly continuous streaming** needs a data-acquisition device (an NI DAQ, or a sound card for audio-band signals), not a scope.

### Alternatives considered

- **BenchVue Basic (Keysight, free):** remote front panel, trace export (CSV, MATLAB, Excel, HDF5), measurement logging limited to under one hour in the free version, Windows only, no custom processing per capture. LabRador replaces it for students.
- **Pure MATLAB:** quickest for coursework, but limited by single-threading and licensing. Kept only as the fallback scripts.
- **Pure Python:** the core of LabRador; MATLAB users reach it through the `.exe` or the server.

### The cable experiment (model for the simulator)

Two BNC cables, believed to be 50 Ω (shorter) and 75 Ω (longer), are each driven from a 50 Ω generator output into the scope's 1 MΩ input. Summing the reflections between the two ends gives the voltage at the scope relative to the generator's internal voltage:

```latex
\frac{V_L}{V_g} = \frac{Z_0}{Z_s+Z_0}\cdot\frac{(1+\Gamma_L)\,e^{-j\omega\tau}}{1-\Gamma_s\Gamma_L\,e^{-2j\omega\tau}}, \qquad \Gamma_s=\frac{Z_s-Z_0}{Z_s+Z_0}, \quad \Gamma_L \approx 1
```

- **50 Ω cable:** Γs = 0, so the amplitude is flat and only the phase changes.
- **75 Ω cable:** Γs = −0.2, so the amplitude ripples. At the quarter-wave frequency f = 1/(4τ), the peak-to-low-frequency amplitude ratio equals Z0/Zs (about 1.5). A peak means Z0 > Zs; a dip means Z0 < Zs. For about 2 m of polyethylene-dielectric coax, f is about 25 MHz.
- **Fitting:** fit the CH2/CH1 ratio (which cancels the scope's bandwidth roll-off and the generator's flatness) with `lsqcurvefit`, unknowns Z0, τ and the scope input capacitance (about 15 pF), with ΓL from a parallel RC load.
- **Derived quantities:** velocity factor ℓ/(τc); εr = (c/v)²; C′ = τ/(Z0ℓ); L′ = Z0τ/ℓ. Independent check: Z0 = τ / C\_total, with the open-ended cable's capacitance measured on an LCR meter or multimeter.
- **Calibration:** run the sweep once with two short identical cables and subtract that delay, to remove generator channel phase and scope channel skew. Swapping cables between channels removes gain mismatch.
- **Further ideas:** group delay τg = −dφ/dω (flat for 50 Ω, wiggles for 75 Ω); a fast-edge square wave shows a reflection staircase with steps of width 2τ (time-domain reflectometry); skin-effect loss (α ∝ √f) is only about 0.2–0.3 dB at 40 MHz for 2 m, so expect to bound it rather than measure it.

## 13. Decision log and open questions

Decisions made so far, newest first, followed by the questions still to settle. Add to both as the project moves.

| Date | Decision | Reason |
| --- | --- | --- |
| 2026-10-26 | Observe-mode allow-list is explicit and fails closed; `:SYSTem:LOCK` refused in both modes | Unlisted queries can have side effects (`*TST?`, `:MEASure`) |
| 2026-10-26 | Generator may be queried in observe mode: read-only, briefly, released at once, opt-in until tested on hardware; stored as `generator_settings` | Stimulus metadata for lab reports without locking the front panel |
| 2026-10-26 | Run state from `:OPERegister:CONDition?` and `:TER?` | The EDUX1052A has no `:RSTate?` |
| 2026-10-26 | Generator API uses methods with a channel argument (`set_frequency(1, 1e3)`) | Maps one-to-one onto the MATLAB wrapper and the server protocol |
| 2026-10-26 | Simulators work at the SCPI transport level, below the real drivers | Tests exercise the real driver code, policy and parsing |
| 2026-10-22 | Name: LabRador; package and command `labrador` | Personal project; EspoTek clash handled by a README note |
| 2026-10-22 | `labrador doctor` and dedicated exit codes for missing VISA / instrument | Most likely first-run failure; one command to diagnose |
| 2026-10-22 | Link to the VISA download page, never bundle the installer | Needs admin rights; redistribution terms unchecked |
| 2026-10-22 | Three distribution routes: `.exe`, pip from GitHub, MATLAB wrapper | Serves MATLAB, Python and Excel users from one codebase |
| 2026-10-21 | Observe mode as the default, enforced in the driver | Must not obstruct students who operate the instruments by hand |
| 2026-10-21 | R&S VISA as the recommended driver | Smallest footprint; NI's support package installed 10+ applications |
| 2026-10-21 | Instrument Control Toolbox not required by the main toolkit | Python owns the instrument connection |
| 2026-10-21 | Option B first, Option C second; Option A rejected as main route | No Python needed for students; stateful live control added later |
| 2026-10-21 | Python core with MATLAB front end | Python for structure, threading and longevity; MATLAB for students' analysis |
| 2026-10-21 | VS Code with the Claude Code extension | One editor for Python and MATLAB |

### Open questions

- [ ] Exact R&S VISA download size and current download page URL.
- [x] Does `:RSTate?` exist on the EDUX1052A? No. Use `:OPERegister:CONDition?` (bit 3 running, bit 5 armed) and `:TER?` (Section 5).
- [ ] How can capture-on-Single tell a completed Single from the user pressing Stop? Both end in run state 32 with `:TER?` = 1 (Section 5), so the registers cannot tell them apart. Decide in Phase 2.
- [x] Do `:MEASure:PHASe?` and `:MEASure:DELay?` take two sources as written? Yes (pages 389, 376).
- [x] Confirm every AFG1062 command in Section 5 against the AFG1000 manual. Done; phase alignment is `SOURce1:PHASe:INITiate`.
- [x] Does the AFG1062 answer `*OPC?` (not in its manual)? It replies `0`; not used.
- [x] Does the AFG1062 amplitude follow the `VPP` suffix? Yes (1 Vpp set, 1.000 Vpp shown, 1.045 V peak-to-peak measured on the scope).
- [x] How does the AFG1062 return to local control after remote commands? The VISA go-to-local control works, and a query alone does not lock the front panel (one contrary answer in an earlier run was a mistaken answer). Generator metadata is on by default (Section 3).
- [ ] After a Single at 5 ms/div (first hardware check, 2026-10-26), `:WAVeform:POINts 1000` returned -222 "Data out of range" once; not reproduced in two later runs. Watch for it in Phase 2 logging.
- [ ] Can the `.exe` run on a JCU lab PC, and do lab PCs already have a VISA installed?
- [ ] Will lab technical staff add LabRador and R&S VISA to the lab image?
- [ ] Final `.mat` field names and CLI exit-code numbers (Phases 2 and 3).
- [ ] `typer` or `argparse` for the CLI.
- [ ] Is a standalone PySide6 GUI needed for non-MATLAB users?
- [ ] Optional `lbr` alias: yes or no?

* [ ] Confirm neither instrument has a 10 MHz reference input or output (rear panels, manuals).
* [ ] Does the AFG1062 have a sync or trigger output for triggering the scope (Phase 9)? Not in the programmer manual, which only mentions an external trigger input; check the rear panel and user manual.
