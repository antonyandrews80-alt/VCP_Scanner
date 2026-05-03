"""
VCP Daily Scanner — Fully Automated Version
Runs via GitHub Actions every weekday at 9:00 PM IST (3:30 PM UTC)
No manual input required — fetches all data automatically
"""

import os
import re
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
# CONFIGURATION — loaded from GitHub Secrets (environment vars)
# ============================================================

CLAUDE_API_KEY      = os.environ.get("CLAUDE_API_KEY", "")
TELEGRAM_TOKEN      = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID    = os.environ.get("TELEGRAM_CHAT_ID", "")
CHARTINK_EMAIL      = os.environ.get("CHARTINK_EMAIL", "")
CHARTINK_PASSWORD   = os.environ.get("CHARTINK_PASSWORD", "")
CHARTINK_SCREEN1_ID = os.environ.get("CHARTINK_SCREEN1_ID", "")  # numeric ID from dashboard URL e.g. 441909
CHARTINK_SCREEN2_ID = os.environ.get("CHARTINK_SCREEN2_ID", "")  # numeric ID from dashboard URL e.g. 441910

# Filter settings — match Colab exactly
TOP_N_PICKS                 = 2
MAX_BASE_DEPTH_PCT          = 40
MIN_RS_RISE_DAYS            = 50
BREAKOUT_VOLUME_MULTIPLIER  = 2.0
MIN_RULES_TO_PASS           = 6

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
# STEP 2 — CHARTINK LOGIN + SCANNER FETCH
# ============================================================

