# event_handler.py (수정 제안)
from okx_trader import OKXTrader
import asyncio

# --- 상수 정의 ---
TRADE_RATIO = 0.0015  # 익절 및 물타기 통합 비율 (0.2%)
TOTAL_PROFIT_RATIO_THRESHOLD = 0.1 # 전체 익절 목표 수익률 (10%)

class EventHandler:
    def __init__(self, trader: OKXTrader, ticker: str, leverage: int, amount: float):
        self.trader = trader
        self.ticker = ticker
        self.leverage = leverage
        self.amount = amount
        self.open_orders = []
        print(f"[EventHandler-{self.ticker}] Initialized with: leverage={self.leverage}, amount={self.amount}")


    async def _sync_open_orders(self):
        # (이전과 동일, 변경 없음)
        print("Syncing open orders with exchange...")
        try:
            fetched_orders = await self.trader.fetch_open_orders(self.ticker)
            self.open_orders = []
            for order in fetched_orders:
                order_type = 'tp' if order['side'] == 'buy' else 'dca'
                self.open_orders.append({
                    'id': order['id'],
                    'type': order_type,
                    'price': order['price'],
                    'amount': order['amount']
                })
            print(f"Sync complete. Found {len(self.open_orders)} open orders.")
        except Exception as e:
            print(f"Error during order sync: {e}")

    async def _execute_initial_entry(self):
        # (이전과 동일, num_contracts를 정수로 변환하는 부분만 수정)
        print("Executing initial entry logic...")
        contract_size = self.trader.get_contract_size(self.ticker)
        if not contract_size:
            print(f"Could not get contract size for {self.ticker}. Aborting entry.")
            return

        current_price = await self.trader.get_current_price(self.ticker)
        if not current_price:
            print("Failed to fetch price, cannot place order.")
            return

        target_coin_amount = self.amount / current_price
        num_contracts = target_coin_amount / contract_size

        print(f"Target: {self.amount} USDT -> {target_coin_amount:.4f} Coins -> {num_contracts} Contracts (size: {contract_size})")

        if num_contracts < self.trader.exchange.markets[self.ticker]['limits']['amount']['min']:
            print(f"Calculated contracts are less than minimum order size. Aborting.")
            return

        await self.trader.create_market_short_order(self.ticker, num_contracts)

    async def handle_initial_state(self):
        # (이전과 동일, 변경 없음)
        print("Handling initial state...")
        await self._sync_open_orders()
        position = await self.trader.get_position(self.ticker)

        if not position or float(position.get('contracts', 0)) == 0:
            print("No active position found.")
            if self.open_orders:
                print(f"Found {len(self.open_orders)} dangling open orders. Cancelling all...")
                await self._cancel_all_open_orders_manually()
                self.open_orders.clear()

                await asyncio.sleep(0.1)


            await self._execute_initial_entry()
        else:
            print(f"Position for {self.ticker} already exists. Bot will monitor existing orders and events.")

    async def on_open(self):
        # (이전과 동일, 변경 없음)
        print("WebSocket connection opened.")
        await self.handle_initial_state()

    # 추가: 모든 DCA 주문을 취소하는 헬퍼 함수
    async def _cancel_all_dca_orders(self):
        """내부 주문 목록에 있는 모든 DCA 주문을 취소합니다."""
        dca_orders_to_cancel = [o for o in self.open_orders if o['type'] == 'dca']
        if not dca_orders_to_cancel:
            return

        print(f"Cancelling {len(dca_orders_to_cancel)} existing DCA orders...")
        for order in dca_orders_to_cancel:
            await self.trader.cancel_order(order['id'], self.ticker)
            # 성공 여부와 관계없이 일단 목록에서 제거 (실패 시 다음 동기화에서 처리됨)
            self.open_orders = [o for o in self.open_orders if o['id'] != order['id']]


    async def _cancel_all_open_orders_manually(self):
        """내부 목록에 있는 모든 미체결 주문을 하나씩 수동으로 취소합니다."""
        # 목록의 복사본을 만들어 순회 (순회 중 원본 리스트가 변경되기 때문)
        orders_to_cancel = list(self.open_orders)
        if not orders_to_cancel:
            return

        print(f"Manually cancelling all {len(orders_to_cancel)} open orders...")
        for order in orders_to_cancel:
            await self.trader.cancel_order(order['id'], self.ticker)
        print("Finished manual cancellation.")


    # 변경: 새로운 거래 로직 적용
    async def on_order_update(self, order):
        """'orders' 채널에서 주문 업데이트 이벤트를 받았을 때 새로운 거래 로직을 처리합니다."""
        order_id = order.get('ordId')
        status = order.get('state')

        # 체결되지 않은 주문은 무시
        if status != 'filled':
            # 만약 주문이 취소되었다면 우리 목록에서도 제거
            if status in ['canceled', 'partially_filled_canceled']:
                self.open_orders = [o for o in self.open_orders if o['id'] != order_id]
            return

        print(f"--- Order Filled: ID={order_id}, Side={order.get('side')} ---")
        # 체결된 주문은 목록에서 즉시 제거
        self.open_orders = [o for o in self.open_orders if o['id'] != order_id]

        side = order.get('side')
        avg_price = float(order.get('avgPx', '0'))
        filled_amount_contracts = float(order.get('accFillSz', '0'))


        # --- 시나리오 1: 숏 주문 (최초 진입 또는 물타기) 체결 ---
        if side == 'sell':
            # 1. 기존의 모든 물타기 주문을 취소합니다.
            await self._cancel_all_dca_orders()

            # 2. 익절(TP) 주문 생성
            tp_price = avg_price * (1 - TRADE_RATIO)
            new_tp_order = await self.trader.create_limit_buy_tp_order(self.ticker, filled_amount_contracts, tp_price)
            if new_tp_order:
                self.open_orders.append({'id': new_tp_order['id'], 'type': 'tp', 'price': tp_price, 'amount': filled_amount_contracts})

            # 3. 다음 물타기(DCA) 주문 생성
            dca_price = avg_price * (1 + TRADE_RATIO)
            new_dca_order = await self.trader.create_limit_short_dca_order(self.ticker, filled_amount_contracts, dca_price)
            if new_dca_order:
                self.open_orders.append({'id': new_dca_order['id'], 'type': 'dca', 'price': dca_price, 'amount': filled_amount_contracts})

        # --- 시나리오 2: 롱 주문 (익절) 체결 ---
        elif side == 'buy':
            print("Take-profit order filled. Checking position status...")
            await asyncio.sleep(0.1) # 포지션 정보가 업데이트될 시간을 줍니다.
            position = await self.trader.get_position(self.ticker)

            # 2-1. 포지션이 완전히 청산되었을 경우 -> 사이클 재시작
            if not position or float(position.get('contracts', 0)) == 0:
                print("Position fully closed. Restarting cycle.")
                await self._cancel_all_open_orders_manually()
                self.open_orders.clear()
                await self._execute_initial_entry()
            # 2-2. 포지션이 아직 남아있을 경우 (부분 익절) -> 다음 물타기 주문 생성
            else:

                # ▼▼▼ 여기가 새로운 '전체 익절' 로직의 시작입니다 ▼▼▼
                if position:
                    try:
                        # ccxt의 표준 구조에 따라 미실현 손익과 초기 증거금을 가져옵니다.
                        unrealized_pnl = float(position.get('unrealizedPnl', 0))
                        realized_pnl = float(position['info'].get('realizedPnl', 0)) # 실현 손익
                        initial_margin = float(position.get('initialMargin', 0))

                        # 2. 두 손익을 더하여 총 손익을 계산합니다.
                        total_pnl = unrealized_pnl + realized_pnl

                        if initial_margin > 0:
                            current_profit_ratio = total_pnl / initial_margin
                            print(f"DEBUG: Profit Ratio Check: RealizedPnl={realized_pnl}, UnrealizedPnl={unrealized_pnl}, TotalPnl={total_pnl}, Margin={initial_margin}, Ratio={current_profit_ratio:.2%}")

                            if current_profit_ratio >= TOTAL_PROFIT_RATIO_THRESHOLD:
                                print(f"Total profit target reached ({current_profit_ratio:.2%}). Closing entire position and restarting.")

                                await self.trader.close_entire_position(self.ticker)
                                await self._cancel_all_open_orders_manually()
                                self.open_orders.clear()
                                await self._execute_initial_entry()
                                return

                    except (ValueError, TypeError, KeyError) as e:
                        print(f"DEBUG: Could not calculate profit ratio due to an error: {e}")

                # 변경: 부분 익절 시에도 모든 DCA 주문을 취소하고 새로 생성합니다.
                print("Partial TP filled. Cancelling old DCA orders and placing a new one.")
                await self._cancel_all_dca_orders()

                # 방금 익절된 가격 기준으로 다음 물타기 주문을 생성합니다.
                dca_price = avg_price * (1 + TRADE_RATIO)
                new_dca_order = await self.trader.create_limit_short_dca_order(self.ticker, filled_amount_contracts, dca_price)
                if new_dca_order:
                    self.open_orders.append({'id': new_dca_order['id'], 'type': 'dca', 'price': dca_price, 'amount': filled_amount_contracts})

        print(f" {self.ticker} Cycle complete. Current open orders: {len(self.open_orders)}")

    # 변경: 로직 간소화
    async def on_position_update(self, position):
        """'positions' 채널에서 포지션 업데이트 이벤트를 받았을 때 (수동청산/강제청산 감지)"""
        pos_size = float(position.get('pos', 0))
        # 포지션이 0이 되었고, 우리 내부에 아직 미체결 주문이 남아있다면 수동/강제 청산으로 간주
        if pos_size == 0 and self.open_orders:
            print("Position manually/liquidated closed. Cancelling all related open orders.")
            await self._cancel_all_open_orders_manually()
            self.open_orders.clear() # 내부 목록도 비웁니다.

    async def on_error(self, error):
        print(f"An error occurred in WebSocket: {error}")

    async def on_close(self):
        print("WebSocket connection closed.")