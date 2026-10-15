
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone

API_KEY = os.environ["TWELVE_DATA_API_KEY"]
TELEGRAM_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = os.environ["TELEGRAM_CHAT_ID"]

# Symbol, display name, price precision, pip size
MARKETS = {
    "EUR/USD":  {"name": "EUR/USD", "digits": 5, "pip": 0.0001},
    "GBP/USD":  {"name": "GBP/USD", "digits": 5, "pip": 0.0001},
    "USD/JPY":  {"name": "USD/JPY", "digits": 3, "pip": 0.01},
    "AUD/USD":  {"name": "AUD/USD", "digits": 5, "pip": 0.0001},
    "USD/CAD":  {"name": "USD/CAD", "digits": 5, "pip": 0.0001},
    "XAU/USD":  {"name": "XAU/USD (GOLD)", "digits": 2, "pip": None},
}

FAST_EMA = 9
SLOW_EMA = 21
RSI_PERIOD = 14
ATR_PERIOD = 14

SL_ATR = 1.5
TP_ATR = 2.25

STATE_FILE = "last_signal.json"


def get_json(url, data=None):
    request = urllib.request.Request(
        url,
        data=data,
        headers={"User-Agent": "MultiMarketSwingSignalBot/1.0"}
    )
    try:
        with urllib.request.urlopen(request, timeout=25) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        raise RuntimeError(f"HTTP status {error.code}") from None
    except (urllib.error.URLError, TimeoutError):
        raise RuntimeError("Network request failed or timed out") from None

    if not isinstance(result, dict):
        raise RuntimeError("Unexpected API response")
    return result


def get_candles(symbol, interval):
    query = urllib.parse.urlencode({
        "symbol": symbol,
        "interval": interval,
        "outputsize": 100,
        "timezone": "UTC",
        "apikey": API_KEY
    })
    result = get_json(
        "https://api.twelvedata.com/time_series?" + query
    )

    if "values" not in result:
        raise RuntimeError(
            str(result.get("message", result.get("status", "No candle data")))
        )

    # Twelve Data returns newest candles first.
    candles = list(reversed(result["values"]))

    # Ignore the newest candle because it may still be forming.
    if candles:
        candles = candles[:-1]

    if len(candles) < 50:
        raise RuntimeError(f"Not enough completed {interval} candles")

    for candle in candles:
        for field in ("open", "high", "low", "close"):
            candle[field] = float(candle[field])

    return candles


def ema(values, period):
    output = [None] * len(values)
    if len(values) < period:
        return output

    current = sum(values[:period]) / period
    output[period - 1] = current
    multiplier = 2 / (period + 1)

    for i in range(period, len(values)):
        current = values[i] * multiplier + current * (1 - multiplier)
        output[i] = current

    return output


def rsi(values, period=14):
    output = [None] * len(values)
    if len(values) <= period:
        return output

    gains = []
    losses = []

    for i in range(1, len(values)):
        change = values[i] - values[i - 1]
        gains.append(max(change, 0))
        losses.append(max(-change, 0))

    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period

    def value(gain, loss):
        if loss == 0:
            return 100.0 if gain > 0 else 50.0
        return 100 - 100 / (1 + gain / loss)

    output[period] = value(avg_gain, avg_loss)

    for i in range(period + 1, len(values)):
        avg_gain = (
            avg_gain * (period - 1) + gains[i - 1]
        ) / period
        avg_loss = (
            avg_loss * (period - 1) + losses[i - 1]
        ) / period
        output[i] = value(avg_gain, avg_loss)

    return output


def atr(candles, period=14):
    ranges = []

    for i, candle in enumerate(candles):
        high = candle["high"]
        low = candle["low"]

        if i == 0:
            tr = high - low
        else:
            previous_close = candles[i - 1]["close"]
            tr = max(
                high - low,
                abs(high - previous_close),
                abs(low - previous_close)
            )
        ranges.append(tr)

    return sum(ranges[-period:]) / period


def load_state():
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as file:
            state = json.load(file)
            return state if isinstance(state, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def save_state(state):
    temporary = STATE_FILE + ".tmp"
    with open(temporary, "w", encoding="utf-8") as file:
        json.dump(state, file, indent=2)
    os.replace(temporary, STATE_FILE)


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
            "Telegram error: " + str(result.get("description", "unknown"))
        )


