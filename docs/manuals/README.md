# Instrument manuals

This folder holds the programmer manuals for the two instruments LabRador supports.
They describe the SCPI (Standard Commands for Programmable Instruments) commands
used to control the instruments over USB, and are used locally during development.

Both manuals are copyrighted by their manufacturers, so they are not included in
this repository. Download them from the links below and save them here under the
file names shown.

| Instrument | File name | Download |
| --- | --- | --- |
| Keysight EDUX1052A (InfiniiVision 1000 X-Series) | `keysight-1000x-programmers-guide.pdf` | [Keysight](https://www.keysight.com/us/en/assets/9018-07747/programming-guides/9018-07747.pdf) |
| Tektronix AFG1062 (AFG1000 Series) | `tektronix-afg1000-programmer-manual.pdf` | [Tektronix](https://www.tek.com/en/manual/function-generator/afg1000-series-arbitrary-function-generator-programmers-manual-afg1000) |

A searchable text copy of each is generated next to the PDF:

    pdftotext keysight-1000x-programmers-guide.pdf keysight-1000x-programmers-guide.txt
    pdftotext tektronix-afg1000-programmer-manual.pdf tektronix-afg1000-programmer-manual.txt

Everything in this folder except this README is excluded from Git.