"""
VCP Daily Scanner — CSV-based Version
Your local script downloads Chartink CSV and pushes to GitHub repo.
GitHub Actions reads today's CSV and runs the full scanner pipeline.

Expected CSV filename in repo root:
  YYYY-MM-DD_All_Scans.csv

CSV format: Date, Symbol, Mcap, Change, Dpower, Mpower, Sector
VCP Daily Scanner v3 — with Sector Boost
- Loads Chartink All_Scans CSV for stock list
- Loads Preferred_Sectors CSV from sectors/ folder (separate from stocks)
- Applies Minervini 8-rule filter
- Scores with Claude AI
- Applies sector performance boost to final ranking
- Sends rich formatted picks to Telegram
"""

import os
@@ -39,6 +38,49 @@

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
@@ -64,6 +106,11 @@ def notify(msg):
# ============================================================

def load_csv():
    """
    Load all stocks from today's combined Chartink CSV.
    Filename: YYYY-MM-DD_All_Scans.csv
    Falls back up to 4 days to handle weekends/holidays.
    """
    now_ist = datetime.now(IST)

    for days_back in range(5):
@@ -96,12 +143,12 @@ def load_csv():
                    if len(sym_clean) < 2 or sym_clean in ['NAN', 'SYMBOL', 'NONE']:
                        continue

                    sec = 'NSE'
                    sec = 'nse'
                    for sc in [sec_col1, sec_col2]:
                        if sc and sc in row:
                            val = str(row[sc]).strip()
                            if val and val.lower() not in ['nan', 'none', '']:
                                sec = val.capitalize()
                                sec = val.lower().strip()
                                break

                    src = str(row.get('Scanner_Source', '')).strip()
@@ -125,10 +172,138 @@ def load_csv():


# ============================================================
# STEP 3 — TECHNICAL INDICATORS
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
@@ -192,6 +367,7 @@ def compute_indicators(df, nifty_df):


