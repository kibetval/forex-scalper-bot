
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API_KEY = os.environ["TWELVE_DATA_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

MARKETS = {
    "EUR/USD": 5,
    "GBP/USD": 5,
    "USD/JPY": 3,
    "AUD/USD": 5,
}

STATE_FILE = "last_signal.json"
FAST_EMA = 9
SLOW_EMA = 21
SL_ATR = 1.5
TP_ATR = 2.25
REQUEST_DELAY = 9
last_request = 0.0


def api_request(params):
    global last_request

    for attempt in range(3):
        delay = REQUEST_DELAY - (time.monotonic() - last_request)
        if delay > 0:
            time.sleep(delay)

        url = "https://api.twelvedata.com/time_series?" + urllib.parse.urlencode(params)
        request = urllib.request.Request(url, headers={"User-Agent": "ForexSignalBot/2.0"})

        try:
            last_request = time.monotonic()
            with urllib.request.urlopen(request, timeout=30) as response:
                data = json.loads(response.read().decode())

            if data.get("status") == "error":
                raise RuntimeError(data.get("message", "Market data error"))

            if "values" not in data:
                raise RuntimeError(f"Unexpected response: {data}")

            return data["values"]

        except urllib.error.HTTPError as error:
            if error.code == 429 and attempt < 2:
                time.sleep(20 * (attempt + 1))
                continue
            raise RuntimeError(f"Market API HTTP {error.code}") from error

    raise RuntimeError("Market API retries exhausted")


def candles(symbol, interval):
    values = api_request({
        "symbol": symbol,
        "interval": interval,
        "outputsize": 100,
        "apikey": API_KEY,
        "order": "ASC",
    })

    result = [{
        "time": item["datetime"],
        "open": float(item["open"]),
        "high": float(item["high"]),
        "low": float(item["low"]),
        "close": float(item["close"]),
    } for item in values]

    result.sort(key=lambda item: item["time"])

    # Ignore the newest candle because it may still be forming.
    if len(result) > 1:
        result = result[:-1]

    if len(result) < 50:
        raise RuntimeError(f"Insufficient {interval} candles for {symbol}")

    return result


def ema(values, period):
    output = [None] * (period - 1)
    current = sum(values[:period]) / period
    output.append(current)
    multiplier = 2 / (period + 1)

    for value in values[period:]:
        current = value * multiplier + current * (1 - multiplier)
        output.append(current)

    return output


def rsi(values, period=14):
    changes = [values[i] - values[i - 1] for i in range(1, len(values))]
    gains = [max(x, 0) for x in changes]
    losses = [max(-x, 0) for x in changes]

    gain = sum(gains[:period]) / period
    loss = sum(losses[:period]) / period
    output = [None] * period

    def value(g, l):
        if l == 0:
            return 100.0 if g > 0 else 50.0
        return 100 - 100 / (1 + g / l)

    output.append(value(gain, loss))

    for i in range(period, len(gains)):
        gain = (gain * (period - 1) + gains[i]) / period
        loss = (loss * (period - 1) + losses[i]) / period
        output.append(value(gain, loss))

    return output


def atr(data, period=14):
    ranges = []

    for i, item in enumerate(data):
        if i == 0:
            tr = item["high"] - item["low"]
        else:
            previous = data[i - 1]["close"]
            tr = max(
                item["high"] - item["low"],
                abs(item["high"] - previous),
                abs(item["low"] - previous),
            )
        ranges.append(tr)

    output = [None] * (period - 1)
    current = sum(ranges[:period]) / period
    output.append(current)

    for tr in ranges[period:]:
        current = (current * (period - 1) + tr) / period
        output.append(current)

    return output


def telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": message,
    }).encode()

    request = urllib.request.Request(url, data=payload, method="POST")

    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode())

    if not result.get("ok"):
        raise RuntimeError("Telegram rejected the message")


def analyse(symbol):
    m15 = candles(symbol, "15min")
    m30 = candles(symbol, "30min")

    close15 = [x["close"] for x in m15]
    close30 = [x["close"] for x in m30]

    fast15 = ema(close15, 9)
    slow15 = ema(close15, 21)
    fast30 = ema(close30, 9)
    slow30 = ema(close30, 21)
    rsi15 = rsi(close15)
    atr15 = atr(m15)

    i = len(m15) - 1
    j = len(m30) - 1
    current = m15[i]
    previous = m15[i - 1]

    buy = (
        fast30[j] > slow30[j]
        and fast15[i] > slow15[i]
        and previous["close"] <= fast15[i - 1]
        and current["close"] > fast15[i]
        and current["close"] > current["open"]
        and 50 <= rsi15[i] <= 68
    )

    sell = (
        fast30[j] < slow30[j]
        and fast15[i] < slow15[i]
        and previous["close"] >= fast15[i - 1]
        and current["close"] < fast15[i]
        and current["close"] < current["open"]
        and 32 <= rsi15[i] <= 50
    )

    if not buy and not sell:
        print(f"{symbol}: No setup on M15 candle {current['time']}")
        return None

    direction = "BUY" if buy else "SELL"
    entry = current["close"]
    volatility = atr15[i]

    if direction == "BUY":
        stop = entry - SL_ATR * volatility
        target = entry + TP_ATR * volatility
    else:
        stop = entry + SL_ATR * volatility
        target = entry - TP_ATR * volatility

    digits = MARKETS[symbol]

    message = (
        "FOREX SIGNAL — M15 / M30\n\n"
        f"Pair: {symbol}\nDirection: {direction}\n"
        f"Entry: {entry:.{digits}f}\n"
        f"Stop Loss: {stop:.{digits}f}\n"
        f"Take Profit: {target:.{digits}f}\n"
        f"RSI: {rsi15[i]:.2f}\n"
        f"Completed M15 candle: {current['time']}\n\n"
        "Signal only. Verify the live price before trading."
    )

    return current["time"], direction, message


def main():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)
    except (OSError, json.JSONDecodeError):
        state = {}

    failures = []

    print("Forex signal bot: M15 entries, M30 trend confirmation")
    print("Markets: " + ", ".join(MARKETS))

    for symbol in MARKETS:
        try:
            signal = analyse(symbol)

            if signal is None:
                continue

            candle_time, direction, message = signal
            signature = f"{direction}|{candle_time}"

            if state.get(symbol) == signature:
                print(f"{symbol}: Already alerted for this candle")
                continue

            telegram(message)
            state[symbol] = signature

            with open(STATE_FILE, "w", encoding="utf-8") as file:
                json.dump(state, file, indent=2)

            print(f"{symbol}: Telegram alert sent")

        except Exception as error:
            failures.append(symbol)
            print(f"{symbol}: ERROR: {error}")

    with open(STATE_FILE, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)

    print(f"Finished at {datetime.now(timezone.utc).isoformat()}")
    if failures:
        print("Unavailable markets: " + ", ".join(failures))


if __name__ == "__main__":
    main()
