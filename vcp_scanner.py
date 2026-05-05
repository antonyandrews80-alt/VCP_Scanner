"""
VCP Daily Scanner — CSV-based Version
Your local script downloads Chartink CSV and pushes to GitHub repo.
GitHub Actions reads today's CSV and runs the full scanner pipeline.

Expected CSV filename in repo root:
  YYYY-MM-DD_All_Scans.csv

CSV format: Date, Symbol, Mcap, Change, Dpower, Mpower, Sector
"""

import os
import json
import time
import requests
import warnings
import pandas as pd
import numpy as np
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
TOP_N_PICKS                = 10
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
  "rs_rating": <integer 1-99>,
  "entry_zone": "<price range e.g. 245-252>",
  "pivot_level": <float>,
  "stop_loss": <float>,
  "target_10pct": <float>,
  "target_20pct": <float>,
  "target_30pct": <float>,
  "vcp_stage": "<early|mid|late>",
  "vcp_pivots": <integer 1-5>,
  "why_this_stock": "<2 sentence max>",
  "buy_reasons": ["<reason 1>", "<reason 2>", "<reason 3>"],
  "hold_signal": "<1 sentence — what to watch to stay in trade>",
  "exit_signal": "<1 sentence — what triggers the exit>",
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

def score_bar(score):
    """Visual score bar out of 10 blocks."""
    filled = round(score / 10)
    return "█" * filled + "░" * (10 - filled)


def format_header(today, total_picks, total_passed):
    """Send a clean header message first."""
    return (
        f"┌─────────────────────────────┐\n"
        f"│  📊 <b>VCP SCANNER REPORT</b>       │\n"
        f"│  📅 {today:<24}│\n"
        f"│  ✅ {total_passed} passed filters          │\n"
        f"│  🏆 {total_picks} top picks selected      │\n"
        f"└─────────────────────────────┘\n"
        f"\n<i>Minervini VCP + Claude AI scoring</i>"
    )


def format_pick_message(pick, rank, total):
    """Format a single stock pick as a rich Telegram message."""

    # Risk:Reward
    rr = round(
        (pick['target_20pct'] - pick['pivot_level']) / max(pick['pivot_level'] - pick['stop_loss'], 1), 1
    )

    # Sanitise all text fields
    def safe(text):
        return str(text).replace('<', '&lt;').replace('>', '&gt;')

    entry       = safe(pick['entry_zone'])
    risk        = safe(pick['key_risk'])
    hold_signal = safe(pick.get('hold_signal', 'Stay above stop loss at all times'))
    exit_signal = safe(pick.get('exit_signal', 'Exit if daily close below stop loss'))
    buy_reasons = pick.get('buy_reasons', [])
    sector      = pick.get('sector', 'NSE').title()
    score       = pick['score']
    rs_rating   = pick.get('rs_rating', '—')
    vcp_pivots  = pick.get('vcp_pivots', '—')
    stage       = str(pick['vcp_stage']).upper()

    # Rank medal
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    medal  = medals.get(rank, f"#{rank}")

    # Stage emoji
    stage_emoji = {"EARLY": "🌱", "MID": "📈", "LATE": "🔥"}.get(stage, "📊")

    # Score colour indicator
    if score >= 80:
        score_icon = "🟢"
    elif score >= 65:
        score_icon = "🟡"
    else:
        score_icon = "🔴"

    # Score bar
    bar = score_bar(score)

    # Buy reasons as bullet points (max 3)
    if buy_reasons:
        reasons_text = "\n".join([f"  · {safe(r)}" for r in buy_reasons[:3]])
    else:
        reasons_text = f"  · {safe(pick.get('why_this_stock', '—'))}"

    msg = (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{medal} <b>{pick['symbol']}</b>  {score_icon}  {stage_emoji} {stage} VCP\n"
        f"📂 {sector}  ·  Pivots: {vcp_pivots}\n"
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"\n"
        f"<b>Score:</b> {score}/100  {bar}\n"
        f"<b>RS Rating:</b> {rs_rating}  ·  <b>R:R</b> 1:{rr}  ·  <b>Hold:</b> ~{pick['hold_days_estimate']}d\n"
        f"\n"
        f"<b>💰 Trade Setup</b>\n"
        f"  Entry Zone  :  ₹{entry}\n"
        f"  Pivot Level :  ₹{pick['pivot_level']}\n"
        f"  Stop Loss   :  ₹{pick['stop_loss']} 🛑\n"
        f"\n"
        f"<b>🎯 Targets</b>\n"
        f"  +10%  →  ₹{pick['target_10pct']}\n"
        f"  +20%  →  ₹{pick['target_20pct']}\n"
        f"  +30%  →  ₹{pick['target_30pct']}\n"
        f"\n"
        f"<b>✅ Why Buy:</b>\n"
        f"{reasons_text}\n"
        f"\n"
        f"<b>📌 Hold Signal:</b>\n"
        f"  {hold_signal}\n"
        f"\n"
        f"<b>🚪 Exit Signal:</b>\n"
        f"  {exit_signal}\n"
        f"\n"
        f"<b>⚠️ Risk:</b> {risk}\n"
    )
    return msg


