STOCK REVIEW 2.3 - WHAT IS THIS THING?
=======================================
by NXT

Two explanations below: a short one and a long one. Read whichever
suits your mood. (How to install it: see INSTALL.txt.)


=======================================================================
THE SIMPLE VERSION
=======================================================================

Stock Review looks up stocks for you.

You pick a group of companies - the ASX 200, the S&P 500, a whole
market like Japan, or just your own list like "AAPL MSFT BHP.AX".
You click the big green Run button. The app downloads the latest
numbers for every company from Yahoo Finance and writes them all into
ONE tidy report file.

For every company, the report shows things like:

  * the current price and how big the company is
  * how expensive the stock is compared to its earnings
  * how fast the company is growing and whether it makes good money
  * whether it pays dividends
  * how the share price has been trending lately
  * what professional analysts think it's worth

The report is deliberately built so you can drag the file into an AI
chat (whichever one you like) and ask something like
"which of these look interesting and why?" - the AI does the reading,
you do the deciding.

While it works, you get a little MS-DOS window full of ASCII-art
scenes - a rocket, a whale, a bank vault, a container ship...
There are 13. Double-click the DOS title bar to change channels.

It's free, it runs entirely on your own computer, and nothing is sent
anywhere except the requests to Yahoo for prices.

IMPORTANT: it is NOT financial advice. It's a research toy that
gathers numbers neatly. What you do with them is up to you (and
ideally a licensed human adviser).


=======================================================================
THE DETAILED VERSION
=======================================================================

WHAT IT ACTUALLY DOES, STEP BY STEP
------------------------------------
1. You choose a "universe" - which stocks to review:
     * Curated default list  - a hand-picked starter watchlist
     * Region / source       - a named index or market: sp500, sp400,
                               sp600, sp1500, australia (= ASX 200),
                               asx-all, japan, uk, germany, hongkong,
                               canada, india, small-cap sleeves, or
                               combos like global / international /
                               everything
     * Sector                - e.g. mining or technology, optionally
                               pinned to one region
     * Custom tickers        - your own list, typed in. Exchange
                               suffixes work: BHP.AX, 7203.T, RIO.L
     * From a file           - a .txt (one ticker per line) or a .csv
                               with a "ticker" column
   The grey line under the form translates whatever you picked into
   plain English, and the "Quick picks" menu fills the whole form in
   one click.

2. It fetches every ticker from Yahoo Finance (via the yfinance
   library), several in parallel. For each company it collects:
     * identity   - name, sector, industry, listing currency
     * size       - market cap, converted to USD so every market is
                    comparable, plus a size bucket (Mega/Large/Mid/
                    Small/Micro)
     * valuation  - P/E, forward P/E, PEG, price/book, price/sales
     * quality    - return on equity, profit margin, revenue growth,
                    earnings growth, debt/equity, dividend yield
     * momentum   - 1/3/6/12-month returns, price vs the 50- and
                    200-day moving averages, distance from the
                    52-week high and low, beta
     * analysts   - consensus rating, price target, implied upside,
                    and how many analysts cover it

3. It writes ONE report file, date-stamped, in your chosen format:
     * md  (default) - a Markdown report with a compact data grid,
                       a column legend, run notes, and a list of
                       anything that couldn't be fetched. Optimised
                       to paste into an AI chat.
     * csv           - just the raw data grid, for Excel/scripts.
     * json          - the records as structured data.
   "Rich" mode makes the md human-friendly instead ($3.2T, +12.6%),
   and "Drop company names" shrinks it further for huge runs.

THE HONESTY RULE
-----------------
The app NEVER invents a number. If Yahoo doesn't have a value, the
cell says "n/a" rather than guessing. If a ticker can't be fetched at
all, it's listed openly in a "could not fetch" section instead of
quietly disappearing. There is no AI inside the app itself - the same
inputs always produce the same report.

HOW IT SURVIVES THE REAL WORLD
-------------------------------
Yahoo rate-limits big runs. The app expects that:
  * Failed tickers are retried automatically with a polite, growing
    pause - and you can SEE the retries happening (yellow marks on
    the ticker tape, a retries counter, lines in the Details log).
  * After the first pass it runs extra gentle "retry rounds" for
    whatever is still missing.
  * Every success is cached for the day. Run the SAME setup again a
    few minutes later and it resumes - cached names are skipped, so
    the gaps fill in instead of starting over.
  * Cancel (Esc) is safe: everything fetched so far is still written
    out as a clearly-marked partial report.

THE TWO PROGRAMS
-----------------
  * Stock Review            - the window. Pick, run, read. This is
                              the one you want 99% of the time.
  * Stock Review (Terminal) - the exact same engine in a console with
                              command-line flags (--source, --sector,
                              --limit, --workers, --format, --out,
                              --rich, ...) and the ASCII animations
                              rendered right in the terminal. For
                              power users and scripts.

LITTLE LUXURIES
----------------
Hover tooltips on every control · Quick picks menu · a live preview
of exactly where the next report lands · File > Recent reports ·
partial-report-safe Cancel · a run summary when it finishes ("198 ok,
2 failed, 3:42") · 13 ASCII channels (double-click the DOS bar to
surf) · Crystal Ball fortunes (Ctrl+8) · a lifetime odometer in the
About box · Tip of the Day · period-correct completion sounds.

FILES AND PRIVACY
------------------
Everything lives in Documents\Stock Review (reports, settings, the
daily cache). The app talks to exactly one thing on the internet:
Yahoo Finance, to fetch the numbers. No accounts, no telemetry, no
data leaves your machine.

THE FINE PRINT
---------------
Automated research screen, NOT financial advice. Prices and
fundamentals come from Yahoo Finance and can be wrong, delayed or
missing; always verify before acting. Made for fun by NXT.