def apply_filters(symbol, ind):
    """Apply all 8 Minervini rules."""
    rules = {
        'Stage 2 MA stack'        : ind['ma_stack_ok'],
        'Base depth < 40%'        : ind['base_depth_ok'],
@@ -208,10 +384,11 @@ def apply_filters(symbol, ind):


# ============================================================
# STEP 4 — CLAUDE AI SCORING
# STEP 5 — CLAUDE AI SCORING
# ============================================================

def score_with_claude(symbol, ind, rules):
    """Score a stock setup using Claude API."""
    client = anthropic.Anthropic(api_key=CLAUDE_API_KEY)
    rules_text = "\n".join([f"  - {r}: {'PASS' if v else 'FAIL'}" for r, v in rules.items()])

@@ -284,23 +461,29 @@ def score_with_claude(symbol, ind, rules):


# ============================================================
# STEP 5 — FORMAT TELEGRAM MESSAGE
# STEP 6 — FORMAT TELEGRAM MESSAGE
# ============================================================

def score_bar(score):
    """Telegram-safe visual score bar using plain ASCII."""
    filled = round(score / 10)
    empty  = 10 - filled
    return ("[" + ("=" * filled) + ("." * empty) + "]")
    return "[" + ("=" * filled) + ("." * empty) + "]"


def format_header(today, total_picks, total_passed, top_sectors):
    """Clean header with market + top sectors summary."""
    sector_line = ""
    if top_sectors:
        sector_line = f"🏆 Top sectors: <b>{', '.join(top_sectors[:3])}</b>\n"

def format_header(today, total_picks, total_passed):
    return (
        f"📊 <b>VCP SCANNER — DAILY PICKS</b>\n"
        f"📅 {today}  |  🤖 Minervini + Claude AI\n"
        f"〰〰〰〰〰〰〰〰〰〰〰〰〰\n"
        f"🔍 Stocks passed filters: <b>{total_passed}</b>\n"
        f"🏆 Top picks selected: <b>{total_picks}</b>\n"
        f"{sector_line}"
        f"〰〰〰〰〰〰〰〰〰〰〰〰〰"
    )

@@ -317,16 +500,20 @@ def format_pick_message(pick, rank, total):
    def safe(text):
        return str(text).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')

    entry       = safe(pick['entry_zone'])
    risk        = safe(pick['key_risk'])
    hold_signal = safe(pick.get('hold_signal', 'Stay above stop loss at all times'))
    exit_signal = safe(pick.get('exit_signal', 'Exit if daily close below stop loss'))
    buy_reasons = pick.get('buy_reasons', [])
    sector      = safe(pick.get('sector', 'NSE').title())
    score       = pick['score']
    rs_rating   = pick.get('rs_rating', '-')
    vcp_pivots  = pick.get('vcp_pivots', '-')
    stage       = str(pick['vcp_stage']).upper()
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
@@ -335,16 +522,24 @@ def safe(text):
    # Stage emoji
    stage_emoji = {"EARLY": "🌱", "MID": "📈", "LATE": "🔥"}.get(stage, "📊")

    # Score icon
    if score >= 80:
    # Score icon based on BOOSTED score
    if boosted_score >= 80:
        score_icon = "🟢"
    elif score >= 65:
    elif boosted_score >= 65:
        score_icon = "🟡"
    else:
        score_icon = "🔴"

    # Score bar — plain ASCII, works on all devices
    bar = score_bar(score)
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
@@ -355,16 +550,18 @@ def safe(text):
    msg = (
        f"〰〰〰〰〰〰〰〰〰〰〰〰〰\n"
        f"{medal} <b>{pick['symbol']}</b>  {score_icon}  {stage_emoji} {stage} VCP\n"
        f"📂 {sector}  |  Pivots: {vcp_pivots}\n"
        f"📂 {sector}  |  Pivots: {vcp_pivots}  |  Sector Rank: #{sector_rank}\n"
        f"〰〰〰〰〰〰〰〰〰〰〰〰〰\n"
        f"\n"
        f"<b>Score:</b> {score}/100  <code>{bar}</code>\n"
        f"<b>Score:</b> {boosted_score}/100  <code>{bar}</code>\n"
        f"<i>Base: {score}  |  {boost_str}</i>\n"
        f"<b>RS Rating:</b> {rs_rating}  |  <b>R:R</b> 1:{rr}  |  <b>Hold:</b> ~{pick['hold_days_estimate']}d\n"
        f"\n"
        f"💰 <b>Trade Setup</b>\n"
        f"  Entry Zone   :  Rs.{entry}\n"
        f"  Pivot Level  :  Rs.{pick['pivot_level']}\n"
        f"  Stop Loss    :  Rs.{pick['stop_loss']}  🛑\n"
        f"  Current Price :  Rs.{current_price}\n"
        f"  Entry Zone    :  Rs.{entry}\n"
        f"  Pivot Level   :  Rs.{pick['pivot_level']}\n"
        f"  Stop Loss     :  Rs.{pick['stop_loss']}  🛑\n"
        f"\n"
        f"🎯 <b>Targets</b>\n"
        f"  +10%  →  Rs.{pick['target_10pct']}\n"
@@ -388,7 +585,7 @@ def safe(text):
def format_footer(total_picks):
    return (
        f"〰〰〰〰〰〰〰〰〰〰〰〰〰\n"
        f"🤖 <i>Powered by Minervini VCP + Claude AI</i>\n"
        f"🤖 <i>Powered by Minervini VCP + Claude AI + Sector Rotation</i>\n"
        f"🛑 <i>Hard stop -7% from entry. No exceptions.</i>\n"
        f"📌 <i>Do your own research before trading.</i>"
    )
@@ -401,7 +598,7 @@ def format_footer(total_picks):
def main():
    now_ist = datetime.now(IST)
    print(f"\n{'='*50}")
    print(f"VCP Scanner starting at {now_ist.strftime('%Y-%m-%d %H:%M IST')}")
    print(f"VCP Scanner v3 starting at {now_ist.strftime('%Y-%m-%d %H:%M IST')}")
    print(f"{'='*50}\n")

    notify(f"🔍 <b>VCP Scanner</b> started\n📅 {now_ist.strftime('%d %b %Y')}  |  ⏰ {now_ist.strftime('%H:%M IST')}")
@@ -419,9 +616,24 @@ def main():
        return

    print(f"\nCSV date: {csv_date} | Stocks: {len(stocks)}")
    candidates = [s['symbol'] for s in stocks]
    sector_map = {s['symbol']: s['sector'] for s in stocks}
    source_map = {s['symbol']: s['source'] for s in stocks}
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
@@ -431,7 +643,7 @@ def main():
        raw = pd.read_csv(nifty_file, parse_dates=['Date'])
        raw = raw.set_index('Date').sort_index()
        nifty = raw[['Open','High','Low','Close','Volume']].dropna()
        print(f"  Nifty: {len(nifty)} days loaded from {nifty_file}")
        print(f"  Nifty: {len(nifty)} days loaded")
    else:
        print(f"  WARNING: {nifty_file} not found — RS scores will be zero")

@@ -453,15 +665,13 @@ def main():
            except Exception as e:
                print(f"  {sym}: read error — {e}")
        else:
            print(f"  {sym}: no price file found ({price_file})")
            print(f"  {sym}: no price file found")

    if not stock_data:
        tried = ', '.join(candidates)
        send_telegram(
            f"<b>VCP Scanner — {now_ist.strftime('%d %b %Y')}</b>\n\n"
            f"Could not fetch price data for any stock.\n"
            f"Stocks tried: {tried}\n"
            f"This may happen if symbols are not listed on NSE or yfinance returned no data."
            f"Could not load price data for any stock.\n"
            f"Please check github_push.py ran correctly."
        )
        return

@@ -478,11 +688,12 @@ def main():
                print(f"    {'✓' if val else '✗'} {name}")
            if passed:
                shortlist.append({
                    'symbol': sym,
                    'sector': sector_map.get(sym, 'NSE'),
                    'indicators': ind,
                    'rules': rules,
                    'rule_score': count
                    'symbol'      : sym,
                    'sector'      : sector_map.get(sym, 'nse'),
                    'indicators'  : ind,
                    'rules'       : rules,
                    'rule_score'  : count,
                    'current_price': round(ind['current_price'], 2)
                })
        except Exception as e:
            print(f"  {sym}: error — {e}")
@@ -506,8 +717,9 @@ def main():
        print(f"  {sym}...", end=' ', flush=True)
        result = score_with_claude(sym, item['indicators'], item['rules'])
        if result:
            result['sector'] = item['sector']
            result['rule_score'] = item['rule_score']
            result['sector']        = item['sector']
            result['rule_score']    = item['rule_score']
            result['current_price'] = item['current_price']
            scored.append(result)
            print(f"{result['score']}/100")
        else:
@@ -518,31 +730,39 @@ def main():
        send_telegram("VCP Scanner: Claude scoring failed for all stocks. Check CLAUDE_API_KEY.")
        return

    scored.sort(key=lambda x: x['score'], reverse=True)
    print(f"\nScoring complete:")
    for s in scored:
        print(f"  {s['symbol']}: {s['score']}/100 [{s['sector']}]")
    # --- Apply sector boost to final scores ---
    print(f"\nApplying sector boost...")
    print(f"  {'Symbol':<15} {'Sector':<15} {'Base':>5} {'Boost':>6} {'Final':>6}  Index")
    print(f"  {'-'*60}")

    # --- Select top picks with sector diversification ---
    final_picks = []
    used_sectors = []
    for s in scored:
        if len(final_picks) >= TOP_N_PICKS:
            break
        if s['sector'] not in used_sectors or s['sector'] == 'NSE':
            final_picks.append(s)
            used_sectors.append(s['sector'])
        stock_sector = s.get('sector', 'nse')
        boost, rank, idx_sym, idx_score = get_sector_boost(stock_sector, sector_scores)

        s['sector_boost']  = boost
        s['sector_rank']   = rank
        s['sector_index']  = idx_sym
        s['boosted_score'] = min(100, s['score'] + boost)  # cap at 100

    if len(final_picks) < TOP_N_PICKS and len(scored) >= TOP_N_PICKS:
        final_picks = scored[:TOP_N_PICKS]
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

    # --- Send to Telegram — one message per stock ---
    # --- Send to Telegram ---
    today = datetime.now(IST).strftime('%d %b %Y')

    # 1. Header
    header = format_header(today, len(final_picks), len(shortlist))
    # 1. Header with top sectors
    header = format_header(today, len(final_picks), len(shortlist), top_sectors)
    r = send_telegram(header)
    print(f"Header sent: {r.get('ok')}")
    time.sleep(0.8)
@@ -565,5 +785,3 @@ def main():

if __name__ == "__main__":
    main()
