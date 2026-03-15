---
name: "market-move-analysis"
description: "Analyzes short-term market moves/市场走势 using fetched price/news data. Invoke for trend explanation, volatility context, event-driven analysis, or 股票走势分析 requests."
---

# Market Move Analysis

## Purpose
Analyze recent market movements for a ticker, index, sector, or theme using fetched data and event context. This skill is analytical and educational, not investment advice.

## Layer
14 — Market Analysis (Price + Event Decomposition)

## Direction
both

## When to Use
- User asks about 7/30/90-day price trends or unusual movements
- User asks why a stock or index moved sharply
- User needs a structured, evidence-based move decomposition
- User asks for benchmark-relative analysis and volatility regime context

## Input Expectations
- Instrument identifier (ticker/index)
- Time window
- At least one price or news source URL

## Analysis Framework
1. Price action:
   - absolute return and relative return vs benchmark
   - drawdown and rebound segments
   - realized volatility regime
2. Event linkage:
   - earnings, guidance, macro prints, policy, sector spillover
   - classify as confirmed, likely, or unconfirmed driver
3. Narrative quality checks:
   - avoid single-cause certainty
   - show alternative explanations
   - state data limitations

## Key Metrics
- Window return: (P_end / P_start) - 1
- Benchmark spread: asset return - benchmark return
- Max drawdown: minimum of (P_t / running_peak - 1)
- Realized volatility: std(log returns) × sqrt(annualization factor)
- Event coverage ratio: events with source-backed linkage / total claimed drivers

## Fetch URL Pack

### Price Series (CSV, fetch-friendly)
- Stooq daily PLTR: https://stooq.com/q/d/l/?s=pltr.us&i=d
- Stooq daily SPY: https://stooq.com/q/d/l/?s=spy.us&i=d
- Stooq daily QQQ: https://stooq.com/q/d/l/?s=qqq.us&i=d
- Stooq template: https://stooq.com/q/d/l/?s=<symbol>.us&i=d

### Volatility and Rates Context
- FRED VIX close CSV: https://fred.stlouisfed.org/graph/fredgraph.csv?id=VIXCLS
- FRED 10Y Treasury yield CSV: https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS10
- FRED 2Y Treasury yield CSV: https://fred.stlouisfed.org/graph/fredgraph.csv?id=DGS2

### Event Sources
- SEC filings feed (ticker example): https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=PLTR&owner=exclude&count=40&output=atom
- Federal Reserve press releases: https://www.federalreserve.gov/newsevents/pressreleases.htm
- BLS news releases: https://www.bls.gov/bls/news-release/home.htm

## Workflow
1. Pull instrument and benchmark price series for the same period
2. Compute return, spread, drawdown, and volatility metrics
3. Pull event sources and align event timestamps to move windows
4. Tag each driver as confirmed, likely, or unconfirmed
5. Produce final decomposition with alternatives and uncertainty notes

## Output Format
- Period Performance: <return, benchmark spread, max drawdown>
- Regime Notes: <trend, volatility, liquidity clues>
- Driver Matrix:
  - <event> | <directional impact> | <confidence> | <source>
- Scenario Watchlist:
  - <bull case signal>
  - <base case signal>
  - <bear case signal>
- Disclaimer: This is informational analysis, not investment advice.

## Worked Examples

### Example 1: 30-Day Single-Name Move
- Scope: PLTR vs SPY, last 30 calendar days
- Data:
  - Stooq PLTR CSV
  - Stooq SPY CSV
  - SEC filing feed + one macro source
- Output:
  - return/spread/drawdown metrics
  - event-linked driver matrix with confidence levels
  - explicit data gaps and alternative interpretation

### Example 2: Policy-Sensitive Sector Move
- Scope: growth-heavy tech basket around FOMC week
- Data:
  - QQQ price series
  - VIX + DGS10 from FRED
  - Fed press releases
- Output:
  - volatility regime shift summary
  - rate-sensitive driver assessment
  - scenario watchlist for follow-up monitoring

## Common Pitfalls
- Comparing assets with mismatched time windows or trading calendars
- Explaining every move with a single headline
- Ignoring benchmark drift and only reporting absolute return
- Omitting uncertainty labels for weak event linkage

## Cross-References
- finance-news-collector: use first when event coverage is sparse or source quality is unclear
