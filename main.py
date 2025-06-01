import asyncio
import websockets
import json
from collections import deque
import csv
import os
from datetime import datetime
import threading
import math
import time

API_TOKEN = "sSldmYDuVtYpjP2"
API_URL = "wss://ws.derivws.com/websockets/v3?app_id=1089"
SYMBOL = "R_75"
SMA_PERIOD = 10  # Number of ticks for SMA

LOG_FILE = "trade_log.csv"

RSI_PERIOD = 14
BB_PERIOD = 20
BB_STD_DEV = 2

def log_trade(trade_type, price, sma, rsi, bb_upper, bb_lower, pnl, result):
    file_exists = os.path.isfile(LOG_FILE)
    with open(LOG_FILE, mode="a", newline="") as file:
        writer = csv.writer(file)
        if not file_exists:
            writer.writerow([
                "timestamp", "trade_type", "price", "sma", "rsi",
                "bb_upper", "bb_lower", "pnl", "result"
            ])
        writer.writerow([
            datetime.utcnow().isoformat(), trade_type, price, sma, rsi,
            bb_upper, bb_lower, pnl, result
        ])

async def authorize(ws):
    await ws.send(json.dumps({"authorize": API_TOKEN}))
    response = await ws.recv()
    print("Authorization:", response)

async def subscribe_ticks(ws):
    await ws.send(json.dumps({"ticks": SYMBOL, "subscribe": 1}))
    print(f"Subscribed to {SYMBOL} ticks.")

async def buy_contract(trade_ws, amount=1, contract_type="CALL"):
    proposal = {
        "buy": 1,
        "price": amount,
        "parameters": {
            "amount": amount,
            "basis": "stake",
            "contract_type": contract_type,  # "CALL" or "PUT"
            "currency": "USD",
            "duration": 1,
            "duration_unit": "m",
            "symbol": SYMBOL
        }
    }
    await trade_ws.send(json.dumps(proposal))
    print(f"Buy {contract_type} contract sent.")
    while True:
        msg = await trade_ws.recv()
        data = json.loads(msg)
        if data.get("msg_type") == "buy":
            print(f"Buy {contract_type} response:", data)
            return data
        elif data.get("msg_type") == "error":
            print(f"Buy {contract_type} error:", data)
            return None

async def tick_listener(tick_queue):
    async with websockets.connect(API_URL, ping_interval=20, ping_timeout=20) as ws:
        await authorize(ws)
        await subscribe_ticks(ws)
        while True:
            msg = await ws.recv()
            data = json.loads(msg)
            if data.get("msg_type") == "tick":
                await tick_queue.put(float(data["tick"]["quote"]))
            await asyncio.sleep(0.1)

def calculate_sma(prices, period):
    if len(prices) < period:
        return None
    return sum(prices) / period

def calculate_rsi(prices, period=RSI_PERIOD):
    if len(prices) < period + 1:
        return None
    gains = []
    losses = []
    for i in range(-period, 0):
        change = prices[i] - prices[i-1]
        if change > 0:
            gains.append(change)
        else:
            losses.append(abs(change))
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0
    if avg_loss == 0:
        return 100
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_bollinger_bands(prices, period=BB_PERIOD, num_std_dev=BB_STD_DEV):
    prices_list = list(prices)
    if len(prices_list) < period:
        return None, None, None
    sma = sum(prices_list[-period:]) / period
    variance = sum((p - sma) ** 2 for p in prices_list[-period:]) / period
    std_dev = math.sqrt(variance)
    upper_band = sma + num_std_dev * std_dev
    lower_band = sma - num_std_dev * std_dev
    return lower_band, sma, upper_band

async def get_balance(ws):
    await ws.send(json.dumps({"balance": 1, "subscribe": 0}))
    while True:
        msg = await ws.recv()
        data = json.loads(msg)
        if data.get("msg_type") == "balance":
            return data["balance"]["balance"]

