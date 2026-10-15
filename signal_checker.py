
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

# ============================================================
# CONFIGURATION
# ============================================================

API_KEY = os.environ["TWELVE_DATA_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Only these four markets are enabled.
MARKETS = {
    "EUR/USD": {"digits": 5},
    "GBP/USD": {"digits": 5},
    "USD/JPY": {"digits": 3},
    "AUD/USD": {"digits": 5},
}

FAST_EMA = 9
SLOW_EMA = 21
RSI_PERIOD = 14
ATR_PERIOD = 14

SL_ATR = 1.5
TP_ATR = 2.25

STATE_FILE = "last_signal.json"

# Space requests out to reduce rate-limit errors.
REQUEST_INTERVAL = 9
MAX_RETRIES = 2
last_request_time = 0.0


# ============================================================
# MARKET DATA
# ============================================================

def get_json(url, params):
    global last_request_time

    for attempt in range(MAX_RETRIES + 1):
        elapsed = time.monotonic() - last_request_time
        if elapsed < REQUEST_INTERVAL:
            time.sleep(REQUEST_INTERVAL - elapsed)

        request_url = url + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(
            request_url,
            headers={"User-Agent": "ForexSignalBot/1.0"}
        )

        try:
            last_request_time = time.monotonic()

            with urllib.request.urlopen(req, timeout=30) as response:
                data = json.loads(response.read().decode("utf-8"))

            # Twelve Data can report API errors in JSON.
            if "status" in data and data["status"] == "error":
                message = data.get("message", "Unknown API error")
                code = data.get("code")

                if code == 429 or "rate limit" in message.lower():
                    if attempt < MAX_RETRIES:
                        wait = 20 * (attempt + 1)
                        print(f"Rate limit reached. Waiting {wait}s...")
                        time.sleep(wait)
                        continue

                raise RuntimeError(
                    f"Twelve Data error {code}: {message}"
                )

            if "values" not in data:
                raise RuntimeError(
                    f"Unexpected market-data response: {data}"
                )

            return data

        except urllib.error.HTTPError as error:
            if error.code == 429 and attempt < MAX_RETRIES:
                wait = 20 * (attempt + 1)
                print(f"HTTP 429. Retrying in {wait}s...")
                time.sleep(wait)
                continue

            raise RuntimeError(
                f"Market data HTTP error: {error.code}"
            ) from error

        except urllib.error.URLError as error:
            if attempt < MAX_RETRIES:
                time.sleep(5)
                continue

            raise RuntimeError(
                f"Market data connection error: {error}"
            ) from error

    raise RuntimeError("Market-data retries exhausted.")


def get_candles(symbol, interval):
    data = get_json(
        "https://api.twelvedata.com/time_series",
        {
            "symbol": symbol,
            "interval": interval,
            "outputsize": 100,
            "apikey": API_KEY,
            "order": "ASC",
        },
    )

    candles = []

    for item in data["values"]:
        candles.append({
            "time": item["datetime"],
            "open": float(item["open"]),
            "high": float(item["high"]),
            "low": float(item["low"]),
            "close": float(item["close"]),
        })

    # Ensure oldest-to-newest order.
    candles.sort(key=lambda candle: candle["time"])

    # Exclude the newest candle because it may still be forming.
    if len(candles) > 1:
        candles = candles[:-1]

    if len(candles) < 50:
        raise RuntimeError(
            f"Not enough completed {interval} candles for {symbol}."
        )

    return candles


# ============================================================
# TECHNICAL INDICATORS
# ============================================================

def ema(values, period):
    if len(values) < period:
        raise ValueError("Not enough data to calculate EMA.")

    result = [None] * (period - 1)
    current = sum(values[:period]) / period
    result.append(current)

    multiplier = 2 / (period + 1)

    for value in values[period:]:
        current = (
            value * multiplier
            + current * (1 - multiplier)
        )
        result.append(current)

    return result


def rsi(values, period=14):
    if len(values) <= period:
        raise ValueError("Not enough data to calculate RSI.")

    changes = [
        values[i] - values[i - 1]
        for i in range(1, len(values))
    ]

    gains = [max(change, 0) for change in changes]
    losses = [max(-change, 0) for change in changes]

    average_gain = sum(gains[:period]) / period
    average_loss = sum(losses[:period]) / period

    result = [None] * period

    def calculate_rsi(gain, loss):
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        relative_strength = gain / loss
        return 100 - (100 / (1 + relative_strength))

    result.append(calculate_rsi(average_gain, average_loss))

    for i in range(period, len(gains)):
        average_gain = (
            average_gain * (period - 1) + gains[i]
        ) / period

        average_loss = (
            average_loss * (period - 1) + losses[i]
        ) / period

        result.append(
            calculate_rsi(average_gain, average_loss)
        )

    return result


def atr(candles, period=14):
    if len(candles) <= period:
        raise ValueError("Not enough data to calculate ATR.")

    true_ranges = []

    for i, candle in enumerate(candles):
        if i == 0:
            true_range = candle["high"] - candle["low"]
        else:
            previous_close = candles[i - 1]["close"]

            true_range = max(
                candle["high"] - candle["low"],
                abs(candle["high"] - previous_close),
                abs(candle["low"] - previous_close),
            )

        true_ranges.append(true_range)

    result = [None] * (period - 1)
    current = sum(true_ranges[:period]) / period
    result.append(current)

    for value in true_ranges[period:]:
        current = (
            current * (period - 1) + value
        ) / period
        result.append(current)

    return result


# ============================================================
# STATE AND TELEGRAM
# ============================================================

def load_state():
    if not os.path.exists(STATE_FILE):
        return {}

    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)

        return state if isinstance(state, dict) else {}

    except (OSError, json.JSONDecodeError):
        print("Could not read saved signal state; starting fresh.")
        return {}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)


