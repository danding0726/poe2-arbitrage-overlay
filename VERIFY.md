# Verification record

- `python -m unittest discover -s tests -v`: 13 tests passed. Coverage includes historical cycle discovery, integer lot sizing, shared quote reuse, expiry, stock checks, stock OCR rejection of ratios, and Poe2Scout independent-book filtering.
- `python main.py --self-test`: loaded the bundled demo, item catalog, icon, and local RapidOCR models.
- GGG PoE2 Currency Exchange history API: fetched two pages and parsed 5,828 market records; generated three- and four-step historical candidates.
- Poe2Scout `Leagues`, `ExchangeSnapshot`, and `SnapshotPairs`: live requests succeeded. On 2026-09-23 UTC, the current standard league was `Forbidden Rites`. The response held 1,633 raw pairs, consolidated to 1,364 exact item-ID pairs. The snapshot was about 82 minutes old when read. The liquidity and gain filters generated 80 displayed cross-currency candidates. This is not a second-by-second executable order book.
- User-provided 1920×1080 screenshot without inventory: OCR in the default region `[588, 160, 1328, 345]` read pay 1, receive 21, and gold fee 11,760 from the selected trade. The lower listing was excluded.
- PySide6 offscreen window: rendered the expanded checklist, historical routes, round-trip mode, and Poe2Scout cross-currency mode. A cached Scout snapshot showed the league, snapshot age, and 80 candidates. A three-order demo produced the expected minimum integer lot counts and profit.
- macOS PyInstaller `--onedir --windowed` smoke build and bundled `--self-test` passed. This is not a Windows EXE validation.

The Windows build script and GitHub Actions workflow are configured. A Windows game session is still needed to calibrate the stock OCR region and verify screenshot permissions, global hotkeys, and borderless-window overlay behavior. Actual market prices and stock may change between reading and trading.
