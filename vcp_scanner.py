"""
VCP Daily Scanner — CSV-based Version
Your local script downloads Chartink CSV and pushes to GitHub repo.
GitHub Actions reads today's CSV and runs the full scanner pipeline.

Expected CSV filename in repo root:
  YYYY-MM-DD_Screener_1.csv

CSV format: Date, Symbol, Mcap, Change, Dpower, Mpower, Sector
"""

import os
import json
import time
import requests
import warnings
import pandas as pd
import numpy as np
import yfinance as yf
import anthropic
from datetime import datetime, timedelta
import pytz

warnings.filterwarnings('ignore')

# ============================================================
# CONFIGURATION — from GitHub Secrets
# ============================================================

CLAUDE_API_KEY   = os.environ.get("CLAUDE_API_KEY", "")
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")

# Filter settings
TOP_N_PICKS                = 2
MAX_BASE_DEPTH_PCT         = 40
MIN_RS_RISE_DAYS           = 50
BREAKOUT_VOLUME_MULTIPLIER = 2.0
MIN_RULES_TO_PASS          = 6

IST = pytz.timezone("Asia/Kolkata")


# ============================================================
# STEP 1 — TELEGRAM
# ============================================================

def send_telegram(message, parse_mode="HTML"):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": parse_mode}
    try:
        r = requests.post(url, data=payload, timeout=10)
        return r.json()
    except Exception as e:
        print(f"Telegram error: {e}")
        return {}

def notify(msg):
    print(msg)
    send_telegram(msg)


# ============================================================
# STEP 2 — LOAD SYMBOLS FROM CSV
# ============================================================

def load_csv():
    """
    Load all stocks from today's combined Chartink CSV.
    Filename: YYYY-MM-DD_All_Scans.csv
    Processes ALL stocks across all scanner sources.
    Falls back up to 4 days to handle weekends/holidays.
    Returns list of dicts with symbol, sector, source.
    """
    now_ist = datetime.now(IST)

    for days_back in range(5):
        date_str = (now_ist - timedelta(days=days_back)).strftime('%Y-%m-%d')
        filename = f"{date_str}_All_Scans.csv"

        for path in [filename, f"data/{filename}", f"csv/{filename}"]:
            if os.path.exists(path):
                print(f"  Loading: {path}")
                df = pd.read_csv(path, encoding='utf-8-sig')
                df.columns = [c.strip() for c in df.columns]
                print(f"  Columns: {list(df.columns)}")
                print(f"  Total rows: {len(df)}")

                # Show scanner sources
                if 'Scanner_Source' in df.columns:
                    print(f"  Sources: {df['Scanner_Source'].value_counts().to_dict()}")

                # Find symbol column
                sym_col = next(
                    (c for c in df.columns if c.lower() in ['symbol','stock','ticker','scrip']),
                    df.columns[1]
                )

                # Sector data is split: 'Sector' for some rows, 'Sectors' for others
                sec_col1 = next((c for c in df.columns if c.strip().lower() == 'sector'), None)
                sec_col2 = next((c for c in df.columns if c.strip().lower() == 'sectors'), None)

                # Build deduplicated stock list (keep first occurrence of each symbol)
                seen = {}
                for _, row in df.iterrows():
                    sym = str(row[sym_col]).strip().upper()
                    # Clean symbol for yfinance (remove hyphens)
                    sym_clean = sym.replace('-', '').replace('&', 'AND')
                    if len(sym_clean) < 2 or sym_clean in ['NAN', 'SYMBOL', 'NONE']:
                        continue

                    # Get sector from whichever column has a value
                    sec = 'NSE'
                    for sc in [sec_col1, sec_col2]:
                        if sc and sc in row:
                            val = str(row[sc]).strip()
                            if val and val.lower() not in ['nan', 'none', '']:
                                sec = val.capitalize()
                                break

                    src = str(row.get('Scanner_Source', '')).strip()

                    if sym_clean not in seen:
                        seen[sym_clean] = {
                            'symbol': sym_clean,
                            'original': sym,
                            'sector': sec,
                            'source': src
                        }

                stocks = list(seen.values())
                print(f"  Loaded {len(stocks)} unique stocks:")
                for s in stocks:
                    print(f"    {s['symbol']} | {s['sector']} | {s['source']}")
                return stocks, date_str

    print("  No CSV file found in last 5 days")
    return [], None

# ============================================================
# STEP 3 — TECHNICAL INDICATORS
# ============================================================

