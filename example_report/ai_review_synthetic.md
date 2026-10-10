# Stock Analyzer - AI Review - 2026-07-16 (SYNTHETIC SAMPLE DATA)

universe=SYNTHETIC | scored=20/20 | generated=2026-07-16 | sectors=Consumer Cyclical, Consumer Defensive, Energy, Financial Services, Healthcare, Industrials, Technology | by size=Large 20 (market cap, USD) | median trailing P/E=21.0 | median 6m return=+12.0%

> SYNTHETIC fixture data - fabricated inputs for a deterministic demo, NOT real market data.

> NOT FINANCIAL ADVICE. Deterministic same-day cross-sectional ranking of the configured universe - no AI model produced any number. A high score is a relative rank, not a forecast or buy signal, and is unproven pending the backtest. Data is best-effort from yfinance (unofficial); missing fields are blank, never fabricated.

## How to read

One CSV row per stock, ranked best first. Every score/percentile column is 0-100 where **higher is always better** (each metric's direction is already baked in); **blank = data unavailable**. A row with many missing metrics for this stock shows a long run of bare consecutive commas - that's expected, not a misaligned row (see the sparse-row note below the table for anything unusually thin). Reason ONLY from these numbers.

**Your job:** name the strongest and weakest stocks and explain *why* from the columns; flag likely data bugs (e.g. a top-ranked name with a glaring weak metric, or contradictory columns); note where missing data weakens a conclusion. This is a research screen, not advice.

**score** = weighted blend of the category scores (fundamentals 35%, technicals 30%, momentum 15%, analyst 10%, sentiment 10%). Fundamentals are ranked within GICS sector; technicals/momentum/analyst across the whole universe. Missing metrics are dropped and the weights renormalised (never zero-filled).

## Columns

`rank,ticker,sector` identify the stock. `score` is the composite 0-100. Category scores 0-100: `fundamentals`, `technicals`, `momentum`, `analyst`. `cov` = number of metrics with data. The remaining columns are each metric's 0-100 percentile (100 = best in its ranking group):

- **fundamentals** (each metric weighted equally): `revenue_growth`=Revenue growth (yoy); `earnings_growth`=Earnings growth (yoy); `pe`=Trailing P/E; `forward_pe`=Forward P/E; `peg`=PEG ratio; `gross_margin`=Gross margin; `operating_margin`=Operating margin; `net_margin`=Net margin; `roe`=Return on equity; `roa`=Return on assets; `debt_to_equity`=Debt / equity; `fcf_yield`=Free cash flow yield; `cash_to_marketcap`=Cash / market cap
- **technicals** (each metric weighted equally): `price_vs_sma50`=Price vs 50d MA; `price_vs_sma200`=Price vs 200d MA; `trend_alignment`=Trend alignment; `rsi_health`=RSI neutrality; `macd_hist`=MACD histogram
- **momentum** (each metric weighted equally): `return_1m`=1-month return; `return_3m`=3-month return; `return_6m`=6-month return; `proximity_52w_high`=Proximity to 52w high; `relative_volume`=Relative volume
- **analyst** (each metric weighted equally): `recommendation_mean`=Consensus rating; `target_upside`=Target upside; `net_upgrades_90d`=Net upgrades (90d)

_Metrics dropped today (too few data points to rank): news_sentiment, social_sentiment._

## Stocks (20 ranked, CSV)

```csv
rank,ticker,sector,score,fundamentals,technicals,momentum,analyst,cov,revenue_growth,earnings_growth,pe,forward_pe,peg,gross_margin,operating_margin,net_margin,roe,roa,debt_to_equity,fcf_yield,cash_to_marketcap,price_vs_sma50,price_vs_sma200,trend_alignment,rsi_health,macd_hist,return_1m,return_3m,return_6m,proximity_52w_high,relative_volume,recommendation_mean,target_upside,net_upgrades_90d
1,SYNTH01,Technology,78.6,78.5,77.5,78.5,82.5,26,100,100,40,40,100,100,100,100,100,60,60,60,60,100,100,65,65,57,55,95,95,95,52,100,47,100
2,SYNTH02,Technology,68.5,71.1,73.5,63.5,51.8,26,80,80,80,60,94,80,50,80,80,60,60,60,60,90,90,65,65,57,55,60,90,60,52,55,53,48
3,SYNTH14,Consumer Cyclical,66.4,65.6,71,67.5,53.6,26,85,90,37,42,88,79,55,60,85,62,65,53,52,85,82,65,65,57,55,90,85,55,52,55,58,48
4,SYNTH11,Healthcare,65.3,67.9,66.5,59.5,61.4,26,75,80,53,52,75,95,55,85,80,62,65,53,52,75,70,65,65,57,55,60,50,80,52,95,42,48
5,SYNTH06,Financial Services,65.3,65.4,70,60.5,57.9,25,70,85,79,80,81,,100,98,52,20,15,53,52,80,82,65,65,57,55,60,50,85,52,89,37,48
6,SYNTH04,Technology,59.4,61.7,65,57.5,37.8,25,60,60,100,80,,30,50,60,60,60,60,60,60,68,70,65,65,57,55,60,50,70,52,55,11,48
7,SYNTH18,Industrials,56.4,50.2,65,67.5,36,26,62,75,58,62,69,11,10,42,60,62,35,53,52,68,70,65,65,57,55,60,80,90,52,55,5,48
8,SYNTH13,Healthcare,55.2,27.3,72,90.5,49.2,24,100,5,,5,,47,5,5,5,5,65,5,52,95,95,65,5,100,100,100,100,100,52,21,32,95
9,SYNTH07,Financial Services,55.1,53.8,57.5,48.5,62.3,24,55,60,84,90,,47,55,90,32,15,10,,52,50,50,65,65,57,55,60,50,25,52,55,84,48
10,SYNTH05,Technology,53.5,52.4,54.5,51.5,57.1,26,40,40,60,100,41,30,50,40,40,60,60,60,60,42,42,65,65,57,55,60,50,40,52,55,68,48
11,SYNTH09,Energy,53.4,54.4,61.5,50.5,29.9,25,10,15,89,85,,5,15,52,65,62,100,100,52,60,60,65,65,57,55,60,20,65,52,16,26,48
12,SYNTH12,Healthcare,52.6,54.7,51.5,53.5,47.5,24,38,40,68,72,41,47,55,65,52,62,65,53,52,35,35,65,65,57,55,60,50,50,52,,,48
13,SYNTH17,Consumer Defensive,50.9,45.8,54.5,52.5,55.3,26,28,50,45,30,41,47,55,35,72,62,25,53,52,42,42,65,65,57,55,60,50,45,52,55,63,48
14,SYNTH19,Industrials,50,50.1,49.5,43.5,60.6,26,38,45,63,62,41,47,55,28,40,62,65,53,52,30,30,65,65,57,55,30,50,30,52,55,79,48
15,SYNTH16,Consumer Defensive,49.7,44.8,59.5,58.5,24.6,26,48,55,29,22,12,16,55,52,95,62,30,53,52,55,55,65,65,57,55,60,50,75,52,11,16,48
16,SYNTH08,Financial Services,44.3,46.6,37.5,38.5,65.8,26,28,35,95,95,41,47,55,75,15,10,5,53,52,25,25,15,65,57,55,25,50,10,52,55,95,48
17,SYNTH15,Consumer Cyclical,37.9,39.2,27.5,38.5,64.1,26,20,30,21,22,41,47,55,20,20,62,65,53,52,20,20,15,25,57,55,20,50,15,52,55,89,48
18,SYNTH10,Energy,33,52.4,8,20.5,58.8,26,5,10,100,100,41,47,55,28,32,62,95,53,52,5,5,15,10,5,5,5,5,35,52,55,74,48
19,SYNTH20,Industrials,32.6,41.1,13,28.5,67.6,26,15,20,74,72,41,47,55,12,10,62,20,53,52,10,10,15,15,15,55,15,15,5,52,55,100,48
20,SYNTH03,Technology,25.4,36.6,15,20.5,24.6,26,20,20,20,20,6,60,50,20,20,60,60,60,60,15,15,15,20,10,10,10,10,20,52,5,21,48
```

## Backtest status

Synthetic fixture: no real prices, so no backtest. Use real daily runs + scripts/run_backtest.py.
