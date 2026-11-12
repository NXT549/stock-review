"""Opens the real Live board window and drives it through a whole session.

Opt-in: it does nothing unless ``STOCK_REVIEW_WINDOW_TEST=1`` (it needs Tk and
a display, and puts a window on screen for a few seconds). The Windows build
workflow sets the flag, so the window is exercised on the Tk the app ships with
before anything is released.

    STOCK_REVIEW_WINDOW_TEST=1 python tests/test_gui_board_window.py

Offline: the worker is replaced by a fake that posts the same queue events a
real run does (rows with and without values, a failure, in-fetch retries, a
retry round, a report file, a no-report ending). Settings and reports go to a
temp folder. Any exception raised inside a Tk callback fails the test; Tk would
otherwise only print it.
"""
from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _fake_worker(gate: threading.Event, mode: dict, app_box: list):
    def run(argv, q, progress, cancel):
        if mode["bad"]:
            q.put(("log", "Could not read the list.\n"))
            q.put(("done", {"code": 2, "error": "Could not read the list. " + "x" * 300,
                            "cancelled": False}))
            return
        progress.total = 33
        q.put(("log", "Fetching 33 tickers\n"))
        for i in range(30):
            progress.update(i + 1, ticker=f"T{i:02d}", ok=i + 1, failed=0, rec={
                "ticker": f"T{i:02d}", "name": f"Test Holdings {i} with a long company name",
                "sector": "Industrials" if i % 3 else "n/a",
                "price": None if i == 4 else 10.0 + i, "currency": "AUD",
                "ret_1m": None if i == 5 else (i - 15) / 60, "error": None})
        progress.update(31, ticker="BAD1", ok=30, failed=1,
                        rec={"ticker": "BAD1", "name": "BAD1", "error": "404 Not Found"})
        progress.update(32, ticker="BAD2", ok=30, failed=2,
                        rec={"ticker": "BAD2", "name": "BAD2", "error": "429 Too Many"})
        progress.retry(ticker="SLOW", attempt=1, max_retries=3, delay=4.0)
        progress.note("retry round 1/4: 2 ticker(s) still missing - pausing 5s")
        gate.wait(20)
        progress.note(None)
        progress.update(33, ticker="SLOW", ok=31, failed=2, rec={
            "ticker": "SLOW", "name": "Slow Co", "sector": "Energy", "price": 3.5,
            "currency": "USD", "ret_1m": 0.01, "error": None})
        app = app_box[0]
        Path(app.path).parent.mkdir(parents=True, exist_ok=True)
        Path(app.path).write_text("# Stock review\n" + "row\n" * 500, encoding="utf-8")
        q.put(("done", {"code": 130 if cancel.is_set() else 0, "error": None,
                        "cancelled": cancel.is_set()}))
    return run