def compute_indicators(df, nifty_df):
    """Compute all Minervini indicators — matches Colab exactly."""
    d = {}
    close  = df['Close'].squeeze()
    volume = df['Volume'].squeeze()
    high   = df['High'].squeeze()
    low    = df['Low'].squeeze()

    # Moving averages
    d['sma50']          = close.rolling(50).mean().iloc[-1]
    d['sma150']         = close.rolling(150).mean().iloc[-1]
    d['sma200']         = close.rolling(200).mean().iloc[-1]
    d['sma200_20d_ago'] = close.rolling(200).mean().iloc[-20]
    d['current_price']  = close.iloc[-1]

    # Stage 2 MA stack
    d['ma_stack_ok'] = (
        d['current_price'] > d['sma50'] and
        d['sma50']  > d['sma150'] and
        d['sma150'] > d['sma200'] and
        d['sma200'] > d['sma200_20d_ago']
    )

    # 52-week metrics
    d['high_52w']       = high.iloc[-252:].max() if len(high) >= 252 else high.max()
    d['low_52w']        = low.iloc[-252:].min()  if len(low)  >= 252 else low.min()
    d['base_depth_pct'] = round((d['high_52w'] - d['low_52w']) / d['high_52w'] * 100, 1)
    d['pct_from_high']  = round((d['high_52w'] - d['current_price']) / d['high_52w'] * 100, 1)
    d['base_depth_ok']  = d['base_depth_pct'] < MAX_BASE_DEPTH_PCT
    d['near_high_ok']   = d['pct_from_high'] < 15

    # Volume contraction
    vol_20d = volume.iloc[-20:].mean()
    vol_60d = volume.iloc[-60:-20].mean() if len(volume) >= 60 else volume.mean()
    d['vol_20d_avg']         = round(vol_20d)
    d['vol_60d_avg']         = round(vol_60d)
    d['vol_contraction_pct'] = round((vol_60d - vol_20d) / vol_60d * 100, 1) if vol_60d > 0 else 0
    d['vol_contraction_ok']  = vol_20d < vol_60d

    # Breakout volume surge
    d['recent_vol_ratio'] = round(volume.iloc[-3:].max() / vol_20d, 2) if vol_20d > 0 else 0
    d['vol_breakout_ok']  = d['recent_vol_ratio'] >= BREAKOUT_VOLUME_MULTIPLIER

    # RS ratio vs Nifty
    try:
        stock_a, nifty_a = close.align(nifty_df['Close'].squeeze(), join='inner')
        rs = stock_a / nifty_a
        if len(rs) >= MIN_RS_RISE_DAYS:
            d['rs_change_pct'] = round((rs.iloc[-1] - rs.iloc[-MIN_RS_RISE_DAYS]) / rs.iloc[-MIN_RS_RISE_DAYS] * 100, 1)
            d['rs_ok'] = d['rs_change_pct'] > 0
        else:
            d['rs_change_pct'] = 0
            d['rs_ok'] = False
    except Exception:
        d['rs_change_pct'] = 0
        d['rs_ok'] = False

    # Price contraction tightness (last 20 days)
    d['contraction_range_pct'] = round((high.iloc[-20:].max() - low.iloc[-20:].min()) / high.iloc[-20:].max() * 100, 1)
    d['contraction_ok']        = d['contraction_range_pct'] < 25

    # MACD cooling
    exp12 = close.ewm(span=12, adjust=False).mean()
    exp26 = close.ewm(span=26, adjust=False).mean()
    macd  = exp12 - exp26
    d['macd_line']         = round(macd.iloc[-1], 4)
    d['macd_pct_of_price'] = round(abs(d['macd_line']) / d['current_price'] * 100, 2)
    d['macd_cooling_ok']   = d['macd_pct_of_price'] < 3

    return d


def apply_filters(symbol, ind):
    """Apply all 8 Minervini rules."""
    rules = {
        'Stage 2 MA stack'        : ind['ma_stack_ok'],
        'Base depth < 40%'        : ind['base_depth_ok'],
        'Near 52W high (<15%)'    : ind['near_high_ok'],
        'Volume contraction'      : ind['vol_contraction_ok'],
        'RS vs Nifty rising'      : ind['rs_ok'],
        'Price contraction tight' : ind['contraction_ok'],
        'MACD cooling in base'    : ind['macd_cooling_ok'],
        'Vol surge on breakout'   : ind['vol_breakout_ok'],
    }
    count  = sum(rules.values())
    passed = count >= MIN_RULES_TO_PASS
    return passed, rules, count


# ============================================================
# STEP 4 — CLAUDE AI SCORING
# ============================================================

