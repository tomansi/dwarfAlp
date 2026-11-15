# DWARF 3 + NINA field tests

Record of the environment and results used to validate the DWARF 3 changes on
branch `dwarf3-nina-fixes` (continuous sessions, full-resolution FITS, GOTO from
NINA, Activity log). DWARFLAB publishes no API and changes the protocol between
releases, so results only hold for the versions listed here.

## Test environment

| Component | Version |
| --- | --- |
| DWARF 3 firmware | V1.5.4.1 |
| DWARFLAB app (used for calibration and to check files) | V3.4.5 (build 794) |
| N.I.N.A. | 3.2.0.9001 |
| Plate solver | ASTAP with D50 database |
| Windows | 11 (build 22631), x64 |
| Python (build machine) | 3.14 |
| dwarfAlp | fork `tomansi/dwarfAlp`, branch `dwarf3-nina-fixes` |

Mount mode: alt-az (`AZI_MODE`). NINA connects to dwarfAlp natively over
Alpaca (no ASCOM Platform installed).

Firmware and app updates are on hold until these changes are tested; repeat the
checks below after updating.

## Results

| Date | Test | Result |
| --- | --- | --- |
| 2026-11-13 | Full-resolution capture (3856×2180, ~16 MB FITS) in DSO mode 2 | OK (indoors) |
| 2026-11-13 | Continuous session at 5, 10, 30 and 60 s; ImageBytes transfer | OK (indoors) |
| 2026-11-13 | Delete the DWARF session after download | OK (needs dwarfAlp open 1–2 min after the sequence) |
| 2026-11-15 | Captures under the sky (4 s VIS, 10 s Astro) | OK |
| 2026-11-15 | First GOTO from NINA (NGC 7000, NGC 2281): one-click GOTO, plate solve, tracking | OK in ~20–25 s |
| 2026-11-15 | 10 × 10 s sequence after GOTO (HFR ≈ 1.7, ~450 stars) | OK |
| 2026-11-15 | Second GOTO after a capture session | Fails: DWARF answers -11501 (busy) until power cycled; fix pending test |
| 2026-11-15 | Astro autofocus before calibration | DWARF acknowledges but never starts it; GOTO now continues without it |
| 2026-11-15 | Folder names created by one-click GOTO (`Caldwell20`, `NGC2281`) | Differ from the app (`C 20`, `NGC 2281`); the app still recognises them |

## Pending checks

- GOTO → sequence → GOTO to another target (GoLive after the session, busy retry).
- NINA `Set Tracking: Stopped` and end-of-sequence "Stop tracking" stop the DWARF tracking.
- Cancelling a sequence from NINA (AbortExposure) ends the DWARF session.
- "Deliver every frame in order" option.
