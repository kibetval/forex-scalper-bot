
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API_KEY = os.environ["TWELVE_DATA_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SYMBOL = "EUR/USD"
INTERVAL = "5min"
FAST_PERIOD = 9
SLOW_PERIOD = 21
STATE_FILE = "last_signal.json"


def get_json(url, data=None):
    request = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": "ForexSignalBot/2.0"}
    )

    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f"HTTP request failed with status {error.code}"
        ) from None


def calculate_ema(values, period):
    """Calculate EMA using an SMA seed."""
    if len(values) < period:
        raise ValueError("Not enough prices to calculate EMA")

    result = [None] * len(values)
    current = sum(values[:period]) / period
    result[period - 1] = current
    multiplier = 2.0 / (period + 1)

    for i in range(period, len(values)):
        current = (
            values[i] * multiplier
            + current * (1.0 - multiplier)
        )
        result[i] = current

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
    }).encode("utf-8")

    result = get_json(url, data=payload)

    if not result.get("ok"):
        description = result.get(
            "description", "Unknown Telegram error"
        )
        raise RuntimeError(
            "Telegram rejected the message: " + description
        )


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
            "Market data unavailable: "
            + str(data.get("message", "No candle data returned"))
        )

    # Twelve Data normally returns newest candles first.
    candles = list(reversed(data["values"]))

    # Exclude the newest candle in case it is still forming.
    if len(candles) > 0:
        candles = candles[:-1]

    if len(candles) < SLOW_PERIOD + 2:
        raise RuntimeError(
            "Not enough completed candles for EMA analysis"
        )

    closes = [float(candle["close"]) for candle in candles]
    fast = calculate_ema(closes, FAST_PERIOD)
    slow = calculate_ema(closes, SLOW_PERIOD)

    current_index = len(candles) - 1
    previous_index = current_index - 1

    previous_fast = fast[previous_index]
    previous_slow = slow[previous_index]
    current_fast = fast[current_index]
    current_slow = slow[current_index]

    candle_time = candles[current_index]["datetime"]

    state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as file:
                state = json.load(file)
        except (json.JSONDecodeError, OSError):
            state = {}

    if state.get("last_checked_candle") == candle_time:
        print("This completed candle was already checked.")
        return

    signal = None

    if previous_fast <= previous_slow and current_fast > current_slow:
        signal = "BUY"
    elif previous_fast >= previous_slow and current_fast < current_slow:
        signal = "SELL"

    # Record the candle so the same candle is not alerted repeatedly.
    state["last_checked_candle"] = candle_time
    state["last_signal"] = signal

    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)

    if signal:
        message = (
            f"EUR/USD {signal} SIGNAL\n"
            f"Timeframe: M5\n"
            f"Completed candle: {candle_time}\n"
            f"Close price: {closes[current_index]:.5f}\n"
            f"Fast EMA ({FAST_PERIOD}): {current_fast:.5f}\n"
            f"Slow EMA ({SLOW_PERIOD}): {current_slow:.5f}\n"
            f"Strategy: EMA 9/21 crossover\n"
            f"Mode: SIGNAL ONLY — no trade placed\n"
            f"Generated: {datetime.now(timezone.utc).isoformat()}"
        )

        send_telegram(message)
        print("Telegram signal sent successfully.")
        print(message)
    else:
        print(
            f"No new crossover for {SYMBOL} "
            f"at {candle_time}"
        )


if __name__ == "__main__":
    main()
