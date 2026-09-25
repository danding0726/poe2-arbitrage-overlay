# Verification record

- The app entry point is `main.py → poe2arb.dashboard.run`. The retired multi-route overlay, checklist, round-trip module, and demo feed are no longer packaged.
- `poe2arb.single_item` computes one item's A→item→B→A path using three independently read, directed quotes. Tests cover whole-order sizing, stock bounds, quote expiry, and exact-order gold handling.
- `poe2arb.core` retains only the published hourly reference parser used to rank items for inspection. Tests cover the latest-hour selection and rejection of missing, one-sided, or scattered price ranges.
- Qt offscreen dashboard smoke testing checks the single-page quote flow, currency/item icon rendering, profit result, and saved quote restoration.
- On 2026-09-25, `python -m unittest discover -s tests -v` passed 28 tests and `python main.py --self-test` passed.
- The Windows PyInstaller `--onedir --windowed` build completed after removing the demo asset. The resulting EXE loaded the bundled RapidOCR models with `--self-test`.
- Windows game-session verification is still needed for screenshot calibration, OCR against live exchange panels, foreground rendering, and actual gold fees at the planned quantity. The app does not place trades.
