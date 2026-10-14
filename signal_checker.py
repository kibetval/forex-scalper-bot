
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API_KEY = os.environ["TWELVE_DATA_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SYMBOL = "EUR/USD"
INTERVAL = "5min"
STATE_FILE = "last_signal.json"


def get_json(url):
    request = urllib.request.Request(
        url, headers={"User-Agent": "ForexSignalBot/1.0"}
    )
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.loads(response.read().decode("utf-8"))


def ema(values, period):
    result = []
    multiplier = 2 / (period + 1)
    current = sum(values[:period]) / period

    for index, value in enumerate(values):
        if index < period:
            result.append(None)
        elif index == period:
            current = sum(values[index - period + 1:index + 1]) / period
            result.append(current)
        else:
            current = value * multiplier + current * (1 - multiplier)
            result.append(current)

    return result


def send_telegram(message):
    url = (
        "https://api.telegram.org/bot"
        + TELEGRAM_TOKEN
        + "/sendMessage"
    )
    payload = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": message
    }).encode()

    request = urllib.request.Request(url, data=payload)
    with urllib.request.urlopen(request, timeout=25) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise RuntimeError("Telegram did not accept the message")


def main():
    query = urllib.parse.urlencode({
        "symbol": SYMBOL,
        "interval": INTERVAL,
        "outputsize": 100,
        "apikey": API_KEY
    })

    data = get_json(
        "https://api.twelvedata.com/time_series?" + query
    )

    if "values" not in data:
        raise RuntimeError(
            "Twelve Data error: " + str(data.get("message", data))
        )

    # API usually returns newest candle first.
    candles = list(reversed(data["values"]))

    # Exclude the newest candle because it may still be forming.
    candles = candles[:-1]

    if len(candles) < 30:
        raise RuntimeError("Not enough completed candles")

    closes = [float(c["close"]) for c in candles]
    fast = ema(closes, 9)
    slow = ema(closes, 21)

    # Compare the two most recent completed candles.
    i = len(candles) - 1
    previous = i - 1

    if fast[previous] is None or slow[previous] is None:
        raise RuntimeError("Not enough EMA data")

    candle_time = candles[i]["datetime"]

    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)

    if state.get("last_checked_candle") == candle_time:
        print("Already checked this candle.")
        return

    signal = None

    if fast[previous] <= slow[previous] and fast[i] > slow[i]:
        signal = "BUY"
    elif fast[previous] >= slow[previous] and fast[i] < slow[i]:
        signal = "SELL"

    state["last_checked_candle"] = candle_time

    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)

    if signal:
        price = closes[i]
        message = (
            f"Forex signal: {signal}\n"
            f"Pair: {SYMBOL}\n"
            f"Timeframe: {INTERVAL}\n"
            f"Completed candle: {candle_time}\n"
            f"Close: {price:.5f}\n"
            f"Strategy: EMA 9/21 crossover\n"
            f"Mode: SIGNAL ONLY — no trade placed\n"
            f"Generated: {datetime.now(timezone.utc).isoformat()}"
        )
        send_telegram(message)
        print(message)
    else:
        print(f"No new crossover for {SYMBOL} at {candle_time}")


if __name__ == "__main__":
    main()
  
