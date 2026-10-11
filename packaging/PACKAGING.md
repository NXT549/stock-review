# Packaging the desktop app

The review tool ships as a Windows desktop app frozen with PyInstaller — it runs
with **no Python install** on the target machine. One build produces **three
executables** that share a single `_internal/` bundle:

| Exe | What it is | Console |
|-----|------------|---------|
| `Stock Review.exe`          | the dark Live board window | none (`console=False`) |
| `Stock Review Terminal.exe` | the real CLI + ASCII animations | yes (`console=True`) |
| `Stock Review Classic.exe`  | the original Win98 window | none (`console=False`) |

Both freeze the same `packaging/frozen_entry.py`, which picks Live board / Classic / CLI from
the running exe's own filename — one `Analysis`, one bundle, zero drift between the
two. The GUI is a thin front-end over `stock_analyzer.review.cli.main`.

## Run it from source (no build)

```powershell
py stock_review_gui.py          # the GUI
py stock_review.py --source japan   # the CLI, unchanged
#  or
py -m stock_analyzer.review.gui
```

## Build the app + installer

From the project root:

```powershell
powershell -ExecutionPolicy Bypass -File packaging/build_exe.ps1
```

This:

1. Installs PyInstaller into `.venv313` if needed.
2. Cleans `build/`, `dist/`, `packaging/installer_output/`.
3. Freezes both exes via `packaging/stock_review_gui.spec` →
   `dist/Stock Review/`.
4. If **Inno Setup** (`ISCC.exe`) is installed, packages a single
   one-click installer:

```
packaging/installer_output/Stock Review Setup.exe
```

Hand that **one** `Setup.exe` to a user — they double-click it, click through the
SmartScreen notice once (see `../INSTALL.txt`), and get desktop + Start-menu
shortcuts. Per-user install (`{autopf}` + `PrivilegesRequired=lowest`) → lands in
`%LocalAppData%\Programs\Stock Review` with **no admin/UAC prompt**.

- Install Inno Setup once: `winget install JRSoftware.InnoSetup`
  (or https://jrsoftware.org/isdl.php). If it's missing, the build still produces
  `dist/Stock Review/` — you can just zip that folder to share the app instead.
- The `dist/Stock Review` folder is ~95 MB unzipped; the `Setup.exe` compresses
  it to ~35–45 MB.
- It's a **one-folder** bundle (chosen over one-file: faster startup and far less
  flaky with pandas/numpy/curl_cffi).

## Where reports/cache land

- **Installed app (frozen):** `Documents\Stock Review\reports\` (and the resume
  cache under `Documents\Stock Review\data\review_cache\`). Easy to find, and
  the GUI's *Open report* / *Open folder* buttons point here.
- **Source checkout:** `./reports/` next to the project, exactly as before.

**Uninstall** removes the install dir completely (`[UninstallDelete]` sweeps
files the app created there too), then asks whether to also delete
`Documents\Stock Review` — the `[Code]` section mirrors the GUI's
Documents-candidate list (Known Folder / OneDrive / USERPROFILE) so it finds
the data wherever the app actually put it. A **silent** uninstall (`/SILENT`)
deletes everything without asking.

It needs a **live internet connection** at runtime (it pulls from Yahoo via
`yfinance`); it is not an offline app.

## Verifying / debugging a build

Headless end-to-end smoke test of the frozen exe (fetches one ticker, no window).
It writes `selftest_result.txt` into the app's data folder
(`Documents\Stock Review\` when frozen):

```powershell
$env:STOCK_REVIEW_SELFTEST = "1"
& "dist\Stock Review\Stock Review.exe"
Get-Content "$env:USERPROFILE\Documents\Stock Review\selftest_result.txt"   # expect exit=0
Remove-Item Env:\STOCK_REVIEW_SELFTEST
```

`STOCK_REVIEW_SELFTEST` forces the GUI worker selftest regardless of which exe
you launch, so it smoke-tests the whole frozen import chain (yfinance, curl_cffi,
pandas, …) for either binary.

If the GUI app fails to start with no message, rebuild the GUI exe with a console
so you can see the traceback: set `console=True` on `exe_gui` in
`packaging/stock_review_gui.spec`, rebuild, run from a terminal, then flip it back
to `False` for the release build. (The Terminal exe already has a console.)

## Notes / gotchas

- **Interpreter:** build with `.venv313` (Python 3.13) — the same interpreter the
  app runs under. Freezing with a different Python than you test with invites
  subtle mismatches.
- **`curl_cffi`** ships a compiled libcurl + CA bundle that PyInstaller misses by
  default; the spec pulls it in with `collect_all("curl_cffi")`. If you bump
  `yfinance`/`curl_cffi` and pulls break inside the exe (but work from source),
  that collection is the first place to look.
- The spec **excludes** `scipy`, `ta`, `sqlalchemy`, `matplotlib`, `PIL` — the
  review/GUI path never imports them (they belong to the separate
  indicators/scoring/storage pipeline). Removing them keeps the bundle small.
- **Icon:** `packaging/stock_review.ico` (regenerate with
  `py packaging/make_icon.py`). It's set on both exes and bundled as a data file
  so the GUI window can display it at runtime (resolved via `sys._MEIPASS`).
