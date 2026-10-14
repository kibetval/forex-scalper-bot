
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# Existing GitHub Actions secrets — keep their names unchanged.
API_KEY = os.environ["TWELVE_DATA_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

SYMBOL = "EUR/USD"
INTERVAL = "1min"
FAST_PERIOD = 9
SLOW_PERIOD = 21
ATR_PERIOD = 14

# Balanced scalping risk estimates.
SL_ATR_MULTIPLIER = 1.2
TP_ATR_MULTIPLIER = 1.8

STATE_FILE = "last_signal.json"


def get_json(url, data=None):
    request = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": "ForexScalperSignalBot/3.0"}
    )

    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f"HTTP request failed with status {error.code}"
        ) from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError(
            "Network request failed or timed out"
        ) from None

    return result


def calculate_ema(values, period):
    """EMA seeded with the first period's simple moving average."""
    if len(values) < period:
        raise ValueError("Not enough prices for EMA")

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


def calculate_atr(candles, period):
    """Calculate simple-average ATR using completed candles."""
    true_ranges = []

    for i, candle in enumerate(candles):
        high = float(candle["high"])
        low = float(candle["low"])

        if i == 0:
            true_range = high - low
        else:
            previous_close = float(candles[i - 1]["close"])
            true_range = max(
                high - low,
                abs(high - previous_close),
                abs(low - previous_close)
            )

        true_ranges.append(true_range)

    if len(true_ranges) < period:
        raise ValueError("Not enough candles for ATR")

    return sum(true_ranges[-period:]) / period


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)
            return state if isinstance(state, dict) else {}
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)


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
        raise RuntimeError(
            "Telegram rejected the message: "
            + str(result.get("description", "unknown error"))
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
            + str(data.get("message", "No candles returned"))
        )

    # Convert newest-first API data into oldest-first order.
    candles = list(reversed(data["values"]))

    # Exclude the latest candle because it may still be forming.
    if candles:
        candles = candles[:-1]

    if len(candles) < SLOW_PERIOD + ATR_PERIOD + 2:
        raise RuntimeError("Not enough completed candles")

    closes = [float(candle["close"]) for candle in candles]
    fast = calculate_ema(closes, FAST_PERIOD)
    slow = calculate_ema(closes, SLOW_PERIOD)
    atr = calculate_atr(candles, ATR_PERIOD)

    i = len(candles) - 1
    previous = i - 1
    candle_time = candles[i]["datetime"]

    state = load_state()

    if state.get("last_checked_candle") == candle_time:
        print("This completed M1 candle was already checked.")
        return

    signal = None

    # Detect a crossover between the two latest completed candles.
    if fast[previous] <= slow[previous] and fast[i] > slow[i]:
        signal = "BUY"
    elif fast[previous] >= slow[previous] and fast[i] < slow[i]:
        signal = "SELL"

    if signal:
        entry = closes[i]
        sl_distance = atr * SL_ATR_MULTIPLIER
        tp_distance = atr * TP_ATR_MULTIPLIER

        if signal == "BUY":
            stop_loss = entry - sl_distance
            take_profit = entry + tp_distance
        else:
            stop_loss = entry + sl_distance
            take_profit = entry - tp_distance

        # EUR/USD pip convention: 1 pip = 0.0001.
        atr_pips = atr / 0.0001
        sl_pips = sl_distance / 0.0001
        tp_pips = tp_distance / 0.0001

        message = (
            f"EUR/USD {signal} SCALPING SIGNAL\n"
            f"Timeframe: M1 (1 minute)\n"
            f"Completed candle: {candle_time}\n"
            f"Estimated entry: {entry:.5f}\n"
            f"Estimated stop-loss: {stop_loss:.5f}\n"
            f"Estimated take-profit: {take_profit:.5f}\n"
            f"ATR ({ATR_PERIOD}): {atr_pips:.1f} pips\n"
            f"SL distance: {sl_pips:.1f} pips\n"
            f"TP distance: {tp_pips:.1f} pips\n"
            f"Risk/reward target: 1:1.5\n"
            f"Strategy: EMA 9/21 crossover + ATR\n"
            f"Mode: SIGNAL ONLY — no trade placed\n"
            f"Prices are estimates; spread and slippage "
            f"are not included.\n"
            f"Generated: {datetime.now(timezone.utc).isoformat()}"
        )

        # Save only after Telegram accepts the message.
        # If sending fails, the next run can retry this candle.
        send_telegram(message)

        state["last_checked_candle"] = candle_time
        state["last_signal"] = signal
        save_state(state)

        print("Telegram scalping signal sent successfully.")
        print(message)

    else:
        state["last_checked_candle"] = candle_time
        state["last_signal"] = None
        save_state(state)

        print(
            f"No new crossover for {SYMBOL} "
            f"on M1 at {candle_time}"
        )


if __name__ == "__main__":
    main()
