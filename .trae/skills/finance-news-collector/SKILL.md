---
name: "finance-news-collector"
description: "Collects and structures finance news/金融资讯 from authoritative sources. Invoke when users ask for market/company/regulatory updates, 财经信息搜集, or source-grounded summaries."
---

# Finance News Collector

## Purpose
Collect, filter, and summarize financial information from source URLs with traceable evidence. This skill is for informational analysis only and does not provide investment advice.

## Layer
13 — Data Integration (News/Event Intelligence)

## Direction
both

## When to Use
- User asks for recent updates about a company, ticker, sector, macro event, or policy change
- User asks to gather evidence from filings, earnings materials, central-bank releases, or exchange notices
- User asks for timeline-style market intelligence with source links
- User asks for structured news briefings with confidence tagging and source hierarchy

## Core Concepts

### 1. Evidence First, Narrative Second
- Always collect primary evidence before generating interpretation
- Keep each fact tied to a source URL and publication timestamp
- Mark confidence as high/medium/low based on source authority and corroboration

### 2. Source Hierarchy
- Primary: regulator filings, issuer IR releases, central-bank statements, official statistics pages
- Secondary: reputable financial media and established data vendors
- Tertiary: commentary, blogs, opinion pieces

### 3. Time Normalization
- Convert all timestamps to a single timezone before building the timeline
- Separate publication time from event effective time
- Highlight stale information when publication lag is material

### 4. Signal Classification
- Corporate: earnings, guidance, management change, M&A, major contracts
- Macro: inflation, employment, rates, policy communications
- Market structure: exchange notices, index rebalancing, trading halts
- Regulatory: enforcement, filing updates, disclosure changes


## Output Rules
- Use only verifiable source content
- Label each item with date, source, and key fact
- Separate facts from interpretation
- Explicitly state uncertainty if source evidence is incomplete
- Do not provide buy/sell/hold instructions or target prices
- Include this statement at the end: "This is informational analysis, not investment advice."

## Recommended Source Priority
1. Primary sources: regulator filings, issuer investor-relations pages, exchange/bourse notices, central-bank publications
2. Secondary sources: reputable financial media and data vendors

## Workflow
1. Define scope: entity, period, geography, and topic tags
2. Build source list with priority order
3. Fetch source URLs and extract material facts
4. De-duplicate overlapping reports
5. Produce:
   - event timeline
   - fact table
   - open questions and data gaps

## Fetch URL Pack

### Regulatory and Filings
- SEC submissions JSON by CIK: https://data.sec.gov/submissions/CIK0001321655.json
- SEC company facts XBRL JSON by CIK: https://data.sec.gov/api/xbrl/companyfacts/CIK0001321655.json
- SEC company filing feed (Atom, by ticker): https://www.sec.gov/cgi-bin/browse-edgar?action=getcompany&CIK=PLTR&owner=exclude&count=40&output=atom

### Company IR and Corporate Updates
- Palantir investor relations main page: https://investors.palantir.com/
- Nasdaq company news page (ticker example): https://www.nasdaq.com/market-activity/stocks/pltr/news-headlines

### Macro and Policy Sources
- Federal Reserve press releases: https://www.federalreserve.gov/newsevents/pressreleases.htm
- FOMC calendars and statements: https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm
- BLS news releases: https://www.bls.gov/bls/news-release/home.htm
- BEA news releases: https://www.bea.gov/news
- ECB press releases: https://www.ecb.europa.eu/press/pr/html/index.en.html

### Market Status and Exchange Notices
- NYSE trader updates and notices: https://www.nyse.com/markets/hours-calendars
- Nasdaq market status: https://www.nasdaqtrader.com/Trader.aspx?id=MarketSystemStatus

### URL Templates
- SEC submissions template: https://data.sec.gov/submissions/CIK<10_DIGIT_CIK>.json
- SEC facts template: https://data.sec.gov/api/xbrl/companyfacts/CIK<10_DIGIT_CIK>.json
- Stooq EOD CSV template: https://stooq.com/q/d/l/?s=<symbol>.us&i=d

## Response Template
- Scope: <company/ticker/topic + period>
- Timeline:
  - <date> | <source> | <fact>
- Key Signals:
  - <signal 1>
  - <signal 2>
- Risks / Unknowns:
  - <unknown 1>
- Sources:
  - <url 1>
  - <url 2>
- Disclaimer: This is informational analysis, not investment advice.

## Worked Examples

### Example 1: Company Event Intelligence
- Scope: PLTR, last 30 days, U.S. equities
- Steps:
  - Pull SEC filings feed and company IR page
  - Pull one secondary source for cross-check
  - Build timeline with earnings date, guidance comments, and material announcements
- Deliverable:
  - 6 to 12 timeline bullets
  - 3 to 5 key signals
  - explicit unknowns section

### Example 2: Macro Shock Context
- Scope: U.S. inflation surprise week
- Steps:
  - Pull BLS release page, Federal Reserve statement page, and one market-status source
  - Tag each item as macro release, policy response, or market structure update
- Deliverable:
  - chronological event map
  - confidence tag for each causal claim

## Common Pitfalls
- Treating headlines as facts without reading the primary document
- Mixing timezone references and creating incorrect event sequencing
- Using only one source family and missing contradictory evidence
- Presenting causal certainty when only correlation is available

## Cross-References
- market-move-analysis: use this after collection to produce movement decomposition
