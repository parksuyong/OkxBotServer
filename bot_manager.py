# bot_manager.py (수정 제안)
import threading
import asyncio
from okx_trader import OKXTrader
from event_handler import EventHandler
from database import get_db_connection
from crypto import decrypt_data
from okx_websocket_client import OKXWebSocketClient


class BotManager:
    def __init__(self):
        # 변경: 딕셔너리의 키 구조에 대한 주석 변경
        self.active_bots = {}  # {(user_id, ticker): (thread, websocket_client)}

    # 변경: 파라미터 이름 통일 (ticker_unified -> ticker)
    def start_bot(self, user_id, ticker, leverage, amount):
        # 봇을 식별하는 키를 (user_id, ticker) 튜플로 변경
        bot_key = (user_id, ticker)

        if bot_key in self.active_bots:
            print(f"Bot for user {user_id} on {ticker} is already running. Stopping it first.")
            # stop_bot 호출 시 ticker도 전달
            self.stop_bot(user_id, ticker)

        conn = get_db_connection()
        if not conn:
            return {"status": "error", "message": "DB connection failed"}
        cursor = conn.cursor(dictionary=True)
        cursor.execute("SELECT * FROM api_keys WHERE user_id = %s", (user_id,))
        keys = cursor.fetchone()
        conn.close()

        if not keys:
            return {"status": "error", "message": "API keys not found"}

        api_key = decrypt_data(keys['api_key'])
        api_secret = decrypt_data(keys['api_secret'])
        api_passphrase = decrypt_data(keys['api_passphrase'])

        ticker_exchange = ticker.replace("/", "-").split(":")[0] + "-SWAP"
        websocket_client = OKXWebSocketClient(api_key, api_secret, api_passphrase)

        # 변경: 스레드에 필요한 모든 정보를 명확하게 넘겨주도록 run_async_loop 함수 구조 변경
        def run_async_loop(uid, tkr, lvg, amt, key, sec, passphrase):

            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)

            async def task():
                trader = None # 추가: finally 블록에서 사용하기 위해 trader를 미리 선언합니다.
                try: # 추가: try...finally 구문을 추가합니다.
                    trader = await OKXTrader.create(key, sec, passphrase, tkr)
                    if trader is None:
                        print(f"Fatal: Could not initialize OKXTrader for {tkr}. Bot thread will stop.")
                        return

                    await trader.set_leverage(tkr, lvg)

                    # 명확하게 전달받은 파라미터로 EventHandler 생성
                    event_handler = EventHandler(trader, tkr, lvg, amt)

                    await websocket_client.connect()
                    await websocket_client.subscribe_to_orders(ticker_exchange)
                    await websocket_client.subscribe_to_positions(ticker_exchange)
                    await websocket_client.listen(event_handler)

                finally: # 추가: finally 블록을 추가하여 항상 실행되도록 보장합니다.
                    if trader: # 추가: trader가 성공적으로 생성되었을 경우에만 close를 호출합니다.
                        await trader.close_connection()
                        print(f"[Thread-{tkr}] Trader connection closed.")

            try:
                loop.run_until_complete(task())
            except Exception as e:
                print(f"Error in bot thread for {uid} on {tkr}: {e}")
            finally:
                loop.close()

        # 변경: 스레드 생성 시 target과 함께 args로 모든 파라미터를 명시적으로 전달
        thread = threading.Thread(target=run_async_loop, args=(user_id, ticker, leverage, amount, api_key, api_secret, api_passphrase))
        thread.start()

        # 새로운 bot_key를 사용하여 봇 정보 저장
        self.active_bots[bot_key] = (thread, websocket_client)
        print(f"Bot started for user {user_id} on {ticker} in a new thread.")
        return {"status": "success", "message": "Bot started successfully"}

    # 변경: stop_bot도 ticker를 파라미터로 받도록 수정
    def stop_bot(self, user_id, ticker):
        # 변경: 봇을 식별하는 키를 (user_id, ticker) 튜플로 변경
        bot_key = (user_id, ticker)

        if bot_key in self.active_bots:
            thread, websocket_client = self.active_bots[bot_key]

            if websocket_client:
                websocket_client.stop()

            thread.join()

            # 변경: 새로운 bot_key를 사용하여 봇 정보 삭제
            del self.active_bots[bot_key]
            print(f"Bot for user {user_id} on {ticker} stopped successfully.")
            return {"status": "success", "message": "Bot stopped successfully"}
        return {"status": "error", "message": "Bot not found or not running"}

# 싱글톤 인스턴스
bot_manager = BotManager()