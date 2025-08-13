# okx_trader.py
import ccxt.async_support as ccxt
import traceback
import time
import uuid
import asyncio

class OKXTrader:
    def __init__(self, exchange_instance):
        self.exchange = exchange_instance
        # 기본 모드 (필요 시 외부에서 설정 가능)
        self.default_pos_side = "short"     # "short" / "long"
        self.default_td_mode = "isolated"   # "isolated" / "cross"

    @classmethod
    async def create(cls, api_key, api_secret, passphrase, ticker):
        """거래소 객체를 생성하고 시장 정보를 로드하는 비동기 팩토리 메소드"""
        exchange = ccxt.okx({
            'apiKey': api_key,
            'secret': api_secret,
            'password': passphrase,
            'options': {
                'defaultType': 'swap',
                'createMarketBuyOrderRequiresPrice': False
            },
        })
        exchange.set_sandbox_mode(True)

        try:
            await exchange.load_markets(True)
            if ticker not in exchange.markets:
                raise ccxt.ExchangeError(f"Market {ticker} not found in loaded markets.")
            return cls(exchange)
        except Exception:
            print("--- Fatal Error during Trader Initialization ---")
            traceback.print_exc()
            await exchange.close()
            return None

    # ---------- 공통 유틸 ----------
    def _cid(self, prefix: str) -> str:
        p = ''.join(ch for ch in str(prefix) if ch.isalnum()) or "ID"
        raw = f"{p}{int(time.time()*1000)}{uuid.uuid4().hex[:6]}"
        return raw[:32]

    def _sanitize_clid(self, clid: str, fallback_prefix: str) -> str:
        s = ''.join(ch for ch in str(clid) if ch.isalnum())
        if not s:
            return self._cid(fallback_prefix)
        return s[:32]

    def _to_px(self, ticker: str, price: float) -> float:
        try:
            return float(self.exchange.price_to_precision(ticker, price))
        except Exception:
            return float(price)

    def _to_amt(self, ticker: str, amount: float) -> float:
        try:
            return float(self.exchange.amount_to_precision(ticker, amount))
        except Exception:
            return float(amount)

    def _min_amount(self, ticker: str) -> float:
        try:
            m = self.exchange.markets.get(ticker) or {}
            return float((m.get("limits", {}).get("amount", {}) or {}).get("min") or 0)
        except Exception:
            return 0.0

    async def _retry(self, coro_fn, *args, retries=1, backoff=0.4, **kwargs):
        """가벼운 리트라이(필요 시 사용)."""
        for i in range(retries + 1):
            try:
                return await coro_fn(*args, **kwargs)
            except Exception as e:
                if i >= retries:
                    raise
                await asyncio.sleep(backoff * (2 ** i))

    # ---------- 조회/유틸 ----------

    def get_contract_size(self, ticker):
        """1계약 당 코인 수"""
        market = self.exchange.markets.get(ticker)
        if market and 'contractSize' in market:
            return float(market['contractSize'])
        return None

    async def get_current_price(self, ticker):
        """현재가(float)"""
        try:
            t = await self.exchange.fetch_ticker(ticker)
            return float(t.get('last') or t.get('close'))
        except Exception as e:
            print(f"Error fetching current price for {ticker}: {e}")
            return None

    async def get_position(self, ticker):
        """현재 포지션(첫 번째 심볼 기준)"""
        try:
            positions = await self.exchange.fetch_positions([ticker])
            if positions:
                return positions[0]
            return None
        except Exception as e:
            print(f"Error fetching position: {e}")
            return None

    async def fetch_open_orders(self, ticker):
        try:
            return await self.exchange.fetch_open_orders(ticker)
        except Exception as e:
            print(f"Error fetching open orders for {ticker}: {e}")
            return []

    async def cancel_order(self, order_id, ticker):
        try:
            await self.exchange.cancel_order(order_id, ticker)
            print(f"[CANCEL] id={order_id}")
            return True
        except Exception as e:
            print(f"Error cancelling order {order_id}: {e}")
            return False

    async def cancel_order_by_client_id(self, ticker, client_id):
        try:
            await self.exchange.cancel_order(None, ticker, {'clOrdId': client_id})
            print(f"[CANCEL by clOrdId] {client_id}")
            return True
        except Exception as e:
            print(f"[cancel by clOrdId ERROR] {client_id}: {e}")
            return False

    def get_tick_size(self, ticker: str) -> float:
        m = self.exchange.markets.get(ticker) or {}
        # OKX는 info.tickSize에 있는 경우가 많음
        return float(m.get("info", {}).get("tickSize")
                     or (m.get("precision", {}).get("price") and 10 ** (-m["precision"]["price"]))
                     or m.get("limits", {}).get("price", {}).get("min")
                     or 0.01)

    async def get_best_ask(self, ticker: str) -> float:
        ob = await self.exchange.fetch_order_book(ticker, 5)
        return float(ob["asks"][0][0]) if ob.get("asks") else await self.get_current_price(ticker)



    # ---------- 주문(place_*) 단일 인터페이스 ----------

    async def place_market_short(self, ticker: str, amount: float, *, clid_prefix="MKTSHORT", extra_params=None):
        amt = self._to_amt(ticker, float(amount))
        min_amt = self._min_amount(ticker)
        if min_amt and amt < min_amt:
            print(f"[SKIP MKTSHORT] amount {amt} < min {min_amt} for {ticker}")
            return None

        params = {
            "tdMode": self.default_td_mode,
            "posSide": "short",
            "reduceOnly": False,
        }
        if extra_params:
            params.update(extra_params)

        # ★ 마지막에 clOrdId 확정(덮어쓰기 방지 + sanitize)
        raw_clid = params.get("clOrdId") or self._cid(clid_prefix)
        params["clOrdId"] = self._sanitize_clid(raw_clid, clid_prefix)

        print(f"[MKTSHORT] {ticker} amt={amt} clOrdId={params['clOrdId']} params={params}")
        return await self.exchange.create_order(ticker, "market", "sell", amt, None, params)


    async def place_limit_short(self, ticker: str, amount: float, price: float, *, ioc=False, post_only=False, clid_prefix="LIMSHORT", extra_params=None):
        amt = self._to_amt(ticker, float(amount))
        min_amt = self._min_amount(ticker)
        if min_amt and amt < min_amt:
            print(f"[SKIP DCA] amount {amt} < min {min_amt} for {ticker}")
            return None
        px  = self._to_px(ticker, float(price))

        params = {
            "tdMode": self.default_td_mode,
            "posSide": "short",
            "reduceOnly": False,
        }
        if ioc:
            params["timeInForce"] = "ioc"
        if post_only:
            params["postOnly"] = True
        if extra_params:
            params.update(extra_params)

        # ★ 마지막에 clOrdId 확정(덮어쓰기 방지 + sanitize)
        raw_clid = params.get("clOrdId") or self._cid(clid_prefix)
        params["clOrdId"] = self._sanitize_clid(raw_clid, clid_prefix)

        print(f"[DCA-LIMIT] {ticker} px={px} amt={amt} clOrdId={params['clOrdId']} params={params}")
        return await self.exchange.create_order(ticker, "limit", "sell", amt, px, params)


    async def place_reduceonly_tp_for_short(self, ticker: str, amount: float, tp_price: float, *, clid_prefix="TP", extra_params=None):
        amt = self._to_amt(ticker, float(amount))
        min_amt = self._min_amount(ticker)
        if min_amt and amt < min_amt:
            print(f"[SKIP TP] amount {amt} < min {min_amt} for {ticker}")
            return None
        px  = self._to_px(ticker, float(tp_price))

        params = {
            "tdMode": self.default_td_mode,
            "posSide": "short",
            "reduceOnly": True,
        }
        if extra_params:
            params.update(extra_params)

        # ★ 마지막에 clOrdId 확정(덮어쓰기 방지 + sanitize)
        raw_clid = params.get("clOrdId") or self._cid(clid_prefix)
        params["clOrdId"] = self._sanitize_clid(raw_clid, clid_prefix)

        print(f"[TP-LIMIT] {ticker} px={px} amt={amt} clOrdId={params['clOrdId']} params={params}")
        return await self.exchange.create_order(ticker, "limit", "buy", amt, px, params)


    # ---------- 기타 ----------

    async def set_leverage(self, ticker, leverage):
        try:
            await self.exchange.set_leverage(
                leverage=leverage,
                symbol=ticker,
                params={"mgnMode": "isolated", "posSide": "short"}
            )
            print(f"[LEV] {ticker} -> {leverage}x")
        except Exception as e:
            print(f"Error setting leverage for {ticker}: {e}")

    async def close_entire_position(self, ticker):
        """포지션 전체 시장가 종료(reduceOnly)."""
        print(f"[CLOSE ALL] {ticker}")
        try:
            position = await self.get_position(ticker)
            if not position or float(position.get('contracts', 0)) == 0:
                print("No position to close.")
                return True
            contracts_to_close = float(position.get('contracts'))
            params = {'reduceOnly': True, 'posSide': 'short', 'tdMode': 'isolated'}
            await self.exchange.create_market_buy_order(ticker, contracts_to_close, params)
            print(f"[CLOSE OK] {contracts_to_close} contracts.")
            return True
        except Exception as e:
            print(f"Error closing entire position for {ticker}: {e}")
            return False

    async def close_connection(self):
        await self.exchange.close()