def send_telegram(message):
    url = (
        f"https://api.telegram.org/bot"
        f"{TELEGRAM_TOKEN}/sendMessage"
    )

    payload = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": message,
    }).encode("utf-8")

    request = urllib.request.Request(
        url,
        data=payload,
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))

    if not result.get("ok"):
        raise RuntimeError(
            f"Telegram rejected the message: {result}"
        )


# ============================================================
# SIGNAL ANALYSIS
# ============================================================

def analyze(symbol):
    h1 = get_candles(symbol, "1h")
    h4 = get_candles(symbol, "4h")

    h1_closes = [candle["close"] for candle in h1]
    h4_closes = [candle["close"] for candle in h4]

    h1_fast = ema(h1_closes, FAST_EMA)
    h1_slow = ema(h1_closes, SLOW_EMA)

    h4_fast = ema(h4_closes, FAST_EMA)
    h4_slow = ema(h4_closes, SLOW_EMA)

    rsi_values = rsi(h1_closes, RSI_PERIOD)
    atr_values = atr(h1, ATR_PERIOD)

    current = h1[-1]
    previous = h1[-2]

    i = len(h1) - 1
    j = len(h4) - 1

    current_rsi = rsi_values[i]
    current_atr = atr_values[i]

    if current_rsi is None or current_atr is None:
        return None

    # H4 trend confirmation.
    bullish_h4 = h4_fast[j] > h4_slow[j]
    bearish_h4 = h4_fast[j] < h4_slow[j]

    # H1 trend alignment.
    bullish_h1 = h1_fast[i] > h1_slow[i]
    bearish_h1 = h1_fast[i] < h1_slow[i]

    # Pullback recovery through EMA 9.
    bullish_recovery = (
        previous["close"] <= h1_fast[i - 1]
        and current["close"] > h1_fast[i]
        and current["close"] > current["open"]
    )

    bearish_recovery = (
        previous["close"] >= h1_fast[i - 1]
        and current["close"] < h1_fast[i]
        and current["close"] < current["open"]
    )

    direction = None

    if (
        bullish_h4
        and bullish_h1
        and bullish_recovery
        and 50 <= current_rsi <= 68
    ):
        direction = "BUY"

    elif (
        bearish_h4
        and bearish_h1
        and bearish_recovery
        and 32 <= current_rsi <= 50
    ):
        direction = "SELL"

    if direction is None:
        print(
            f"{symbol}: No qualified setup on H1 candle "
            f"{current['time']}."
        )
        return None

    entry = current["close"]

    if direction == "BUY":
        stop_loss = entry - SL_ATR * current_atr
        take_profit = entry + TP_ATR * current_atr
    else:
        stop_loss = entry + SL_ATR * current_atr
        take_profit = entry - TP_ATR * current_atr

    digits = MARKETS[symbol]["digits"]

    return {
        "symbol": symbol,
        "direction": direction,
        "candle_time": current["time"],
        "entry": entry,
        "stop_loss": stop_loss,
        "take_profit": take_profit,
        "rsi": current_rsi,
        "atr": current_atr,
        "digits": digits,
    }


def format_signal(signal):
    digits = signal["digits"]

    return (
        "FOREX SWING SIGNAL\n\n"
        f"Pair: {signal['symbol']}\n"
        f"Direction: {signal['direction']}\n"
        "Entry timeframe: H1\n"
        "Trend confirmation: H4\n\n"
        f"Entry: {signal['entry']:.{digits}f}\n"
        f"Stop Loss: {signal['stop_loss']:.{digits}f}\n"
        f"Take Profit: {signal['take_profit']:.{digits}f}\n\n"
        f"RSI (14): {signal['rsi']:.2f}\n"
        f"ATR (14): {signal['atr']:.{digits}f}\n"
        "Strategy: EMA 9/21 trend, pullback recovery, "
        "candle confirmation and RSI filter.\n\n"
        f"Completed H1 candle: {signal['candle_time']}\n"
        "Signal only — no automatic trade execution. "
        "Verify the market price before entering."
    )


# ============================================================
# MAIN CHECK
# ============================================================

def main():
    state = load_state()
    successful_checks = 0
    failed_markets = []

    print(
        "Starting Forex Signal Bot at "
        + datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    )
    print("Enabled markets: " + ", ".join(MARKETS.keys()))

    for symbol in MARKETS:
        try:
            signal = analyze(symbol)
            successful_checks += 1

            if signal is None:
                continue

            signature = (
                f"{signal['direction']}|{signal['candle_time']}"
            )

            if state.get(symbol) == signature:
                print(f"{symbol}: This signal was already sent.")
                continue

            message = format_signal(signal)
            send_telegram(message)

            state[symbol] = signature
            save_state(state)

            print(f"{symbol}: Telegram signal sent.")

        except Exception as error:
            failed_markets.append(symbol)
            print(f"{symbol}: ERROR: {error}")
            continue

    # Always save the state file, including on a no-signal run.
    save_state(state)

    if failed_markets:
        print(
            "Some markets were unavailable: "
            + ", ".join(failed_markets)
        )

    if successful_checks == 0:
        print("No markets could be analysed during this run.")
    else:
        print(
            f"Finished. Successfully analysed {successful_checks} "
            f"of {len(MARKETS)} markets."
        )


if __name__ == "__main__":
    main()