def main() -> int:
    if os.environ.get("STOCK_REVIEW_WINDOW_TEST") != "1":
        print("SKIP window test (set STOCK_REVIEW_WINDOW_TEST=1 to open the real window)")
        return 0

    import tkinter as tk

    from stock_analyzer.review import gui_board as gb
    from stock_analyzer.review.universe import DEFAULT_TICKERS

    tmp = Path(tempfile.mkdtemp(prefix="stock_review_window_"))
    (tmp / "reports").mkdir()
    gb._gui.BASE_DIR = tmp
    gb.BOARD_SETTINGS = tmp / "gui_board_settings.json"
    os.chdir(tmp)

    gate, mode, app_box = threading.Event(), {"bad": False}, []
    gb._gui._run_review = _fake_worker(gate, mode, app_box)

    root = tk.Tk()
    errors: list[str] = []

    def on_error(exc, val, tb):
        import traceback
        errors.append("".join(traceback.format_exception(exc, val, tb)))
    root.report_callback_exception = on_error

    def pump(seconds: float = 0.25, until=None) -> None:
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            root.update()
            if until is not None and until():
                return
            time.sleep(0.01)
        assert until is None, "timed out waiting for the window to reach a state"

    def check(cond, what: str) -> None:
        assert not errors, "exception in a Tk callback:\n" + "\n".join(errors)
        assert cond, what
        print(f"OK {what}")

    print(f"Tk {root.tk.call('info', 'patchlevel')} on {sys.platform}")
    app = gb.BoardApp(root)
    app_box.append(app)
    pump()

    # -- idle: every source, the inputs, the preview ---------------------------
    check(app.screen == "idle" and len(app.table.rows) == len(DEFAULT_TICKERS),
          "opens idle with the starter list previewed")
    for key in gb.MODE_KEYS:
        app.set_mode(key)
        pump(0.1)
    check(app.mode == "file", "every source in the sidebar builds its inputs")
    app.set_mode("source")
    app._pick("source", "australia")
    pump(0.1)
    check(app.title_lbl["text"] == "ASX 200" and not app.table.rows,
          "a looked-up list shows its name and the empty state")
    app.set_mode("custom")
    app.tickers_var.set("aaa, bbb ccc")
    pump(0.1)
    check([r.ticker for r in app.table.rows] == ["AAA", "BBB", "CCC"],
          "typed tickers preview in the table")

    # -- options drawer: live validation ---------------------------------------
    app.toggle_drawer()
    app.limit_var.set("abc")
    pump(0.1)
    check(bool(app.blocker) and str(app.run_btn["state"]) == "disabled",
          "a bad Limit blocks Run and says why")
    app.limit_var.set("")
    app._set_option("fmt", "csv")
    app._set_option("fmt", "md")
    pump(0.1)
    check(not app.blocker and str(app.run_btn["state"]) == "normal",
          "clearing it enables Run again")
    app.toggle_drawer()

    # -- a run ---------------------------------------------------------------
    app.set_mode("source")
    app.start()
    pump(5, until=lambda: len(app.model.rows) >= 33)
    pump(0.6)            # one animation tick
    c = app.model.counts()
    check(app.screen == "run" and (c["ok"], c["retry"], c["total"]) == (30, 3, 33),
          "run: cumulative counts, failures read as retrying between rounds")
    check(app.model.rows["T04"].price is None and app.model.rows["T05"].ret_1m is None
          and app.model.rows["T00"].sector == "",
          "missing price / return / sector stay missing")
    check(bool(app.finish_btn.winfo_ismapped()) and app.run_eta["text"] == "",
          "Finish now is offered and no ETA is shown while waiting")

    # table: sort, filter, find, keyboard, mouse
    app.on_sort("ret")
    top = app.table.rows[0]
    check(top.ret_1m == max(r.ret_1m for r in app.model.rows.values() if r.ret_1m is not None),
          "sort by 1-month: best first")
    check(app.table.rows[-1].ret_1m is None, "rows with no return sort last")
    app.on_sort("ret")
    check(app.table.rows[-1].ret_1m is None, "and still last when the order flips")
    app.on_sort("ret")
    app.set_filter("retry")
    check(len(app.table.rows) == 3, "Retrying filter")
    app.set_filter("all")
    app.find_var.set("holdings 7")
    check([r.ticker for r in app.table.rows] == ["T07"], "find by company name")
    app.find_var.set("")
    body = app.table.body
    body.focus_force()
    for seq, kw in (("<Motion>", {"x": 20, "y": 40}), ("<Button-1>", {"x": 20, "y": 40}),
                    ("<Down>", {}), ("<Next>", {}), ("<End>", {}), ("<Home>", {}),
                    ("<MouseWheel>", {"delta": -120}), ("<Leave>", {})):
        body.event_generate(seq, **kw)
        pump(0.03)
    app.table.head.event_generate("<Motion>", x=20, y=10)
    app.table.head.event_generate("<Button-1>", x=20, y=10)
    pump(0.05)
    check(app.sort_key == "ticker" and app.table.selected is not None,
          "table mouse, wheel, keyboard and header clicks")
    app.on_sort("ticker")
    app.on_sort("ticker")
    if app.mosaic is not None:
        app.mosaic.canvas.event_generate("<Motion>", x=3, y=3)
        app.mosaic.canvas.event_generate("<Button-1>", x=3, y=3)
        pump(0.05)
        check(app.table.selected == "T00", "clicking a mosaic square selects its row")
    app.toggle_log()
    app.toggle_log()

    # -- done ------------------------------------------------------------------
    gate.set()
    pump(8, until=lambda: not app.running)
    pump(0.2)
    s = app.summary
    check(app.screen == "done" and s["kind"] == "warn"
          and s["title"] == "31 of 33 fetched · 2 failed" and s["actions"],
          "done: result band names the outcome")
    check([r.state for r in app.table.rows[:3]] == ["fail", "fail", "ok"],
          "failed tickers are listed first")
    check(len(app.recent) == 1, "the new report shows under Recent reports")
    app.copy_for_ai()
    check(app.status_kind == "ok", "Copy for AI")

    # -- theme switch rebuilds from state ----------------------------------------
    for name in ("light", "dark", "auto"):
        app.set_theme(name)
        pump(0.4)
        check(app.screen == "done" and app.summary is s and len(app.model.rows) == 33,
              f"theme '{name}' rebuilds the view and keeps the run")
    check(app.t.name in ("dark", "light"), "auto resolves to a real theme")

    # -- resize: the mosaic steps aside in a narrow window ------------------------
    root.geometry(f"{app.px(960)}x{app.px(560)}")
    pump(0.4)
    check(app.mosaic is None, "narrow window: the band drops the mosaic")
    root.geometry(f"{app.px(1240)}x{app.px(760)}")
    pump(0.4)

    # -- finish early / cancel ---------------------------------------------------
    gate.clear()
    app.start()
    pump(5, until=lambda: len(app.model.rows) >= 33)
    app.finish_early()
    gate.set()
    pump(8, until=lambda: not app.running)
    check(app.summary["title"].endswith("finished early"), "Finish now writes a partial report")

    # -- a run that writes no report ---------------------------------------------
    mode["bad"] = True
    app.start()
    pump(8, until=lambda: not app.running)
    pump(0.2)
    check(app.summary["kind"] == "bad" and app.log_open and not app.summary["actions"],
          "no report: says so and opens the log")
    app.set_mode("curated")
    pump(0.2)
    check(app.screen == "idle" and app.status_var.get() == gb.READY and not app.log_open,
          "choosing another list returns to a clean idle state")

    for seq in ("<F5>",):                       # the keyboard shortcut starts a run too
        mode["bad"] = False
        gate.set()
        root.event_generate(seq)
        pump(8, until=lambda: not app.running and app.screen == "done")
    check(app.screen == "done", "F5 runs")

    app.on_close()
    assert not errors, "exception in a Tk callback:\n" + "\n".join(errors)
    saved = gb.load_board_settings()
    assert saved.get("mode") == "curated" and saved.get("theme") == "auto", saved
    print("\nLive board window test passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
