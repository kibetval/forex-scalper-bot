
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# Keep your existing GitHub Secrets unchanged.
API_KEY = os.environ["TWELVE_DATA_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

MARKETS = {
    "EUR/USD": {"label": "EUR/USD", "precision": 5},
    "XAU/USD": {"label": "XAU/USD (GOLD)", "precision": 2},
}

INTERVAL = "1min"
FAST_PERIOD = 9
SLOW_PERIOD = 21
ATR_PERIOD = 14

SL_ATR_MULTIPLIER = 1.2
TP_ATR_MULTIPLIER = 1.8

STATE_FILE = "last_signal.json"


def get_json(url, data=None):
    request = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": "ForexGoldSignalBot/1.0"}
    )

    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(
            f"HTTP request failed: status {error.code}"
        ) from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError("Network request failed or timed out") from None

    if not isinstance(result, dict):
        raise RuntimeError("Unexpected API response format")

    return result


def calculate_ema(values, period):
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
    ranges = []

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

        ranges.append(true_range)

    if len(ranges) < period:
        raise ValueError("Not enough candles for ATR")

    return sum(ranges[-period:]) / period


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)
            if isinstance(state, dict):
                return state
    except (FileNotFoundError, json.JSONDecodeError, OSError):
        pass

    return {}


def save_state(state):
    temporary_file = STATE_FILE + ".tmp"

    with open(temporary_file, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)

    os.replace(temporary_file, STATE_FILE)


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


def check_market(symbol, market, state):
    query = urllib.parse.urlencode({
        "symbol": symbol,
        "interval": INTERVAL,
        "outputsize": 100,
        "timezone": "UTC",
        "apikey": API_KEY
    })

    url = "https://api.twelvedata.com/time_series?" + query
    data = get_json(url)

    if "values" not in data:
        reason = data.get("message", data.get("status", "No candles returned"))
        raise RuntimeError("Market data unavailable: " + str(reason))

    # API normally returns newest candle first.
    candles = list(reversed(data["values"]))

    # Exclude the newest candle because it may still be forming.
    if candles:
        candles = candles[:-1]

    minimum = max(SLOW_PERIOD, ATR_PERIOD) + 2
    if len(candles) < minimum:
        raise RuntimeError("Not enough completed M1 candles")

    closes = [float(candle["close"]) for candle in candles]
    fast = calculate_ema(closes, FAST_PERIOD)
    slow = calculate_ema(closes, SLOW_PERIOD)
    atr = calculate_atr(candles, ATR_PERIOD)

    i = len(candles) - 1
    previous = i - 1
    candle_time = candles[i]["datetime"]

    market_state = state.setdefault("markets", {}).setdefault(symbol, {})

    if market_state.get("last_checked_candle") == candle_time:
        print(f"{symbol}: this completed candle was already checked.")
        return

    signal = None

    # Detect a fresh EMA crossover on the latest completed candle.
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

        precision = market["precision"]

        # Do not call Gold movements "pips"; report raw price movement.
        if symbol == "EUR/USD":
            movement_label = "EUR/USD movement"
            unit = "pips"
            unit_size = 0.0001
        else:
            movement_label = "Gold price movement"
            unit = "USD"
            unit_size = 1.0

        atr_amount = atr / unit_size
        sl_amount = sl_distance / unit_size
        tp_amount = tp_distance / unit_size

        message = (
            f"{market['label']} {signal} SIGNAL\n"
            f"Timeframe: M1\n"
            f"Completed candle (UTC): {candle_time}\n"
            f"Estimated entry: {entry:.{precision}f}\n"
            f"Estimated stop-loss: {stop_loss:.{precision}f}\n"
            f"Estimated take-profit: {take_profit:.{precision}f}\n"
            f"ATR ({ATR_PERIOD}): {atr_amount:.2f} {unit}\n"
            f"SL distance: {sl_amount:.2f} {unit}\n"
            f"TP distance: {tp_amount:.2f} {unit}\n"
            f"Risk/reward target: 1:1.5\n"
            f"Strategy: EMA 9/21 crossover + ATR\n"
            f"Mode: SIGNAL ONLY — no trade placed\n"
            f"Prices are estimates. Spread, slippage, and broker "
            f"price differences are not included.\n"
            f"Generated: {datetime.now(timezone.utc).isoformat()}"
        )

        # Only mark the candle checked after Telegram accepts the alert.
        send_telegram(message)

        market_state["last_checked_candle"] = candle_time
        market_state["last_signal"] = signal
        save_state(state)

        print(f"{symbol}: Telegram signal sent successfully.")
    else:
        market_state["last_checked_candle"] = candle_time
        market_state["last_signal"] = None
        save_state(state)

        print(f"{symbol}: No new crossover at {candle_time} UTC.")


def main():
    state = load_state()
    failures = []

    for symbol, market in MARKETS.items():
        try:
            check_market(symbol, market, state)
        except Exception as error:
            # A problem with one market should not stop the other.
            print(f"{symbol}: ERROR: {error}")
            failures.append(symbol)

    if failures:
        raise RuntimeError(
            "Market check failed for: " + ", ".join(failures)
        )

    print("All configured markets checked successfully.")


if __name__ == "__main__":
    main()
