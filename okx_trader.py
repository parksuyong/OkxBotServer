import ccxt.async_support as ccxt
import traceback

class OKXTrader:
    def __init__(self, exchange_instance):
        self.exchange = exchange_instance
        print("OKX Trader Initialized.")

    @classmethod
    async def create(cls, api_key, api_secret, passphrase,ticker):
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

    async def set_leverage(self, ticker, leverage):
        try:
            await self.exchange.set_leverage(
                leverage=leverage,
                symbol=ticker,
                params={
                    "mgnMode": "isolated",
                    "posSide": "short"
                }
            )
            print(f"[OKXTrader-{ticker}] Leverage set to {leverage}x successfully.")
        except Exception as e:
            print(f"Error setting leverage for {ticker}: {e}")

    async def close_entire_position(self, ticker):
        """현재 포지션 전체를 시장가로 즉시 종료합니다."""
        print(f"[OKXTrader-{ticker}] Attempting to close entire position...")
        try:
            # 1. 현재 포지션 정보를 가져옵니다.
            position = await self.get_position(ticker)
            if not position or float(position.get('contracts', 0)) == 0:
                print("No position to close.")
                return True

            contracts_to_close = float(position.get('contracts'))

            # 2. 포지션 청산을 위한 시장가 주문을 생성합니다. (숏 포지션이므로 매수 주문)
            # reduceOnly: True 옵션은 포지션을 초과하여 주문이 체결되는 것을 방지하는 안전장치입니다.
            params = {
                'reduceOnly': True,
                'posSide': 'short',
                'tdMode': 'isolated'
            }
            await self.exchange.create_market_buy_order(ticker, contracts_to_close, params)
            print(f"Market close order for {contracts_to_close} contracts placed successfully.")
            return True

        except Exception as e:
            print(f"Error closing entire position for {ticker}: {e}")
            return False

    async def get_balance(self, currency='USDT'):
        """지정한 통화의 잔고를 조회합니다."""
        try:
            balance = await self.exchange.fetch_balance()
            return balance.get(currency, {}).get('free', 0)
        except Exception as e:
            print(f"Error fetching balance: {e}")
            return None

    def get_contract_size(self, ticker):
        """지정한 티커의 계약 가치(1계약 당 코인 수)를 반환합니다."""
        market = self.exchange.markets.get(ticker)
        if market and 'contractSize' in market:
            print(f"get_contract_size: {float(market['contractSize'])}")
            return float(market['contractSize'])
        return None

    async def get_current_price(self, ticker):
        """지정한 티커의 현재 시장 가격을 가져옵니다."""
        try:
            ticker_info = await self.exchange.fetch_ticker(ticker)
            return ticker_info['last']
        except Exception as e:
            print(f"Error fetching current price for {ticker}: {e}")
            return None

    async def get_position(self, ticker):
        """지정한 티커의 현재 포지션 정보를 가져옵니다."""
        try:
            positions = await self.exchange.fetch_positions([ticker])
            if positions:
                return positions[0]
            return None
        except Exception as e:
            print(f"Error fetching position: {e}")
            return None

    async def fetch_open_orders(self, ticker):
        """지정한 티커의 모든 대기 주문을 가져옵니다."""
        try:
            orders = await self.exchange.fetch_open_orders(ticker)
            return orders
        except Exception as e:
            print(f"Error fetching open orders for {ticker}: {e}")
            return []

    async def create_market_short_order(self, ticker, contract_amount):
        """지정한 코인 수량으로 시장가 숏 주문을 생성합니다."""
        try:
            params = {
                'posSide': 'short',
                'tdMode': 'isolated',

            }

            order = await self.exchange.create_market_sell_order(ticker, contract_amount, params)
            print("Market short order created successfully.")
            return order
        except Exception as e:
            print(f"Error creating market short order: {e}")
            return None

    async def create_limit_buy_tp_order(self, ticker, amount, price):
        """'Reduce-Only' 옵션이 적용된 지정가 익절(TP) 주문을 생성합니다."""
        try:
            params = {
                'reduceOnly': True,
                'posSide': 'short',
                'tdMode': 'isolated',

            }
            order = await self.exchange.create_limit_buy_order(ticker, amount, price, params)
            print(f"Limit TP buy order for {ticker} at {price} created.")
            return order
        except Exception as e:
            print(f"Error creating limit TP buy order: {e}")
            return None

    async def create_limit_short_dca_order(self, ticker, amount, price):
        """익절(TP) 조건이 내장된 지정가 물타기(DCA) 숏 주문을 생성합니다."""
        try:
            params = {
                'posSide': 'short',
                'tdMode': 'isolated',

            }
            order = await self.exchange.create_limit_sell_order(ticker, amount, price, params)
            print(f"Limit DCA short order for {ticker} at {price} created.")
            return order
        except Exception as e:
            print(f"Error creating limit DCA short order: {e}")
        return None


    async def cancel_order(self, order_id, ticker):
        """지정한 ID의 주문을 취소합니다."""
        try:
            await self.exchange.cancel_order(order_id, ticker)
            print(f"Order {order_id} has been cancelled.")
            return True
        except Exception as e:
            print(f"Error cancelling order {order_id}: {e}")
            return False

    async def close_connection(self):
        """거래소와의 연결을 닫습니다."""
        await self.exchange.close()