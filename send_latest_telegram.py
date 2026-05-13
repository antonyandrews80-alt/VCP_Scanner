"""
Send only TODAY's Telegram summary
Prevents sending old summaries from previous days
"""

import os
import sys
import requests
from datetime import datetime
from pathlib import Path

def send_to_telegram(message):
    """Send message to Telegram"""
    TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
    
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: Telegram credentials not set!")
        return False
    
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown"
    }
    
    try:
        response = requests.post(url, json=payload)
        if response.status_code == 200:
            print(f"✅ Telegram message sent!")
            return True
        else:
            print(f"❌ Failed: {response.status_code}")
            return False
    except Exception as e:
        print(f"❌ Error: {e}")
        return False


def main():
    """Send ONLY today's summary"""
    print("="*55)
    print("Send Latest Telegram Summary")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*55)
    
    # Get TODAY's date (UTC date matches what GitHub Actions uses)
    today = datetime.utcnow().strftime('%Y-%m-%d')
    summary_file = f"{today}_Telegram_Summary.txt"
    
    print(f"\nLooking for TODAY's file: {summary_file}")
    
    # Check if TODAY's file exists
    if not Path(summary_file).exists():
        print(f"❌ Today's summary not found. This is not today's push.")
        print("Available files:")
        for f in sorted(Path('.').glob('*_Telegram_Summary.txt'), reverse=True)[:3]:
            print(f"  - {f.name}")
        sys.exit(0)  # Exit successfully (not an error)
    
    # Read TODAY's summary
    print(f"✅ Found today's summary!")
    with open(summary_file, 'r', encoding='utf-8') as f:
        full_message = f.read()
    
    # Split into 2 parts at "TOP 10 ANALYSIS"
    if "🔍 *TOP 10 ANALYSIS*" in full_message:
        parts = full_message.split("🔍 *TOP 10 ANALYSIS*")
        part1 = parts[0].strip()
        part2 = "🔍 *TOP 10 ANALYSIS*" + parts[1]
        
        print("\nSending Part 1 (Summary)...")
        send_to_telegram(part1)
        
        print("\nWaiting 2 seconds...")
        import time
        time.sleep(2)
        
        print("\nSending Part 2 (Detailed Analysis)...")
        send_to_telegram(part2)
    else:
        # Send as one message if split marker not found
        print("\nSending complete message...")
        send_to_telegram(full_message)
    
    print("\n✅ Done!")


if __name__ == "__main__":
    main()