def format_footer(total_picks):
    return (
        f"━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"<i>🤖 Powered by Minervini VCP + Claude AI</i>\n"
        f"<i>🛑 Hard stop -7% from entry. No exceptions.</i>\n"
        f"<i>📌 Do your own research before trading.</i>"
    )


# ============================================================
# MAIN
# ============================================================

def main():
    now_ist = datetime.now(IST)
    print(f"\n{'='*50}")
    print(f"VCP Scanner starting at {now_ist.strftime('%Y-%m-%d %H:%M IST')}")
    print(f"{'='*50}\n")

    notify(f"🔍 <b>VCP Scanner</b> started\n📅 {now_ist.strftime('%d %b %Y')}&nbsp; ⏰ {now_ist.strftime('%H:%M IST')}")

    # --- Load stocks from CSV ---
    print("Loading Chartink CSV...")
    stocks, csv_date = load_csv()

    if not stocks:
        send_telegram(
            f"⚠️ <b>VCP Scanner — {now_ist.strftime('%d %b %Y')}</b>\n\n"
            f"No CSV file found in repo.\n"
            f"Please run <code>github_push.py</code> to upload today's Chartink data."
        )
        return

    print(f"\nCSV date: {csv_date} | Stocks: {len(stocks)}")
    candidates  = [s['symbol'] for s in stocks]
    sector_map  = {s['symbol']: s['sector'] for s in stocks}
    source_map  = {s['symbol']: s['source'] for s in stocks}

    # --- Load Nifty 50 from pre-fetched CSV ---
    print("\nLoading Nifty 50...")
    nifty = pd.DataFrame(columns=['Open','High','Low','Close','Volume'])
    nifty_file = f"{csv_date}_NIFTY50.csv"
    if os.path.exists(nifty_file):
        raw = pd.read_csv(nifty_file, parse_dates=['Date'])
        raw = raw.set_index('Date').sort_index()
        nifty = raw[['Open','High','Low','Close','Volume']].dropna()
        print(f"  Nifty: {len(nifty)} days loaded from {nifty_file}")
    else:
        print(f"  WARNING: {nifty_file} not found — RS scores will be zero")

    # --- Load pre-fetched stock price data from prices/ folder ---
    print(f"\nLoading price data for {len(candidates)} stocks...")
    stock_data = {}
    for sym in candidates:
        price_file = f"prices/{csv_date}_{sym}.csv"
        if os.path.exists(price_file):
            try:
                df = pd.read_csv(price_file, parse_dates=['Date'])
                df = df.set_index('Date').sort_index()
                df = df[['Open','High','Low','Close','Volume']].dropna()
                if len(df) >= 30:
                    stock_data[sym] = df
                    print(f"  {sym}: {len(df)} days OK")
                else:
                    print(f"  {sym}: only {len(df)} days — skipping")
            except Exception as e:
                print(f"  {sym}: read error — {e}")
        else:
            print(f"  {sym}: no price file found ({price_file})")

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
            f"📉 <b>VCP Scanner — {now_ist.strftime('%d %b %Y')}</b>\n\n"
            f"Checked <b>{checked}</b> stocks — none passed Minervini filters today.\n\n"
            f"<i>Market may be in a weak phase. Check again tomorrow.</i>"
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

    # --- Send to Telegram — one message per stock + header/footer ---
    today = datetime.now(IST).strftime('%d %b %Y')

    # 1. Send header
    header = format_header(today, len(final_picks), len(shortlist))
    r = send_telegram(header)
    print(f"Header sent: {r.get('ok')}")
    time.sleep(0.8)

    # 2. Send each pick as individual message
    for rank, pick in enumerate(final_picks, 1):
        msg = format_pick_message(pick, rank, len(final_picks))
        r = send_telegram(msg)
        print(f"Pick {rank} ({pick['symbol']}) sent: {r.get('ok')}")
        time.sleep(0.8)

    # 3. Send footer
    footer = format_footer(len(final_picks))
    r = send_telegram(footer)
    print(f"Footer sent: {r.get('ok')}")

    print(f"\nDone at {datetime.now(IST).strftime('%H:%M IST')}")
    print("=" * 50)


if __name__ == "__main__":
    main()