def score_with_claude(symbol, ind, rules):
    """Score a stock setup using Claude API."""
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    rules_text = "\n".join([f"  - {r}: {'PASS' if v else 'FAIL'}" for r, v in rules.items()])

    prompt = f"""You are a VCP (Volatility Contraction Pattern) specialist trained on Mark Minervini's methodology.

Score this NSE stock as a VCP breakout candidate. Return ONLY a JSON object, no other text.

Stock: {symbol}
Date: {datetime.now(IST).strftime('%Y-%m-%d')}

STOCK INDICATORS:
  Current price: {round(ind['current_price'], 2)}
  SMA 50: {round(ind['sma50'], 2)}
  SMA 150: {round(ind['sma150'], 2)}
  SMA 200: {round(ind['sma200'], 2)}
  52W High: {round(ind['high_52w'], 2)}
  52W Low: {round(ind['low_52w'], 2)}
  % from 52W high: {ind['pct_from_high']}%
  Base depth (52W high-low range): {ind['base_depth_pct']}%
  Price contraction range (20d): {ind['contraction_range_pct']}%
  Volume contraction (recent vs older): {ind['vol_contraction_pct']}% drier
  Recent volume ratio vs 20d avg: {ind['recent_vol_ratio']}x
  RS vs Nifty change ({MIN_RS_RISE_DAYS}d): {ind['rs_change_pct']}%
  MACD line % of price: {ind['macd_pct_of_price']}%

MINERVINI RULE RESULTS:
{rules_text}

SCORING INSTRUCTIONS:
Score the VCP setup quality from 0 to 100 using these weights:
  - VCP pattern quality (tightness, contractions, pivot clarity): 40%
  - RS strength vs Nifty: 30%
  - Volume pattern (drying in base, surge on breakout): 30%

Return ONLY this JSON (no markdown, no explanation):
{{
  "symbol": "{symbol}",
  "score": <integer 0-100>,
  "vcp_quality_score": <integer 0-40>,
  "rs_score": <integer 0-30>,
  "volume_score": <integer 0-30>,
  "entry_zone": "<price range e.g. 245-252>",
  "pivot_level": <float>,
  "stop_loss": <float>,
  "target_10pct": <float>,
  "target_20pct": <float>,
  "target_30pct": <float>,
  "vcp_stage": "<early|mid|late>",
  "why_this_stock": "<2 sentence max>",
  "key_risk": "<1 sentence>",
  "hold_days_estimate": <integer>
}}"""

    try:
        msg = client.messages.create(
            model="claude-haiku-4-5-20251001",
            max_tokens=1000,
            messages=[{"role": "user", "content": prompt}]
        )
        raw = msg.content[0].text.strip().replace('```json', '').replace('```', '').strip()
        return json.loads(raw)
    except Exception as e:
        print(f"  Claude error for {symbol}: {e}")
        return None


# ============================================================
# STEP 5 — FORMAT TELEGRAM MESSAGE
# ============================================================

def format_picks_message(picks):
    today = datetime.now(IST).strftime('%d %b %Y')
    msg = f"<b>VCP Daily Picks — {today}</b>\n\n"
    for i, p in enumerate(picks, 1):
        rr = round(
            (p['target_20pct'] - p['pivot_level']) / max(p['pivot_level'] - p['stop_loss'], 1), 1
        )
        msg += f"""<b>Pick {i}: {p['symbol']}</b> [{p.get('sector','NSE')}]
Score: {p['score']}/100 | Stage: {p['vcp_stage'].upper()} VCP
Entry zone : {p['entry_zone']}
Pivot level: {p['pivot_level']}
Stop loss  : {p['stop_loss']} (-7%)
Target 10% : {p['target_10pct']}
Target 20% : {p['target_20pct']}
Target 30% : {p['target_30pct']}
Hold est.  : {p['hold_days_estimate']} days | R:R 1:{rr}

Why: {p['why_this_stock']}
Risk: {p['key_risk']}

"""
    msg += "<i>System: Minervini VCP + Claude AI</i>\n"
    msg += "<i>Hard stop -7% from entry. No exceptions.</i>"
    return msg


# ============================================================
# MAIN
# ============================================================

