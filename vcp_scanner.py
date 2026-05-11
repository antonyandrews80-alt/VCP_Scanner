"""
VCP Scanner - Telegram Sender
Reads today's Telegram summary from GitHub and sends it to Telegram
"""

import os
import requests
from datetime import datetime
from pathlib import Path

def send_to_telegram(message):
    """Send message to Telegram"""
    TELEGRAM_TOKEN = os.getenv('TELEGRAM_TOKEN')
    TELEGRAM_CHAT_ID = os.getenv('TELEGRAM_CHAT_ID')
    
    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: Telegram credentials not set in secrets!")
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
            print("✅ Telegram message sent successfully!")
            return True
        else:
            print(f"❌ Telegram send failed: {response.status_code}")
            print(f"Response: {response.text}")
            return False
    except Exception as e:
        print(f"❌ Telegram error: {e}")
        return False


def main():
    """Find and send today's Telegram summary"""
    print("="*55)
    print("VCP Scanner - Telegram Sender")
    print(f"Time: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("="*55)
    
    # Get today's date
    today = datetime.now().strftime('%Y-%m-%d')
    summary_file = f"{today}_Telegram_Summary.txt"
    
    print(f"\nLooking for: {summary_file}")
    
    # Check if file exists
    if not Path(summary_file).exists():
        print(f"❌ ERROR: {summary_file} not found!")
        print("\nAvailable summary files:")
        for f in sorted(Path('.').glob('*_Telegram_Summary.txt'), reverse=True):
            print(f"  - {f.name}")
        return
    
    # Read the summary
    print(f"✅ Found {summary_file}")
    with open(summary_file, 'r', encoding='utf-8') as f:
        message = f.read()
    
    print(f"\nMessage length: {len(message)} characters")
    print("\n" + "="*55)
    print("MESSAGE PREVIEW:")
    print("="*55)
    print(message[:500] + "..." if len(message) > 500 else message)
    print("="*55)
    
    # Send to Telegram
    print("\nSending to Telegram...")
    success = send_to_telegram(message)
    
    if success:
        print("\n✅ SUCCESS! Check your Telegram for the message.")
    else:
        print("\n❌ FAILED to send Telegram message.")


if __name__ == "__main__":
    main()
