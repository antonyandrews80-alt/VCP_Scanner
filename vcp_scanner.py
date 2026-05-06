"""
VCP Daily Scanner v3 — with Sector Boost
- Loads Chartink All_Scans CSV for stock list
- Loads Preferred_Sectors CSV from sectors/ folder (separate from stocks)
- Applies Minervini 8-rule filter
- Scores with Claude AI
- Applies sector performance boost to final ranking
- Sends rich formatted picks to Telegram
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
# SECTOR MAP — Stock sector names → Chartink index symbols
# Maps All_Scans.csv sector values to Preferred_Sectors.csv symbols
# ============================================================

SECTOR_MAP = {
    'healthcare'  : 'NIFTYHEALTHCARE',
    'pharma'      : 'CNXPHARMA',
    'chemical'    : 'NIFTYCHEMICALS',
    'chemicals'   : 'NIFTYCHEMICALS',
    'financials'  : 'NIFTYFINSERVICE',
    'finance'     : 'NIFTYFINSERVICE',
    'bank'        : 'BANKNIFTY',
    'banking'     : 'BANKNIFTY',
    'psubank'     : 'NIFTYPSUBANK',
    'pvtbank'     : 'NIFTYPVTBANK',
    'energy'      : 'CNXENERGY',
    'oil'         : 'NIFTYOILANDGAS',
    'oilgas'      : 'NIFTYOILANDGAS',
    'auto'        : 'NIFTYAUTO',
    'automobile'  : 'NIFTYAUTO',
    'fmcg'        : 'NIFTYFMCG',
    'metal'       : 'NIFTYMETAL',
    'metals'      : 'NIFTYMETAL',
    'realty'      : 'CNXREALTY',
    'real estate' : 'CNXREALTY',
    'media'       : 'NIFTYMEDIA',
    'infra'       : 'CNXINFRA',
    'infrastructure': 'CNXINFRA',
    'it'          : 'CNXIT',
    'technology'  : 'CNXIT',
    'tech'        : 'CNXIT',
    'commodity'   : 'NIFTYCOMMODITIES',
    'commodities' : 'NIFTYCOMMODITIES',
    'consumption' : 'NIFTYCONSUMPTION',
    'defence'     : 'NIFTYINDDEFENCE',
    'defense'     : 'NIFTYINDDEFENCE',
    'housing'     : 'NIFTYHOUSING',
    'cpse'        : 'NIFTYCPSE',
    'midcap'      : 'NIFTYMIDCAP150',
    'smallcap'    : 'NIFTYSMALLCAP250',
}


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
    Falls back up to 4 days to handle weekends/holidays.
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

                if 'Scanner_Source' in df.columns:
                    print(f"  Sources: {df['Scanner_Source'].value_counts().to_dict()}")

                sym_col = next(
                    (c for c in df.columns if c.lower() in ['symbol','stock','ticker','scrip']),
                    df.columns[1]
                )

                sec_col1 = next((c for c in df.columns if c.strip().lower() == 'sector'), None)
                sec_col2 = next((c for c in df.columns if c.strip().lower() == 'sectors'), None)

                seen = {}
                for _, row in df.iterrows():
                    sym = str(row[sym_col]).strip().upper()
                    sym_clean = sym.replace('-', '').replace('&', 'AND')
                    if len(sym_clean) < 2 or sym_clean in ['NAN', 'SYMBOL', 'NONE']:
                        continue

                    sec = 'nse'
                    for sc in [sec_col1, sec_col2]:
                        if sc and sc in row:
                            val = str(row[sc]).strip()
                            if val and val.lower() not in ['nan', 'none', '']:
                                sec = val.lower().strip()
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
# STEP 3 — LOAD SECTOR SCORES (separate from stock screening)
# ============================================================

def load_sector_scores(csv_date):
    """
    Load sector performance from sectors/ folder.
    Completely separate from stock price data in prices/ folder.
    Returns dict: {chartink_symbol: {score, boost, rank, weekly, 20d}}
    """
    now_ist = datetime.now(IST)

    # Try today first, then fall back up to 4 days
    sector_file = None
    for days_back in range(5):
        date_str = (now_ist - timedelta(days=days_back)).strftime('%Y-%m-%d')
        candidate = f"sectors/{date_str}_Preferred_Sectors.csv"
        if os.path.exists(candidate):
            sector_file = candidate
            print(f"  Sector file: {candidate}")
            break

    if not sector_file:
        print("  WARNING: No sector file found in sectors/ folder")
        print("  Sector boost will be disabled today")
        return {}, []

    try:
        df = pd.read_csv(sector_file, encoding='utf-8-sig')
        df.columns = [c.strip() for c in df.columns]
        print(f"  Sector columns: {list(df.columns)}")

        # Find relevant columns
        sym_col     = next((c for c in df.columns if c.lower() in ['symbol','index']), df.columns[0])
        weekly_col  = next((c for c in df.columns if 'week' in c.lower()), None)
        twenty_col  = next((c for c in df.columns if '20' in c.lower() or 'twenty' in c.lower()), None)
        mom_col     = next((c for c in df.columns if 'mom' in c.lower()), None)

        print(f"  Using columns: sym={sym_col}, weekly={weekly_col}, 20d={twenty_col}, mom={mom_col}")

        # Drop rows with missing key data
        df = df.dropna(subset=[sym_col])
        df[sym_col] = df[sym_col].astype(str).str.strip().str.upper()

        # Convert numeric columns safely
        def to_float(col):
            if col and col in df.columns:
                return pd.to_numeric(df[col], errors='coerce').fillna(0)
            return pd.Series([0] * len(df))

        weekly  = to_float(weekly_col)
        twenty  = to_float(twenty_col)
        mom     = to_float(mom_col)

        # Composite sector score
        # Weekly % and 20d % are equally important (40% each)
        # Momentum adds confirmation (20%)
        df['sector_score'] = (weekly * 0.4) + (twenty * 0.4) + (mom * 0.2)

        # Sort best to worst
        df = df.sort_values('sector_score', ascending=False).reset_index(drop=True)

        print(f"\n  Sector Rankings:")
        print(f"  {'Rank':<5} {'Symbol':<25} {'Score':>6}  {'Boost':>6}")
        print(f"  {'-'*50}")

        sector_scores = {}
        for i, row in df.iterrows():
            sym   = row[sym_col]
            score = round(row['sector_score'], 2)
            rank  = i + 1

            # Boost tiers based on Minervini sector rotation principle
            if rank <= 3:
                boost = 10      # Top 3 — strong tailwind
            elif rank <= 8:
                boost = 5       # Sectors 4-8 — mild tailwind
            elif rank <= 15:
                boost = 0       # Sectors 9-15 — neutral
            elif rank <= 22:
                boost = -3      # Sectors 16-22 — mild headwind
            else:
                boost = -5      # Bottom sectors — strong headwind

            sector_scores[sym] = {
                'score'  : score,
                'boost'  : boost,
                'rank'   : rank,
                'weekly' : round(float(weekly.iloc[i]), 2),
                '20d'    : round(float(twenty.iloc[i]), 2),
            }

            boost_str = f"+{boost}" if boost > 0 else str(boost)
            print(f"  {rank:<5} {sym:<25} {score:>6.2f}  {boost_str:>6}")

        print(f"\n  Total sectors loaded: {len(sector_scores)}")
        return sector_scores, df[sym_col].tolist()

    except Exception as e:
        print(f"  ERROR loading sector file: {e}")
        return {}, []


def get_sector_boost(stock_sector, sector_scores):
    """
    Map stock sector name to Chartink index symbol
    and return boost + rank info.
    """
    sector_lower = str(stock_sector).lower().strip()

    # Direct lookup in SECTOR_MAP
    chartink_sym = SECTOR_MAP.get(sector_lower, '')

    if chartink_sym and chartink_sym in sector_scores:
        data = sector_scores[chartink_sym]
        return data['boost'], data['rank'], chartink_sym, data['score']

    # Fuzzy match — try partial matches
    for key, sym in SECTOR_MAP.items():
        if key in sector_lower or sector_lower in key:
            if sym in sector_scores:
                data = sector_scores[sym]
                return data['boost'], data['rank'], sym, data['score']

    return 0, 99, 'UNKNOWN', 0.0


# ============================================================
# STEP 4 — TECHNICAL INDICATORS
# ============================================================

def compute_indicators(df, nifty_df):
    """Compute all Minervini indicators."""
    d = {}
    close  = df['Close'].squeeze()
    volume = df['Volume'].squeeze()
    high   = df['High'].squeeze()
    low    = df['Low'].squeeze()

    d['sma50']          = close.rolling(50).mean().iloc[-1]
    d['sma150']         = close.rolling(150).mean().iloc[-1]
    d['sma200']         = close.rolling(200).mean().iloc[-1]
    d['sma200_20d_ago'] = close.rolling(200).mean().iloc[-20]
    d['current_price']  = close.iloc[-1]

    d['ma_stack_ok'] = (
        d['current_price'] > d['sma50'] and
        d['sma50']  > d['sma150'] and
        d['sma150'] > d['sma200'] and
        d['sma200'] > d['sma200_20d_ago']
    )

    d['high_52w']       = high.iloc[-252:].max() if len(high) >= 252 else high.max()
    d['low_52w']        = low.iloc[-252:].min()  if len(low)  >= 252 else low.min()
    d['base_depth_pct'] = round((d['high_52w'] - d['low_52w']) / d['high_52w'] * 100, 1)
    d['pct_from_high']  = round((d['high_52w'] - d['current_price']) / d['high_52w'] * 100, 1)
    d['base_depth_ok']  = d['base_depth_pct'] < MAX_BASE_DEPTH_PCT
    d['near_high_ok']   = d['pct_from_high'] < 15

    vol_20d = volume.iloc[-20:].mean()
    vol_60d = volume.iloc[-60:-20].mean() if len(volume) >= 60 else volume.mean()
    d['vol_20d_avg']         = round(vol_20d)
    d['vol_60d_avg']         = round(vol_60d)
    d['vol_contraction_pct'] = round((vol_60d - vol_20d) / vol_60d * 100, 1) if vol_60d > 0 else 0
    d['vol_contraction_ok']  = vol_20d < vol_60d

    d['recent_vol_ratio'] = round(volume.iloc[-3:].max() / vol_20d, 2) if vol_20d > 0 else 0
    d['vol_breakout_ok']  = d['recent_vol_ratio'] >= BREAKOUT_VOLUME_MULTIPLIER

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

    d['contraction_range_pct'] = round((high.iloc[-20:].max() - low.iloc[-20:].min()) / high.iloc[-20:].max() * 100, 1)
    d['contraction_ok']        = d['contraction_range_pct'] < 25

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
# STEP 5 — CLAUDE AI SCORING
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
# STEP 6 — FORMAT TELEGRAM MESSAGE
# ============================================================

def score_bar(score):
    """Telegram-safe visual score bar using plain ASCII."""
    filled = round(score / 10)
    empty  = 10 - filled
    return "[" + ("=" * filled) + ("." * empty) + "]"


def format_header(today, total_picks, total_passed, top_sectors):
    """Clean header with market + top sectors summary."""
    sector_line = ""
    if top_sectors:
        sector_line = f"🏆 Top sectors: <b>{', '.join(top_sectors[:3])}</b>\n"

    return (
        f"📊 <b>VCP SCANNER — DAILY PICKS</b>\n"
        f"📅 {today}  |  🤖 Minervini + Claude AI\n"
        f"〰〰〰〰〰〰〰〰〰〰〰〰〰\n"
        f"🔍 Stocks passed filters: <b>{total_passed}</b>\n"
        f"🏆 Top picks selected: <b>{total_picks}</b>\n"
        f"{sector_line}"
        f"〰〰〰〰〰〰〰〰〰〰〰〰〰"
    )


def format_pick_message(pick, rank, total):
    """Format a single stock pick — clean Telegram-safe rich text."""

    # Risk:Reward
    rr = round(
        (pick['target_20pct'] - pick['pivot_level']) / max(pick['pivot_level'] - pick['stop_loss'], 1), 1
    )

    # Sanitise all text fields
    def safe(text):
        return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    entry         = safe(pick['entry_zone'])
    risk          = safe(pick['key_risk'])
    hold_signal   = safe(pick.get('hold_signal', 'Stay above stop loss at all times'))
    exit_signal   = safe(pick.get('exit_signal', 'Exit if daily close below stop loss'))
    buy_reasons   = pick.get('buy_reasons', [])
    sector        = safe(pick.get('sector', 'NSE').title())
    score         = pick['score']
    boosted_score = pick.get('boosted_score', score)
    sector_boost  = pick.get('sector_boost', 0)
    sector_rank   = pick.get('sector_rank', 99)
    rs_rating     = pick.get('rs_rating', '-')
    vcp_pivots    = pick.get('vcp_pivots', '-')
    stage         = str(pick['vcp_stage']).upper()
    current_price = round(pick.get('current_price', 0), 2)

    # Rank medal
    medals = {1: "🥇", 2: "🥈", 3: "🥉"}
    medal  = medals.get(rank, f"#{rank}")

    # Stage emoji
    stage_emoji = {"EARLY": "🌱", "MID": "📈", "LATE": "🔥"}.get(stage, "📊")

    # Score icon based on BOOSTED score
    if boosted_score >= 80:
        score_icon = "🟢"
    elif boosted_score >= 65:
        score_icon = "🟡"
    else:
        score_icon = "🔴"

    # Score bar
    bar = score_bar(boosted_score)

    # Sector boost display
    if sector_boost > 0:
        boost_str = f"+{sector_boost} sector tailwind 🚀"
    elif sector_boost < 0:
        boost_str = f"{sector_boost} sector headwind ⚠️"
    else:
        boost_str = "sector neutral"

    # Buy reasons
    if buy_reasons:
        reasons_text = "\n".join([f"  ▸ {safe(r)}" for r in buy_reasons[:3]])
    else:
        reasons_text = f"  ▸ {safe(pick.get('why_this_stock', '-'))}"

    msg = (
        f"〰〰〰〰〰〰〰〰〰〰〰〰〰\n"
        f"{medal} <b>{pick['symbol']}</b>  {score_icon}  {stage_emoji} {stage} VCP\n"
        f"📂 {sector}  |  Pivots: {vcp_pivots}  |  Sector Rank: #{sector_rank}\n"
        f"〰〰〰〰〰〰〰〰〰〰〰〰〰\n"
        f"\n"
        f"<b>Score:</b> {boosted_score}/100  <code>{bar}</code>\n"
        f"<i>Base: {score}  |  {boost_str}</i>\n"
        f"<b>RS Rating:</b> {rs_rating}  |  <b>R:R</b> 1:{rr}  |  <b>Hold:</b> ~{pick['hold_days_estimate']}d\n"
        f"\n"
        f"💰 <b>Trade Setup</b>\n"
        f"  Current Price :  Rs.{current_price}\n"
        f"  Entry Zone    :  Rs.{entry}\n"
        f"  Pivot Level   :  Rs.{pick['pivot_level']}\n"
        f"  Stop Loss     :  Rs.{pick['stop_loss']}  🛑\n"
        f"\n"
        f"🎯 <b>Targets</b>\n"
        f"  +10%  →  Rs.{pick['target_10pct']}\n"
        f"  +20%  →  Rs.{pick['target_20pct']}\n"
        f"  +30%  →  Rs.{pick['target_30pct']}\n"
        f"\n"
        f"✅ <b>Why Buy:</b>\n"
        f"{reasons_text}\n"
        f"\n"
        f"📌 <b>Hold Signal:</b>\n"
        f"  {hold_signal}\n"
        f"\n"
        f"🚪 <b>Exit Signal:</b>\n"
        f"  {exit_signal}\n"
        f"\n"
        f"⚠️ <b>Risk:</b>  {risk}\n"
    )
    return msg


def format_footer(total_picks):
    return (
        f"〰〰〰〰〰〰〰〰〰〰〰〰〰\n"
        f"🤖 <i>Powered by Minervini VCP + Claude AI + Sector Rotation</i>\n"
        f"🛑 <i>Hard stop -7% from entry. No exceptions.</i>\n"
        f"📌 <i>Do your own research before trading.</i>"
    )


# ============================================================
# MAIN
# ============================================================

def main():
    now_ist = datetime.now(IST)
    print(f"\n{'='*50}")
    print(f"VCP Scanner v3 starting at {now_ist.strftime('%Y-%m-%d %H:%M IST')}")
    print(f"{'='*50}\n")

    notify(f"🔍 <b>VCP Scanner</b> started\n📅 {now_ist.strftime('%d %b %Y')}  |  ⏰ {now_ist.strftime('%H:%M IST')}")

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

    # --- Load sector scores (SEPARATE from stock screening) ---
    print("\nLoading sector performance data...")
    sector_scores, sector_ranking = load_sector_scores(csv_date)

    # Get top 3 sector names for header
    top_sectors = []
    for sym in sector_ranking[:3]:
        # Find friendly name
        for friendly, chartink in SECTOR_MAP.items():
            if chartink == sym:
                top_sectors.append(friendly.title())
                break
        if sym not in [SECTOR_MAP.get(k) for k in SECTOR_MAP]:
            top_sectors.append(sym)
    top_sectors = top_sectors[:3]

    # --- Load Nifty 50 ---
    print("\nLoading Nifty 50...")
    nifty = pd.DataFrame(columns=['Open','High','Low','Close','Volume'])
    nifty_file = f"{csv_date}_NIFTY50.csv"
    if os.path.exists(nifty_file):
        raw = pd.read_csv(nifty_file, parse_dates=['Date'])
        raw = raw.set_index('Date').sort_index()
        nifty = raw[['Open','High','Low','Close','Volume']].dropna()
        print(f"  Nifty: {len(nifty)} days loaded")
    else:
        print(f"  WARNING: {nifty_file} not found — RS scores will be zero")

    # --- Load pre-fetched stock price data ---
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
            print(f"  {sym}: no price file found")

    if not stock_data:
        send_telegram(
            f"<b>VCP Scanner — {now_ist.strftime('%d %b %Y')}</b>\n\n"
            f"Could not load price data for any stock.\n"
            f"Please check github_push.py ran correctly."
        )
        return

    # --- Apply Minervini filters ---
    print(f"\nApplying Minervini filters ({MIN_RULES_TO_PASS}/8 rules required)...")
    shortlist = []
    for sym, df in stock_data.items():
        try:
            ind = compute_indicators(df, nifty)
            passed, rules, count = apply_filters(sym, ind)
            status = 'PASS' if passed else f'FAIL ({count}/8)'
            print(f"  {sym}: {status}")
            for name, val in rules.items():
                print(f"    {'✓' if val else '✗'} {name}")
            if passed:
                shortlist.append({
                    'symbol'      : sym,
                    'sector'      : sector_map.get(sym, 'nse'),
                    'indicators'  : ind,
                    'rules'       : rules,
                    'rule_score'  : count,
                    'current_price': round(ind['current_price'], 2)
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
            result['sector']        = item['sector']
            result['rule_score']    = item['rule_score']
            result['current_price'] = item['current_price']
            scored.append(result)
            print(f"{result['score']}/100")
        else:
            print("scoring failed")
        time.sleep(1)

    if not scored:
        send_telegram("VCP Scanner: Claude scoring failed for all stocks. Check CLAUDE_API_KEY.")
        return

    # --- Apply sector boost to final scores ---
    print(f"\nApplying sector boost...")
    print(f"  {'Symbol':<15} {'Sector':<15} {'Base':>5} {'Boost':>6} {'Final':>6}  Index")
    print(f"  {'-'*60}")

    for s in scored:
        stock_sector = s.get('sector', 'nse')
        boost, rank, idx_sym, idx_score = get_sector_boost(stock_sector, sector_scores)

        s['sector_boost']  = boost
        s['sector_rank']   = rank
        s['sector_index']  = idx_sym
        s['boosted_score'] = min(100, s['score'] + boost)  # cap at 100

        boost_str = f"+{boost}" if boost > 0 else str(boost)
        print(f"  {s['symbol']:<15} {stock_sector:<15} {s['score']:>5} {boost_str:>6} {s['boosted_score']:>6}  {idx_sym}")

    # Sort by boosted score
    scored.sort(key=lambda x: x['boosted_score'], reverse=True)

    print(f"\nFinal ranking after sector boost:")
    for i, s in enumerate(scored, 1):
        print(f"  #{i} {s['symbol']}: {s['boosted_score']}/100 (base {s['score']} {s['sector_boost']:+d}) [{s['sector']}]")

    # --- Select top picks ---
    final_picks = scored[:TOP_N_PICKS]
    print(f"\nFinal {len(final_picks)} picks: {[p['symbol'] for p in final_picks]}")

    # --- Send to Telegram ---
    today = datetime.now(IST).strftime('%d %b %Y')

    # 1. Header with top sectors
    header = format_header(today, len(final_picks), len(shortlist), top_sectors)
    r = send_telegram(header)
    print(f"Header sent: {r.get('ok')}")
    time.sleep(0.8)

    # 2. Each pick individually
    for rank, pick in enumerate(final_picks, 1):
        msg = format_pick_message(pick, rank, len(final_picks))
        r = send_telegram(msg)
        print(f"Pick {rank} ({pick['symbol']}) sent: {r.get('ok')}")
        time.sleep(0.8)

    # 3. Footer
    footer = format_footer(len(final_picks))
    r = send_telegram(footer)
    print(f"Footer sent: {r.get('ok')}")

    print(f"\nDone at {datetime.now(IST).strftime('%H:%M IST')}")
    print("=" * 50)


if __name__ == "__main__":
    main()
