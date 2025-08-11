import asyncio
import websockets
import json
import hmac
import base64
import time

class OKXWebSocketClient:
    def __init__(self, api_key, api_secret, passphrase, is_demo=True):
        self.api_key = api_key
        self.api_secret = api_secret
        self.passphrase = passphrase
        self.ws_url = "wss://ws.okx.com:8443/ws/v5/private?brokerId=9999"
        if is_demo:
            self.ws_url = "wss://wspap.okx.com:8443/ws/v5/private?brokerId=9999"
        self.websocket = None
        self.is_running = False # 실행 상태 플래그 추가

    async def connect(self):
        self.websocket = await websockets.connect(self.ws_url)
        await self._login()

    def _get_signature(self, timestamp, method, request_path):
        message = f"{timestamp}{method}{request_path}"
        mac = hmac.new(bytes(self.api_secret, encoding='utf8'), bytes(message, encoding='utf-8'), digestmod='sha256')
        return base64.b64encode(mac.digest()).decode()

    async def _login(self):
        timestamp = str(time.time())
        signature = self._get_signature(timestamp, "GET", "/users/self/verify")
        login_payload = {
            "op": "login",
            "args": [{
                "apiKey": self.api_key,
                "passphrase": self.passphrase,
                "timestamp": timestamp,
                "sign": signature
            }]
        }
        await self.websocket.send(json.dumps(login_payload))
        response = await self.websocket.recv()
        print(f"Login Response: {response}")

    async def subscribe_to_orders(self, ticker):
        sub_payload = {
            "op": "subscribe",
            "args": [{
                "channel": "orders",
                "instType": "SWAP",
                "instId": ticker
            }]
        }
        await self.websocket.send(json.dumps(sub_payload))
        print(f"Subscribed to orders channel for {ticker}")


    async def subscribe_to_positions(self, ticker):
        """포지션 채널을 구독합니다."""
        sub_payload = {
            "op": "subscribe",
            "args": [{
                "channel": "positions",
                "instType": "SWAP",
                "instId": ticker
            }]
        }
        await self.websocket.send(json.dumps(sub_payload))
        print(f"Subscribed to positions channel for {ticker}")


    async def listen(self, event_handler):
        # 이전에 on_open을 호출하던 부분을 EventHandler의 on_open을 호출하도록 변경
        await event_handler.on_open()
        self.is_running = True # 리스닝 시작 시 플래그를 True로 설정

        while self.is_running: # while True 대신 플래그를 확인
            try:
                message = await asyncio.wait_for(self.websocket.recv(), timeout=1.0) # 타임아웃 추가
                data = json.loads(message)

                if 'event' in data and data['event'] == 'error':
                    await event_handler.on_error(data)
                elif 'arg' in data and data['arg']['channel'] == 'orders':
                    for order_data in data.get('data', []):
                        await event_handler.on_order_update(order_data)

                # --- 포지션 채널 데이터 처리 로직 추가 ---
                elif 'arg' in data and data['arg']['channel'] == 'positions':
                    for position_data in data.get('data', []):
                        await event_handler.on_position_update(position_data)

            except asyncio.TimeoutError:
                # 타임아웃은 정상적인 상황이므로 그냥 계속 진행
                continue

            except websockets.exceptions.ConnectionClosed:
                if self.is_running: # 중지 신호가 아닐 때만 재연결 시도
                    print("Connection closed unexpectedly. Reconnecting...")
                    await self.connect()
                    await self.subscribe_to_orders(event_handler.ticker)
                    await self.subscribe_to_positions(event_handler.ticker)
            except Exception as e:
                await event_handler.on_error(e)


    async def close(self):
        if self.websocket:
            await self.websocket.close()
            print("OKX WebSocket Client connection closed.")

    def stop(self): # 외부에서 호출할 stop 함수
        print("Stopping WebSocket client...")
        self.is_running = False