async def trade_executor(tick_queue):
    price_history = deque(maxlen=max(SMA_PERIOD, RSI_PERIOD + 2, BB_PERIOD, 3))
    last_signal = None
    starting_balance = None
    current_balance = None
    last_trade_time = 0
    trade_cooldown = 60  # seconds
    loss_cooldown = 180  # longer cooldown after a loss

    base_stake = 1
    current_stake = base_stake

    async with websockets.connect(API_URL, ping_interval=20, ping_timeout=20) as trade_ws:
        await authorize(trade_ws)
        starting_balance = await get_balance(trade_ws)
        print(f"Starting balance: {starting_balance}")
        current_balance = starting_balance

        prev_rsi = None
        last_trade_result = None

        while True:
            price = await tick_queue.get()
            price_history.append(price)
            sma = calculate_sma(price_history, SMA_PERIOD)
            rsi = calculate_rsi(price_history, RSI_PERIOD)
            lower_bb, mid_bb, upper_bb = calculate_bollinger_bands(price_history, BB_PERIOD, BB_STD_DEV)
            print(f"Tick: {price}, SMA: {sma}, RSI: {rsi}, BB: ({lower_bb}, {mid_bb}, {upper_bb})")

            now = time.time()
            cooldown = loss_cooldown if last_trade_result == "LOSS" else trade_cooldown
            can_trade = (now - last_trade_time) >= cooldown

            # Candlestick confirmation: bullish/bearish engulfing
            if len(price_history) >= 3:
                change1 = price_history[-1] - price_history[-2]
                change2 = price_history[-2] - price_history[-3]
                bullish_engulfing = change1 > 0 and change2 < 0 and abs(change1) > abs(change2)
                bearish_engulfing = change1 < 0 and change2 > 0 and abs(change1) > abs(change2)
            else:
                bullish_engulfing = bearish_engulfing = False

            # RSI cross confirmation with minimum distance
            rsi_cross_up = (
                prev_rsi is not None and prev_rsi < 48 and rsi is not None and rsi > 52
            )
            rsi_cross_down = (
                prev_rsi is not None and prev_rsi > 52 and rsi is not None and rsi < 48
            )

            # Avoid trading during high volatility
            if upper_bb and lower_bb and (upper_bb - lower_bb) / mid_bb > 0.05:
                await asyncio.sleep(1)
                prev_rsi = rsi
                continue

            if sma is not None and rsi is not None and lower_bb is not None and can_trade:
                # CALL: price > sma, RSI crosses above 50, bullish engulfing, price < upper_bb
                if (
                    price > sma and
                    rsi_cross_up and
                    bullish_engulfing and
                    price < upper_bb
                ):
                    trade_type = "CALL"
                    buy_result = await buy_contract(trade_ws, amount=current_stake, contract_type=trade_type)
                    if buy_result:
                        balance_before = current_balance
                        await asyncio.sleep(65)
                        current_balance = await get_balance(trade_ws)
                        pnl = current_balance - starting_balance
                        trade_pnl = current_balance - balance_before
                        result = "WIN" if trade_pnl > 0 else "LOSS"
                        print(f"CONFIRMED {trade_type} trade. PnL: {pnl:.2f} | Trade Result: {result} | Stake: {current_stake}")
                        log_trade(
                            trade_type, price, sma, rsi, upper_bb, lower_bb, pnl,
                            result
                        )
                        last_signal = "above"
                        last_trade_time = now
                        last_trade_result = result

                        # Martingale logic
                        if result == "WIN":
                            current_stake = base_stake
                        else:
                            current_stake *= 2

                # PUT: price < sma, RSI crosses below 50, bearish engulfing, price > lower_bb
                elif (
                    price < sma and
                    rsi_cross_down and
                    bearish_engulfing and
                    price > lower_bb
                ):
                    trade_type = "PUT"
                    buy_result = await buy_contract(trade_ws, amount=current_stake, contract_type=trade_type)
                    if buy_result:
                        balance_before = current_balance
                        await asyncio.sleep(65)
                        current_balance = await get_balance(trade_ws)
                        pnl = current_balance - starting_balance
                        trade_pnl = current_balance - balance_before
                        result = "WIN" if trade_pnl > 0 else "LOSS"
                        print(f"CONFIRMED {trade_type} trade. PnL: {pnl:.2f} | Trade Result: {result} | Stake: {current_stake}")
                        log_trade(
                            trade_type, price, sma, rsi, upper_bb, lower_bb, pnl,
                            result
                        )
                        last_signal = "below"
                        last_trade_time = now
                        last_trade_result = result

                        # Martingale logic
                        if result == "WIN":
                            current_stake = base_stake
                        else:
                            current_stake *= 2

            prev_rsi = rsi
            await asyncio.sleep(1)

async def main():
    while True:
        try:
            tick_queue = asyncio.Queue()
            await asyncio.gather(
                tick_listener(tick_queue),
                trade_executor(tick_queue)
            )
        except Exception as e:
            print(f"Error occurred: {e}. Reconnecting in 5 seconds...")
            await asyncio.sleep(5)

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("Bot stopped.")