def main():
    now_ist = datetime.now(IST)
    print(f"\n{'='*50}")
    print(f"VCP Scanner starting at {now_ist.strftime('%Y-%m-%d %H:%M IST')}")
    print(f"{'='*50}\n")

    notify(f"VCP Scanner started — {now_ist.strftime('%d %b %Y %H:%M IST')}")

    # --- Load stocks from CSV ---
    print("Loading Chartink CSV...")
    stocks, csv_date = load_csv()

    if not stocks:
        send_telegram(
            f"<b>VCP Scanner — {now_ist.strftime('%d %b %Y')}</b>\n\n"
            f"No CSV file found in repo.\n"
            f"Please push <code>YYYY-MM-DD_Screener_1.csv</code> to the repo before running."
        )
        return

    print(f"\nCSV date: {csv_date} | Stocks: {len(stocks)}")
    candidates  = [s['symbol'] for s in stocks]
    sector_map  = {s['symbol']: s['sector'] for s in stocks}
    source_map  = {s['symbol']: s['source'] for s in stocks}

    # --- Fetch Nifty 50 ---
    print("\nFetching Nifty 50...")
    nifty = yf.download('^NSEI', period='1y', interval='1d', progress=False, auto_adjust=True)
    nifty.columns = [c[0] if isinstance(c, tuple) else c for c in nifty.columns]
    print(f"Nifty: {len(nifty)} days")

    # --- Fetch stock data ---
    print(f"\nFetching price data for {len(candidates)} stocks...")
    stock_data = {}
    for sym in candidates:
        df = None
        # Try NSE first, then BSE as fallback
        for suffix in ['.NS', '.BO']:
            try:
                raw = yf.download(sym + suffix, period='1y', interval='1d', progress=False, auto_adjust=True)
                if len(raw) >= 30:
                    raw.columns = [c[0] if isinstance(c, tuple) else c for c in raw.columns]
                    df = raw
                    print(f"  {sym}: {len(df)} days OK ({suffix})")
                    break
                else:
                    print(f"  {sym}{suffix}: only {len(raw)} days")
            except Exception as e:
                print(f"  {sym}{suffix}: error — {e}")
        if df is None:
            print(f"  {sym}: no data from NSE or BSE — skipping")
        else:
            stock_data[sym] = df
        time.sleep(0.3)

    if not stock_data:
        tried = ', '.join(candidates)
        send_telegram(
            f"<b>VCP Scanner — {now_ist.strftime('%d %b %Y')}</b>\n\n"
            f"Could not fetch price data for any stock.\n"
            f"Stocks tried: {tried}\n"
            f"This may happen if symbols are not listed on NSE or yfinance returned no data."
        )
        return

    # --- Apply Minervini filters ---
    print(f"\nApplying Minervini filters ({MIN_RULES_TO_PASS}/8 rules required)...")
    shortlist = []
    for sym, df in stock_data.items():
        try:
            ind = compute_indicators(df, nifty)
            passed, rules, count = apply_filters(sym, ind)
            status = 'PASS ✓' if passed else f'FAIL  ({count}/8)'
            print(f"  {sym}: {status}")
            for name, val in rules.items():
                print(f"    {'✓' if val else '✗'} {name}")
            if passed:
                shortlist.append({
                    'symbol': sym,
                    'sector': sector_map.get(sym, 'NSE'),
                    'indicators': ind,
                    'rules': rules,
                    'rule_score': count
                })
        except Exception as e:
            print(f"  {sym}: error — {e}")

    print(f"\nShortlist: {len(shortlist)} stocks passed filters")

    if not shortlist:
        checked = len(stock_data)
        send_telegram(
            f"<b>VCP Scanner — {now_ist.strftime('%d %b %Y')}</b>\n\n"
            f"Checked {checked} stocks from Screener 1.\n"
            f"No stocks passed all Minervini filters today.\n\n"
            f"<i>Try again on next trading day.</i>"
        )
        return

    # --- Claude AI scoring ---
    print(f"\nScoring {len(shortlist)} stocks with Claude AI...")
    scored = []
    for item in shortlist:
        sym = item['symbol']
        print(f"  {sym}...", end=' ', flush=True)
        result = score_with_claude(sym, item['indicators'], item['rules'])
        if result:
            result['sector'] = item['sector']
            result['rule_score'] = item['rule_score']
            scored.append(result)
            print(f"{result['score']}/100")
        else:
            print("scoring failed")
        time.sleep(1)

    if not scored:
        send_telegram("VCP Scanner: Claude scoring failed for all stocks. Check CLAUDE_API_KEY.")
        return

    scored.sort(key=lambda x: x['score'], reverse=True)
    print(f"\nScoring complete:")
    for s in scored:
        print(f"  {s['symbol']}: {s['score']}/100 [{s['sector']}]")

    # --- Select top picks with sector diversification ---
    final_picks = []
    used_sectors = []
    for s in scored:
        if len(final_picks) >= TOP_N_PICKS:
            break
        if s['sector'] not in used_sectors or s['sector'] == 'NSE':
            final_picks.append(s)
            used_sectors.append(s['sector'])

    # If sector diversification leaves us short, just take top scores
    if len(final_picks) < TOP_N_PICKS and len(scored) >= TOP_N_PICKS:
        final_picks = scored[:TOP_N_PICKS]

    print(f"\nFinal {len(final_picks)} picks: {[p['symbol'] for p in final_picks]}")

    # --- Send to Telegram ---
    message = format_picks_message(final_picks)
    result = send_telegram(message)
    if result.get('ok'):
        print("Picks sent to Telegram!")
    else:
        print(f"Telegram send failed: {result}")

    print(f"\nDone at {datetime.now(IST).strftime('%H:%M IST')}")
    print("=" * 50)


if __name__ == "__main__":
    main()