def analyze(symbol, market, state):
    # Analyze completed H1 candles for entry and H4 candles for trend.
    h1 = get_candles(symbol, "1h")
    h4 = get_candles(symbol, "4h")

    h1_close = [c["close"] for c in h1]
    h4_close = [c["close"] for c in h4]

    fast1 = ema(h1_close, FAST_EMA)
    slow1 = ema(h1_close, SLOW_EMA)
    fast4 = ema(h4_close, FAST_EMA)
    slow4 = ema(h4_close, SLOW_EMA)
    rsi1 = rsi(h1_close, RSI_PERIOD)
    volatility = atr(h1, ATR_PERIOD)

    i = len(h1) - 1
    p = i - 1
    j = len(h4) - 1

    candle_time = h1[i]["datetime"]
    market_state = state.setdefault("markets", {}).setdefault(symbol, {})

    if market_state.get("last_checked_candle") == candle_time:
        print(f"{symbol}: H1 candle already checked.")
        return

    close_now = h1[i]["close"]
    close_before = h1[p]["close"]
    open_now = h1[i]["open"]
    rsi_now = rsi1[i]

    if any(x is None for x in (
        fast1[i], fast1[p], slow1[i],
        fast4[j], slow4[j], rsi_now
    )):
        raise RuntimeError("Indicator calculation incomplete")

    # H4 must confirm the trend.
    h4_bullish = fast4[j] > slow4[j]
    h4_bearish = fast4[j] < slow4[j]

    # H1 must be trending in the same direction.
    h1_bullish = fast1[i] > slow1[i]
    h1_bearish = fast1[i] < slow1[i]

    # Wait for price to cross back over EMA 9 after a pullback.
    # Also require a confirming candle and a sensible RSI range.
    buy = (
        h4_bullish
        and h1_bullish
        and close_before <= fast1[p]
        and close_now > fast1[i]
        and close_now > open_now
        and 50 <= rsi_now <= 68
    )

    sell = (
        h4_bearish
        and h1_bearish
        and close_before >= fast1[p]
        and close_now < fast1[i]
        and close_now < open_now
        and 32 <= rsi_now <= 50
    )

    signal = "BUY" if buy else "SELL" if sell else None

    if signal is None:
        market_state["last_checked_candle"] = candle_time
        market_state["last_signal"] = None
        save_state(state)
        print(f"{symbol}: No qualified setup on H1 candle {candle_time}.")
        return

    entry = close_now
    sl_distance = volatility * SL_ATR
    tp_distance = volatility * TP_ATR

    if signal == "BUY":
        stop_loss = entry - sl_distance
        take_profit = entry + tp_distance
        trend_text = "Bullish H1 + H4"
    else:
        stop_loss = entry + sl_distance
        take_profit = entry - tp_distance
        trend_text = "Bearish H1 + H4"

    digits = market["digits"]

    if market["pip"] is None:
        atr_text = f"${volatility:.2f}"
        sl_text = f"${sl_distance:.2f}"
        tp_text = f"${tp_distance:.2f}"
    else:
        pip = market["pip"]
        atr_text = f"{volatility / pip:.1f} pips"
        sl_text = f"{sl_distance / pip:.1f} pips"
        tp_text = f"{tp_distance / pip:.1f} pips"

    message = (
        f"QUALIFIED SWING SIGNAL: {market['name']} {signal}\n\n"
        f"Entry timeframe: H1\n"
        f"Trend confirmation: H4\n"
        f"Completed H1 candle (UTC): {candle_time}\n"
        f"Estimated entry: {entry:.{digits}f}\n"
        f"Estimated stop-loss: {stop_loss:.{digits}f}\n"
        f"Estimated take-profit: {take_profit:.{digits}f}\n\n"
        f"Trend: {trend_text}\n"
        f"RSI (14): {rsi_now:.1f}\n"
        f"ATR (14): {atr_text}\n"
        f"Estimated SL distance: {sl_text}\n"
        f"Estimated TP distance: {tp_text}\n"
        f"Target reward/risk: 1.5:1\n\n"
        f"Filters: H4 trend + H1 trend + pullback recovery "
        f"+ candle confirmation + RSI\n"
        f"Mode: SIGNAL ONLY — no trade placed.\n"
        f"These are estimates, not guaranteed entries. "
        f"Spread, slippage and broker prices may differ.\n"
        f"Generated: {datetime.now(timezone.utc).isoformat()}"
    )

    # Save signal state only after Telegram confirms delivery.
    send_telegram(message)
    market_state["last_checked_candle"] = candle_time
    market_state["last_signal"] = signal
    save_state(state)
    print(f"{symbol}: Telegram {signal} alert sent successfully.")


def main():
    state = load_state()
    errors = []

    for symbol, market in MARKETS.items():
        try:
            analyze(symbol, market, state)
        except Exception as error:
            print(f"{symbol}: ERROR: {error}")
            errors.append(symbol)

    if errors:
        raise RuntimeError(
            "Market checks failed for: " + ", ".join(errors)
        )

    print("All configured markets checked successfully.")


if __name__ == "__main__":
    main()
