# VCP Daily Scanner — GitHub Actions Setup

Automated NSE stock scanner based on Minervini VCP methodology.
Runs every weekday at **9:00 PM IST** automatically via GitHub Actions.

---

## What It Does

1. Logs into Chartink and fetches your Screen 1 + Screen 2 results
2. Fetches your MACD breadth (Advance/Decline) numbers
3. Checks breadth gate — stops if market internals are negative
4. Applies all 8 Minervini filters on each candidate stock
5. Scores each stock via Claude AI (0–100)
6. Sends top 2 picks to your Telegram by 9:10 PM IST

---

## One-Time Setup (takes about 20 minutes)

### Step 1 — Create GitHub Account

Go to github.com and create a free account if you do not have one.

### Step 2 — Create Repository

1. Click the **+** button (top right) → **New repository**
2. Name it: `vcp-scanner`
3. Set to **Private** (important — keeps your keys safe)
4. Click **Create repository**

### Step 3 — Upload These Files

Upload all three files to your repository root:
- `vcp_scanner.py`
- `requirements.txt`
- `.github/workflows/vcp_scanner.yml`

To upload: In your repository, click **Add file → Upload files**

For the workflow file, you must create the folder structure:
- Click **Add file → Create new file**
- Type the filename as: `.github/workflows/vcp_scanner.yml`
- Paste the workflow file contents

### Step 4 — Find Your Chartink Scanner IDs

Your Chartink scanner URL looks like this:
```
https://chartink.com/screener/123456
```
The number at the end (123456) is your Scanner ID.

- Open Screen 1 in Chartink → copy the number from the URL
- Open Screen 2 in Chartink → copy the number from the URL
- Open your breadth dashboard → copy the number from the URL

### Step 5 — Add GitHub Secrets

This is where all your sensitive credentials go. GitHub encrypts these — no one can see them.

1. In your repository, click **Settings** (top menu)
2. Click **Secrets and variables → Actions** (left sidebar)
3. Click **New repository secret** for each of these:

| Secret Name | Value to Enter |
|---|---|
| `CLAUDE_API_KEY` | Your Claude API key from console.anthropic.com |
| `TELEGRAM_TOKEN` | Your Telegram bot token from @BotFather |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID |
| `CHARTINK_EMAIL` | Your Chartink login email |
| `CHARTINK_PASSWORD` | Your Chartink login password |
| `CHARTINK_SCREEN1_ID` | The number from your Screen 1 URL |
| `CHARTINK_SCREEN2_ID` | The number from your Screen 2 URL |
| `CHARTINK_BREADTH_ID` | The number from your breadth dashboard URL |

### Step 6 — Enable GitHub Actions

1. Click the **Actions** tab in your repository
2. If prompted, click **I understand my workflows, go ahead and enable them**

### Step 7 — Test Manual Run

1. Click **Actions** tab
2. Click **VCP Daily Scanner** in the left list
3. Click **Run workflow → Run workflow**
4. Watch the run — green tick = success, red X = check the logs

---

## Daily Schedule

The scanner runs automatically at **3:30 PM UTC = 9:00 PM IST** every Monday to Friday.

You do not need to do anything. Picks arrive on Telegram automatically.

---

## If Something Goes Wrong

You will receive a Telegram message saying the scanner failed.

To see what happened:
1. Go to your GitHub repository
2. Click **Actions** tab
3. Click the failed run (red X)
4. Click **run-scanner** to expand
5. Read the error message
6. Copy the error and share it for help

---

## Score Guide

| Score | Action |
|---|---|
| 80–100 | Full position size |
| 65–79 | Normal position size |
| 50–64 | Half position, watch closely |
| Below 50 | Skip |

## Hard Rules (Never Break)

- Hard stop at **-7% from entry** — exit immediately, no exceptions
- If breadth flips negative after entry — tighten stops to -3%
- If price closes below 50 SMA on volume — exit the trade