def chartink_login():
    """Login to Chartink Atlas and return authenticated session."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    })

    # Step 1 — Load login page to get CSRF token
    r = session.get("https://chartink.com/login", timeout=15)
    csrf = None
    match = re.search(r'<meta name="csrf-token" content="([^"]+)"', r.text)
    if match:
        csrf = match.group(1)
    if not csrf:
        match = re.search(r'name="_token"[^>]+value="([^"]+)"', r.text)
        if match:
            csrf = match.group(1)
    print(f"  CSRF token found: {'yes' if csrf else 'no'}")

    # Step 2 — Post login credentials
    login_data = {
        "_token": csrf or "",
        "email": CHARTINK_EMAIL,
        "password": CHARTINK_PASSWORD,
        "remember": "on",
    }
    resp = session.post(
        "https://chartink.com/login",
        data=login_data,
        headers={"Referer": "https://chartink.com/login"},
        timeout=15,
        allow_redirects=True
    )
    print(f"  Login POST status: {resp.status_code}, final URL: {resp.url}")

    # Step 3 — Verify login succeeded by checking for user-specific content
    check = session.get("https://chartink.com/dashboard", timeout=15)
    logged_in = "logout" in check.text.lower() or "dashboard" in check.url
    print(f"  Login verified: {logged_in} (status {check.status_code})")

    return session


def normalise_symbol_column(df):
    """Find the stock symbol column regardless of Chartink column naming."""
    for col in df.columns:
        if str(col).strip().lower() in ['symbol', 'stock', 'ticker', 'name', 'scrip']:
            return col
    return df.columns[0]  # fallback to first column


def fetch_chartink_scanner(session, scanner_id):
    """
    Fetch stock symbols from a Chartink Atlas dashboard.
    URL: https://chartink.com/dashboard/<scanner_id>
    Tries multiple methods to extract the stock table.
    """
    dashboard_url = f"https://chartink.com/dashboard/{scanner_id}"

    # --- Method 1: Atlas API endpoint ---
    try:
        # First load the dashboard page to get fresh CSRF
        r = session.get(dashboard_url, timeout=20)
        print(f"  Dashboard {scanner_id} page status: {r.status_code}")

        csrf = None
        m = re.search(r'<meta name="csrf-token" content="([^"]+)"', r.text)
        if m:
            csrf = m.group(1)

        # Try Atlas screener data API
        for api_path in [
            "/api/dashboard/screener-data",
            "/atlas/screener-data",
            "/dashboard/screener-data",
        ]:
            try:
                api_resp = session.post(
                    f"https://chartink.com{api_path}",
                    json={"dashboard_id": scanner_id},
                    headers={
                        "X-CSRF-TOKEN": csrf or "",
                        "X-Requested-With": "XMLHttpRequest",
                        "Accept": "application/json",
                        "Content-Type": "application/json",
                        "Referer": dashboard_url,
                    },
                    timeout=20
                )
                if api_resp.status_code == 200:
                    data = api_resp.json()
                    print(f"  API {api_path} keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
                    stocks = None
                    if isinstance(data, dict):
                        for key in ["data", "stocks", "results", "items", "screener_data"]:
                            if key in data and isinstance(data[key], list):
                                stocks = data[key]
                                break
                    if stocks:
                        df = pd.DataFrame(stocks)
                        print(f"  Columns: {list(df.columns)[:8]}")
                        sym_col = normalise_symbol_column(df)
                        symbols = df[sym_col].dropna().astype(str).str.strip().str.upper().tolist()
                        symbols = [s for s in symbols if len(s) > 1 and s not in ["NAN","NONE"]]
                        if symbols:
                            print(f"  Scanner {scanner_id} (API): {len(symbols)} stocks → {symbols[:5]}")
                            return symbols
            except Exception as e:
                print(f"  API {api_path} failed: {e}")

    except Exception as e:
        print(f"  Dashboard page fetch failed: {e}")

    # --- Method 2: Parse HTML table directly from dashboard page ---
    try:
        r = session.get(dashboard_url, timeout=20)

        # Look for stock symbols in JSON embedded in page (common in JS apps)
        # Pattern: array of objects with symbol field
        json_matches = re.findall(r'\{"symbol":"([A-Z&-]{2,20})"', r.text)
        if not json_matches:
            json_matches = re.findall(r'"symbol"\s*:\s*"([A-Z&-]{2,20})"', r.text)
        if not json_matches:
            # Try broader pattern for NSE symbols
            json_matches = re.findall(r'"([A-Z]{2,15})"\s*,\s*[\d.]+\s*,\s*[\d.]+', r.text)

        if json_matches:
            symbols = list(dict.fromkeys(json_matches))  # deduplicate preserving order
            print(f"  Scanner {scanner_id} (JSON in HTML): {len(symbols)} stocks → {symbols[:5]}")
            return symbols

        # Try HTML tables
        tables = pd.read_html(r.text)
        print(f"  HTML tables found: {len(tables)}")
        for i, df in enumerate(tables):
            print(f"    Table {i}: shape={df.shape}, cols={list(df.columns)[:6]}")
            sym_col = normalise_symbol_column(df)
            symbols = df[sym_col].dropna().astype(str).str.strip().str.upper().tolist()
            symbols = [s for s in symbols if len(s) > 1
                       and s not in ['SYMBOL', 'STOCK', 'NAME', 'TICKER', 'SCRIP', 'NAN']]
            if len(symbols) > 0:
                print(f"  Scanner {scanner_id} (HTML table {i}): {len(symbols)} stocks → {symbols[:5]}")
                return symbols

        # Last resort — dump page for debugging
        print(f"  Page length: {len(r.text)} chars")
        print(f"  Page snippet 1: {r.text[:500]}")
        print(f"  Page snippet 2: {r.text[2000:2500]}")
        # Check if we got redirected to login (not authenticated)
        if "login" in r.url or "sign" in r.url.lower():
            print(f"  WARNING: Redirected to login page — session not authenticated!")
        elif r.status_code == 404:
            print(f"  WARNING: 404 — dashboard ID {scanner_id} may be wrong or private")

    except Exception as e:
        print(f"  HTML fallback failed: {e}")

    print(f"  Scanner {scanner_id}: returned 0 stocks")
    return []

def normalise_symbol_column(df):
    """Find the stock symbol column regardless of Chartink column naming."""
    for col in df.columns:
        if str(col).strip().lower() in ['symbol', 'stock', 'ticker', 'name', 'scrip']:
            return col
    return df.columns[0]  # fallback to first column


def fetch_chartink_scanner(session, scanner_id):
    """
    Fetch scanner results from a Chartink dashboard.
    URL format: https://chartink.com/dashboard/<scanner_id>
    Uses Chartink's dashboard data API to get stock symbols.
    """
    # --- Try dashboard data API (JSON) ---
    try:
        dashboard_url = f"https://chartink.com/dashboard/{scanner_id}"
        r = session.get(dashboard_url, timeout=20)
        print(f"  Scanner {scanner_id} dashboard status: {r.status_code}")

        # Extract CSRF token from dashboard page
        csrf = None
        m = re.search(r'<meta name="csrf-token" content="([^"]+)"', r.text)
        if m:
            csrf = m.group(1)

        # Try dashboard stocks API endpoint
        api_resp = session.post(
            "https://chartink.com/dashboard/get-data",
            data={"dashboard_id": scanner_id},
            headers={
                "X-CSRF-TOKEN": csrf or "",
                "X-Requested-With": "XMLHttpRequest",
                "Referer": dashboard_url,
            },
            timeout=20
        )
        print(f"  Dashboard API status: {api_resp.status_code}")

        try:
            data = api_resp.json()
            print(f"  Dashboard API response keys: {list(data.keys()) if isinstance(data, dict) else type(data)}")
            # Try common response structures
            stocks = None
            if isinstance(data, dict):
                stocks = data.get("data") or data.get("stocks") or data.get("results") or data.get("items")
            if isinstance(stocks, list) and len(stocks) > 0:
                df = pd.DataFrame(stocks)
                print(f"  Dashboard data columns: {list(df.columns)[:8]}")
                sym_col = normalise_symbol_column(df)
                symbols = df[sym_col].dropna().astype(str).str.strip().str.upper().tolist()
                symbols = [s for s in symbols if len(s) > 1 and s not in ["NAN", "NONE"]]
                print(f"  Scanner {scanner_id} (dashboard API): {len(symbols)} stocks")
                return symbols
        except Exception as e:
            print(f"  Dashboard API JSON parse failed: {e}")

    except Exception as e:
        print(f"  Scanner {scanner_id} dashboard attempt failed: {e}")

    # --- Fallback: parse HTML tables from dashboard page ---
    try:
        dashboard_url = f"https://chartink.com/dashboard/{scanner_id}"
        r = session.get(dashboard_url, timeout=20)
        tables = pd.read_html(r.text)
        print(f"  Scanner {scanner_id} HTML tables found: {len(tables)}")
        for i, df in enumerate(tables):
            print(f"    Table {i}: shape={df.shape}, cols={list(df.columns)[:6]}")
            sym_col = normalise_symbol_column(df)
            symbols = df[sym_col].dropna().astype(str).str.strip().str.upper().tolist()
            symbols = [s for s in symbols if len(s) > 1
                       and s not in ['SYMBOL', 'STOCK', 'NAME', 'TICKER', 'SCRIP', 'NAN']]
            if len(symbols) > 0:
                print(f"  Scanner {scanner_id} (HTML table {i}): {len(symbols)} stocks → {symbols[:5]}")
                return symbols
    except Exception as e:
        print(f"  Scanner {scanner_id} HTML fallback failed: {e}")

    print(f"  Scanner {scanner_id}: returned 0 stocks")
    return []


# ============================================================
# STEP 3 — TECHNICAL INDICATORS (exact match to Colab Block 7)
# ============================================================

def compute_indicators(df, nifty_df):
    """Compute all Minervini indicators for a stock."""
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
    """Apply all 8 Minervini rules — exact match to Colab Block 7."""
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
# STEP 4 — CLAUDE AI SCORING (exact prompt from Colab Block 8)
# ============================================================

def score_with_claude(symbol, ind, rules):
    """Score a stock setup using Claude API — prompt matches Colab exactly."""
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)

    rules_text = "\n".join([
        f"  - {r}: {'PASS' if v else 'FAIL'}" for r, v in rules.items()
    ])

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
  - VCP pattern quality (tightness, number of contractions, pivot clarity): 40%
  - RS strength vs Nifty: 30%
  - Volume pattern (drying in base, surge on breakout): 30%

Return ONLY this JSON (no markdown, no explanation outside JSON):
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
  "why_this_stock": "<2 sentence max plain English reason>",
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
        print(f"Claude error for {symbol}: {e}")
        return None


# ============================================================
# STEP 5 — FORMAT TELEGRAM MESSAGE (matches Colab Block 10)
# ============================================================

def format_picks_message(picks):
    today = datetime.now(IST).strftime('%d %b %Y')

    msg = f"<b>VCP Daily Picks — {today}</b>\n\n"

    for i, p in enumerate(picks, 1):
        rr = round(
            (p['target_20pct'] - p['pivot_level']) / max(p['pivot_level'] - p['stop_loss'], 1), 1
        )
        msg += f"""<b>Pick {i}: {p['symbol']}</b> [{p.get('sector', 'NSE')}]
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

    # Weekend check disabled — runs any day
    # if now_ist.weekday() >= 5:
    #     print("Weekend — no scan today.")
    #     return

    notify(f"VCP Scanner started — {now_ist.strftime('%d %b %Y %H:%M IST')}")

    # --- Login to Chartink ---
    print("Logging into Chartink...")
    try:
        session = chartink_login()
        print("Chartink login done")
    except Exception as e:
        notify(f"Chartink login failed: {e}")
        return

    # --- Fetch Screen 1 and Screen 2 ---
    print("\nFetching Screen 1 (VCP base)...")
    symbols_s1 = set(fetch_chartink_scanner(session, CHARTINK_SCREEN1_ID))
    print(f"Screen 1: {len(symbols_s1)} stocks → {sorted(symbols_s1)[:10]}")

    print("\nFetching Screen 2 (trigger)...")
    symbols_s2 = set(fetch_chartink_scanner(session, CHARTINK_SCREEN2_ID))
    print(f"Screen 2: {len(symbols_s2)} stocks → {sorted(symbols_s2)[:10]}")

    # --- Candidate selection (same logic as Colab Block 5) ---
    intersection = symbols_s1.intersection(symbols_s2)
    screen1_only = symbols_s1 - symbols_s2

    print(f"\nHigh priority (both screens): {len(intersection)} stocks")
    print(f"Watch list (Screen 1 only)  : {len(screen1_only)} stocks")

    if len(intersection) > 0:
        candidates = sorted(intersection)
        print(f"Using intersection: {candidates}")
    else:
        candidates = sorted(symbols_s1)
        print(f"No intersection — using Screen 1: {candidates[:10]}")

    if not candidates:
        send_telegram(f"<b>VCP Scanner — {now_ist.strftime('%d %b %Y')}</b>\n\nNo candidate stocks found today.")
        print("No candidates — exiting.")
        return

    # --- Fetch Nifty 50 ---
    print("\nFetching Nifty 50...")
    nifty = yf.download('^NSEI', period='1y', interval='1d', progress=False, auto_adjust=True)
    nifty.columns = [c[0] if isinstance(c, tuple) else c for c in nifty.columns]
    print(f"Nifty data: {len(nifty)} trading days")

    # --- Fetch stock data ---
    print(f"\nFetching data for {len(candidates)} stocks...")
    stock_data = {}
    failed = []
    for sym in candidates:
        ticker = sym + '.NS'
        try:
            df = yf.download(ticker, period='1y', interval='1d', progress=False, auto_adjust=True)
            if len(df) >= 50:
                df.columns = [c[0] if isinstance(c, tuple) else c for c in df.columns]
                stock_data[sym] = df
                print(f"  {sym}: {len(df)} days")
            else:
                failed.append(sym)
                print(f"  {sym}: too few data points ({len(df)})")
        except Exception as e:
            failed.append(sym)
            print(f"  {sym}: failed ({e})")
        time.sleep(0.3)

    print(f"\nData ready for {len(stock_data)} stocks.")
    if failed:
        print(f"Failed to fetch: {failed}")

    if not stock_data:
        send_telegram(f"<b>VCP Scanner — {now_ist.strftime('%d %b %Y')}</b>\n\nCould not fetch data for any candidate stocks.")
        return

    # --- Apply Minervini filters ---
    print("\nApplying Minervini filters...")
    shortlist = []
    for sym, df in stock_data.items():
        try:
            ind = compute_indicators(df, nifty)
            passed, rules, count = apply_filters(sym, ind)
            status = 'PASS' if passed else 'FAIL'
            print(f"  {sym}: {status} ({count}/8 rules passed)")
            for rule_name, rule_val in rules.items():
                print(f"    {'✓' if rule_val else '✗'} {rule_name}")
            if passed:
                shortlist.append({'symbol': sym, 'indicators': ind, 'rules': rules, 'rule_score': count})
        except Exception as e:
            print(f"  {sym}: error — {e}")

    print(f"\nShortlist after Minervini filters: {len(shortlist)} stocks")

    if not shortlist:
        send_telegram(f"<b>VCP Scanner — {now_ist.strftime('%d %b %Y')}</b>\n\nNo stocks passed Minervini filters today.")
        return

    # --- Claude AI scoring ---
    print(f"\nScoring {len(shortlist)} stocks with Claude...")
    scored = []
    for item in shortlist:
        sym = item['symbol']
        print(f"  Scoring {sym}...", end=' ')
        result = score_with_claude(sym, item['indicators'], item['rules'])
        if result:
            result['rule_score'] = item['rule_score']
            scored.append(result)
            print(f"Score: {result['score']}/100")
        else:
            print("Failed")
        time.sleep(1)

    if not scored:
        send_telegram("Claude scoring failed for all stocks today.")
        return

    scored.sort(key=lambda x: x['score'], reverse=True)

    print(f"\nScoring complete. Top results:")
    for s in scored:
        print(f"  {s['symbol']}: {s['score']}/100")

    # --- Sector diversification (Colab Block 9) ---
    print("\nFetching sector info...")
    for s in scored:
        try:
            info = yf.Ticker(s['symbol'] + '.NS').info
            s['sector'] = info.get('sector', 'NSE')
        except Exception:
            s['sector'] = 'NSE'
        print(f"  {s['symbol']}: {s['sector']}")
        time.sleep(0.3)

    final_picks = []
    used_sectors = []
    for s in scored:
        if len(final_picks) >= TOP_N_PICKS:
            break
        if s['sector'] not in used_sectors or s['sector'] == 'NSE':
            final_picks.append(s)
            used_sectors.append(s['sector'])

    if len(final_picks) < TOP_N_PICKS and len(scored) >= TOP_N_PICKS:
        final_picks = scored[:TOP_N_PICKS]

    print(f"\nFINAL {len(final_picks)} PICKS:")
    for i, p in enumerate(final_picks, 1):
        print(f"  Pick {i}: {p['symbol']} | Score: {p['score']}/100 | Sector: {p['sector']}")
        print(f"           Entry: {p['entry_zone']} | Stop: {p['stop_loss']} | Target: {p['target_20pct']}")

    # --- Send to Telegram ---
    print("\nSending picks to Telegram...")
    message = format_picks_message(final_picks)
    result = send_telegram(message)

    if result.get('ok'):
        print("Picks sent successfully!")
    else:
        print(f"Telegram error: {result}")

    print(f"\nDone at {datetime.now(IST).strftime('%H:%M IST')}")
    print("=" * 50)


if __name__ == "__main__":
    main()
