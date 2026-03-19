"""
get_chat_id.py — Find your Telegram chat ID.

Usage:
  1. Set TELEGRAM_BOT_TOKEN in .env
  2. Run: python get_chat_id.py
  3. Open Telegram → Send /start to your bot
  4. Your chat ID will appear in terminal
"""

import os
import time
import requests
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
if not TOKEN:
    print("❌ TELEGRAM_BOT_TOKEN not set in .env")
    exit(1)

print("📱 Telegram Chat ID Finder")
print("=" * 40)
print("Step 1: Open Telegram and send a message to your bot (type /start)")
print("Step 2: Waiting for message (checking every 3 seconds)...\n")

seen = set()
for attempt in range(30):
    try:
        r = requests.get(f"https://api.telegram.org/bot{TOKEN}/getUpdates", timeout=10)
        data = r.json()
        updates = data.get("result", [])
        for update in updates:
            chat = update.get("message", {}).get("chat", {})
            chat_id = chat.get("id")
            username = chat.get("username", "")
            first_name = chat.get("first_name", "")
            if chat_id and chat_id not in seen:
                seen.add(chat_id)
                print(f"✅ Found chat!")
                print(f"   Name:    {first_name} (@{username})")
                print(f"   Chat ID: {chat_id}")
                print(f"\nAdd to .env:")
                print(f"   TELEGRAM_CHAT_ID={chat_id}")
                exit(0)
    except Exception as e:
        print(f"Error: {e}")

    print(f"Waiting... (attempt {attempt + 1}/30)")
    time.sleep(3)

print("❌ No message received. Make sure you sent a message to your bot.")
