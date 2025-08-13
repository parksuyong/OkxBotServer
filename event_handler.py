# event_handler.py
import asyncio
import math
import time
from typing import Dict, Any, Optional

TRADE_STEP  = 0.0015   # 0.5% 물타기 간격
TP_STEP     = 0.0015  # 각 레그 익절 0.5%
MAX_DCA     = 12      # 현재가 위로 물타기 개수
BATCH_PAUSE = 0.15    # 벌크 주문 간 딜레이
TICK_INTERVAL = 1.5   # 초 단위, 가격/포지션/주문 정합성 주기


def ccxt_to_ws_symbol(ccxt_symbol: str) -> str:
    """
    'ETH/USDT:USDT' -> 'ETH-USDT-SWAP'
    """
    base, rest = ccxt_symbol.split('/')
    quote = rest.split(':')[0]
    return f"{base}-{quote}-SWAP"

def ws_to_ccxt_symbol(ws_symbol: str) -> str:
    """
    'ETH-USDT-SWAP' -> 'ETH/USDT:USDT'
    """
    base, quote, _ = ws_symbol.split('-')
    return f"{base}/{quote}:USDT"

class EventHandler:
    def __init__(self, trader, ticker_ccxt: str, leverage: int, amount_usdt: float):
        """
        trader: OKXTrader
        ticker_ccxt: ccxt 포맷 심볼 (예: 'ETH/USDT:USDT') ← 외부는 항상 이걸로 전달
        leverage: 레버리지
        amount_usdt: 레그당 USDT (계약수량은 현재가/contract_size로 환산)
        """
        # 심볼 표준화 (내부는 ccxt 포맷으로 통일)
        self.symbol = ticker_ccxt                    # ccxt 포맷
        self.symbol_ws = ccxt_to_ws_symbol(self.symbol)  # WS 포맷 (필요 시 바깥에서 사용)

        self.trader = trader
        self.leverage = leverage
        self.amount_usdt = float(amount_usdt)

        # 내부 상태(서비스에선 DB 권장)

        self.last_filled_leg_price: Optional[float] = None
        self.enter_on_start = True        # WS open 시 포지션 없으면 1레그 시장가 숏 진입
        self.keep_dca_across_ticks = True # 매 틱마다 전체 취소 X, 차분 리배치
        self.open_dca_orders: Dict[str, Dict[str, Any]] = {}  # id -> {price, amount}
        self.open_tp_orders : Dict[str, Dict[str, Any]] = {}  # id -> {price, amount}

        # 텔레메트리/스로틀
        self.metrics = {
            "oos_events": 0,        # out-of-order 등 이상 이벤트 수(필요시 증가)
            "tp_trim_count": 0,     # TP 합계 > 포지션 → 트림 횟수
            "reconcile_drift": 0,   # 로컬↔거래소 불일치 감지 횟수
            "catchup_count": 0,     # 급등 캐치업 횟수
        }
        self._tp_created_for_leg = set()

        self._last_catchup_ts = 0
        self._last_metrics_ts = 0
        self.CATCHUP_THROTTLE_SEC = 3
        self.METRICS_EVERY_SEC    = 30
        self._tick_task = None
        self.grid_anchor_price: Optional[float] = None


        self.reenter_on_flat = True        # 포지션 0이면 자동 재진입
        self.reenter_cooldown_sec = 5.0    # 재진입 쿨다운(초) - 과도한 재시도 방지
        self._last_reenter_ts = 0.0        # 마지막 재진입 시각

        # 캐치업 슬리피지 보호 옵션 (False=시장가, True=IOC 지정가)
        self.use_ioc_for_catchup = False

        print(f"[EH-{self.symbol}] Init (lev={self.leverage}, legUSDT={self.amount_usdt})  WS={self.symbol_ws}")

    # 루프 함수
    async def _tick_loop(self):
        print(f"[TICK LOOP START] {self.symbol} every {TICK_INTERVAL}s")
        try:
            while True:
                await self.on_price_tick()
                await asyncio.sleep(TICK_INTERVAL)
        except asyncio.CancelledError:
            print(f"[TICK LOOP STOP] {self.symbol}")
            raise


    # ---------- WS 훅(라우팅부가 호출할 수 있음) ----------
    async def on_open(self):
        """WS 연결 직후: (1) 초기 시장가 진입(옵션), (2) 틱 루프 시작"""
        print(f"[WS OPEN] {self.symbol} (WS={self.symbol_ws})")

        # 초기화(정적분석 경고 방지용)
        pos = None
        current_contracts = 0.0

        # 1) 초기 시장가 진입 (enter_on_start=True 이고 포지션 0이면 1레그 진입)
        try:
            if getattr(self, "enter_on_start", True):
                pos = await self.trader.get_position(self.symbol)
                current_contracts = float(pos.get("contracts", 0) or 0) if pos else 0.0
                if current_contracts <= 0:
                    await self._enter_initial_position()  # 내부에서 last_filled_leg_price/grid_anchor_price 세팅
        except Exception as e:
            print(f"[INIT ENTRY ERROR] {self.symbol}: {e}")

        # 2) 틱 루프 시작 (이미 돌고 있지 않으면)
        if getattr(self, "_tick_task", None) is None or self._tick_task.done():
            try:
                self._tick_task = asyncio.create_task(self._tick_loop())
            except Exception as e:
                print(f"[TICK LOOP START ERROR] {self.symbol}: {e}")





    async def on_close(self, code=None, reason=None):
        print(f"[WS CLOSE] {self.symbol} code={code} reason={reason}")
        if self._tick_task and not self._tick_task.done():
            self._tick_task.cancel()
            try:
                await self._tick_task
            except asyncio.CancelledError:
                pass
            self._tick_task = None

    async def on_error(self, err: Exception):
        """WS 에러 훅(비동기)."""
        print(f"[WS ERROR] {self.symbol}: {err}")

    # ---------- CID 규칙(결정적) ----------
    def _symkey(self) -> str:
        return ''.join(c for c in self.symbol if c.isalnum())[:12]

    def _cid_leg(self, px: float) -> str:
        return f"LEG{self._symkey()}{int(px*1e4)}"[:32]

    def _cid_tp_for_leg(self, leg_cid: str) -> str:
        base = leg_cid.replace("LEG", "", 1)
        return f"TP{base}"[:32]

    def _cid_catchup(self, now_price: float) -> str:
        return f"CATCHUP{self._symkey()}{int(now_price*1e2)}"[:32]

    def _cid_tp_rebuild(self) -> str:
        return f"TP{self._symkey()}REBUILD"[:32]




    # ---------- 유틸 ----------
    async def _contracts_for_usdt(self, ref_price: float) -> float:
        cs = self.trader.get_contract_size(self.symbol)
        if not cs or cs <= 0:
            raise RuntimeError(f"Cannot get contract size for {self.symbol}")
        coin_qty = self.amount_usdt / ref_price
        return float(coin_qty / cs)

    def _tp_for_short(self, entry_price: float) -> float:
        return entry_price * (1.0 - TP_STEP)

    def _count_missing_up_legs(self, last_leg_price: float, now_price: float) -> int:
        if last_leg_price <= 0 or now_price <= 0:
            return 0
        ratio = (now_price / last_leg_price) - 1.0
        return max(0, math.floor(ratio / TRADE_STEP))

    async def on_order_update(self, msg: Dict[str, Any]):
        """
        OKX 주문채널 WS 업데이트 엔트리.
        - 라우터가 원본 그대로 넘겨줘도 되고, 단일 주문 dict를 넘겨줘도 됨.
        - filled/partially_filled면 on_leg_filled로 위임
        - canceled 취소면 내부 오픈목록에서 제거
        """
        items = msg.get("data") if isinstance(msg, dict) and "data" in msg else None
        if items is None:
            items = [msg]  # 단일 dict도 처리

        for o in items:
            try:
                state = (o.get("state") or o.get("ordStatus") or "").lower()
                side  = (o.get("side") or "").lower()
                oid   = o.get("ordId") or o.get("id")
                cid   = o.get("clOrdId") or o.get("clientOrderId")

                # 취소 이벤트면 내부 오픈목록에서 제거
                if state in ("canceled", "cancelled"):
                    if oid in self.open_dca_orders:
                        self.open_dca_orders.pop(oid, None)
                    if oid in self.open_tp_orders:
                        self.open_tp_orders.pop(oid, None)
                    continue

                # 체결/부분체결 → on_leg_filled로 정규화 후 위임
                if "fill" in state:  # "filled", "partially_filled" 등
                    normalized = {
                        "side": side,
                        "avgPx": o.get("avgPx") or o.get("fillPx") or o.get("px"),
                        "accFillSz": o.get("accFillSz") or o.get("fillSz") or o.get("sz"),
                        "id": oid,
                        "clOrdId": cid,
                        "clientOrderId": cid,
                    }
                    await self.on_leg_filled(normalized)

            except Exception as e:
                print(f"[on_order_update ERROR] {self.symbol}: {e} item={o}")

    # EventHandler 클래스 내부 (재사용용 헬퍼)
    async def _enter_initial_position(self):
        """시장가 숏 1레그로 진입하고 앵커/베이스를 세팅"""
        try:
            px = await self.trader.get_current_price(self.symbol)
            if not px:
                return False
            c = await self._contracts_for_usdt(px)
            mkto = await self.trader.place_market_short(self.symbol, c)
            if mkto:
                self.last_filled_leg_price = px
                self.grid_anchor_price = px
                print(f"[INIT/RE-ENTRY] market short 1-leg at ~{px} (contracts≈{c})")
                return True
        except Exception as e:
            print(f"[INIT/RE-ENTRY ERROR] {self.symbol}: {e}")
        return False


    # ---------- 외부 이벤트 훅 ----------
    async def on_price_tick(self):
        """주기(1~5초): 현재가 확정 → 급등 캐치업 → 물타기 재생성 → 정합성."""
        now = await self.trader.get_current_price(self.symbol)
        if not now:
            return
        await self._reconcile_jump_and_regenerate(now)
        await self._reconcile_reduceonly_vs_position()
        await self._reconcile_open_orders()

        # 메트릭스 주기 출력
        now_ts = time.time()
        if now_ts - self._last_metrics_ts >= self.METRICS_EVERY_SEC:
            print(f"[METRICS {self.symbol}] catchup={self.metrics['catchup_count']} "
                  f"tpTrim={self.metrics['tp_trim_count']} drift={self.metrics['reconcile_drift']}")
            self._last_metrics_ts = now_ts

    async def on_leg_filled(self, order: Dict[str, Any]):
        side = (order.get("side") or "").lower()
        if side != "sell":
            return
        avg_px = float(order.get("avgPx", 0) or 0)
        filled_contracts = float(order.get("accFillSz", 0) or 0)
        if avg_px <= 0 or filled_contracts <= 0:
            return

        # ★★★ 여기서 앵커 갱신 (체결된 순간에만 고정 그리드의 기준을 이동)
        self.last_filled_leg_price = avg_px
        self.grid_anchor_price = avg_px
        # ★★★

        leg_cid = (order.get("clientOrderId") or order.get("clOrdId")) or self._cid_leg(avg_px)
        if leg_cid in self._tp_created_for_leg:
            return

        tp_px = self._tp_for_short(avg_px)
        tp_cid = self._cid_tp_for_leg(leg_cid)
        try:
            tp_order = await self.trader.place_reduceonly_tp_for_short(
                ticker=self.symbol,
                amount=filled_contracts,
                tp_price=tp_px,
                extra_params={"clOrdId": tp_cid}
            )
            if tp_order:
                self.open_tp_orders[tp_order["id"]] = {"price": tp_px, "amount": filled_contracts}
                self._tp_created_for_leg.add(leg_cid)
                print(f"[TP created] {self.symbol} tp={tp_px} amt={filled_contracts} tpCID={tp_cid}")
        except Exception as e:
            print(f"[TP create ERROR] {self.symbol} entry={avg_px} tp={tp_px} err={e}")


    async def _maker_safe_sell_price(self, target_price: float) -> float:
        best_ask = await self.trader.get_best_ask(self.symbol)
        tick = self._tick_size()
        safe = max(float(target_price), float(best_ask) + (tick or 0))
        return safe

    # event_handler.py - EventHandler 클래스 내부(아무 곳)
    def _tick_size(self) -> float:
        return self.trader.get_tick_size(self.symbol)

    def _key_by_tick(self, px: float) -> int:
        t = self._tick_size()
        if not t:
            return int(round(px*1e4))  # 안전장치
        return int(round(float(px) / t))

    def _targets_from_anchor(self, anchor: float) -> list[float]:
        """anchor 기준 위로 TRADE_STEP 간격으로 MAX_DCA 개의 목표가 생성"""
        targets = []
        p = float(anchor)
        for _ in range(MAX_DCA):
            p *= (1.0 + TRADE_STEP)
            targets.append(p)
        return targets


    async def on_position_update(self, position: Dict[str, Any]):
        try:
            pos_size = float(position.get('pos', 0) or 0)
        except Exception:
            pos_size = 0.0

        if pos_size == 0:
            # 우선 기존 주문 정리
            await self._cancel_all_open_orders()
            self.open_dca_orders.clear()
            self.open_tp_orders.clear()
            print(f"[Position=0] cleared all open orders for {self.symbol}")

            # ★ 자동 재진입
            if getattr(self, "reenter_on_flat", False):
                now = time.time()
                if now - getattr(self, "_last_reenter_ts", 0.0) >= getattr(self, "reenter_cooldown_sec", 5.0):
                    ok = await self._enter_initial_position()
                    if ok:
                        self._last_reenter_ts = now



    # ---------- 핵심: 급등 캐치업 + 물타기 재배치 ----------
    async def _reconcile_jump_and_regenerate(self, now_price: float):
        """
        (1) 급등 캐치업: last_filled_leg_price 기준 누락 레그를 시장가/IOC로 보정
        (2) 물타기 차분 리배치: '앵커(grid_anchor_price)' 기준 고정 그리드 유지
            - 부족한 자리만 추가, 목표 밖만 취소 (전체 리셋 없음)
        """
        # ---------- (1) 급등 캐치업 ----------
        if self.last_filled_leg_price is not None and now_price > self.last_filled_leg_price:
            missing = self._count_missing_up_legs(self.last_filled_leg_price, now_price)
            if missing > 0:
                now_ts = time.time()
                if now_ts - self._last_catchup_ts >= self.CATCHUP_THROTTLE_SEC:
                    MAX_CATCHUP_LEGS = 6
                    missing = min(missing, MAX_CATCHUP_LEGS)
                    try:
                        contracts_per_leg = await self._contracts_for_usdt(now_price)
                        qty = contracts_per_leg * missing
                        catchup_cid = self._cid_catchup(now_price)
                        print(f"[CATCH-UP] now={now_price:.8f}, last={self.last_filled_leg_price:.8f}, missing={missing}, qty={qty}")
                        if self.use_ioc_for_catchup:
                            await self.trader.place_limit_short(
                                ticker=self.symbol,
                                amount=qty,
                                price=now_price,
                                ioc=True,
                                post_only=False,
                                extra_params={"clOrdId": catchup_cid}
                            )
                        else:
                            await self.trader.place_market_short(
                                ticker=self.symbol,
                                amount=qty,
                                extra_params={"clOrdId": catchup_cid}
                            )
                        self._last_catchup_ts = now_ts
                        self.metrics["catchup_count"] += 1
                    except Exception as e:
                        print(f"[CATCH-UP ERROR] {self.symbol}: {e}")

        # ---------- (2) 물타기 차분 리배치(앵커 기반) ----------
        # 앵커가 없으면(초기) 임시로 now_price 사용 — 첫 체결 후 on_leg_filled에서 앵커가 갱신됨
        anchor = self.grid_anchor_price or now_price
        targets = self._targets_from_anchor(anchor)  # anchor 위로 TRADE_STEP 간격, MAX_DCA 개

        # 거래소 최신 오픈오더 조회 후, LEG 주문만 틱키로 매핑
        fetched = await self.trader.fetch_open_orders(self.symbol)
        def key(px: float) -> int: return self._key_by_tick(px)

        ex_dca_by_key = {}
        for o in fetched:
            cid = (o.get("clientOrderId") or o.get("clOrdId") or "")
            if cid.startswith("LEG"):
                try:
                    ex_dca_by_key[key(o.get("price"))] = o
                except Exception:
                    continue

        need_keys = { key(tp) for tp in targets }

        # 2-1) 부족한 것만 생성
        created = 0
        for tp in targets:
            k = key(tp)
            if k in ex_dca_by_key:
                continue  # 이미 존재 → 유지
            try:
                contracts_per_leg = await self._contracts_for_usdt(tp)
                leg_cid = self._cid_leg(tp)
                # 메이커가 보정: 최우선 매도호가 + 1틱 이상으로 올려서 postOnly 거절 회피
                safe_px = await self._maker_safe_sell_price(tp)
                order = await self.trader.place_limit_short(
                    ticker=self.symbol,
                    amount=contracts_per_leg,
                    price=safe_px,
                    ioc=False,
                    post_only=True,
                    extra_params={"clOrdId": leg_cid}
                )
                if order:
                    created += 1
                    await asyncio.sleep(BATCH_PAUSE)
            except Exception as e:
                print(f"[DCA create ERROR] {self.symbol} px={tp}: {e}")

        # 2-2) 목표 밖(need_keys에 없는) LEG만 취소
        for k_existing, o in list(ex_dca_by_key.items()):
            if k_existing not in need_keys:
                try:
                    await self.trader.cancel_order(o["id"], self.symbol)
                    await asyncio.sleep(BATCH_PAUSE)
                except Exception as e:
                    print(f"[DCA cancel EXTRA ERROR] {self.symbol} oid={o.get('id')}: {e}")

        print(f"[DCA reconciled] {self.symbol} created={created} keep={len(need_keys)} anchor={anchor}")


    # ---------- 리컨실(정합성) ----------
    async def _reconcile_reduceonly_vs_position(self):
        """TP 합계 > 포지션 방지. 초과 시 TP 재배치(한 방)."""
        pos = await self.trader.get_position(self.symbol)
        if not pos:
            return
        current_contracts = float(pos.get("contracts", 0) or 0)
        if current_contracts <= 0:
            if self.open_tp_orders:
                await self._cancel_open_tp_orders()
                self.open_tp_orders.clear()
            return

        tp_total = sum(float(v["amount"]) for v in self.open_tp_orders.values())
        if tp_total > current_contracts:
            print(f"[TP TRIM] total {tp_total} > pos {current_contracts}. Rebuild.")
            self.metrics["tp_trim_count"] += 1
            await self._cancel_open_tp_orders()
            self.open_tp_orders.clear()

            base_px = self.last_filled_leg_price or await self.trader.get_current_price(self.symbol)
            tp_px = self._tp_for_short(base_px)
            tp_cid = self._cid_tp_rebuild()
            try:
                order = await self.trader.place_reduceonly_tp_for_short(
                    ticker=self.symbol,
                    amount=current_contracts,
                    tp_price=tp_px,
                    extra_params={"clOrdId": tp_cid}
                )
                if order:
                    self.open_tp_orders[order["id"]] = {"price": tp_px, "amount": current_contracts}
            except Exception as e:
                print(f"[TP rebuild ERROR] {self.symbol}: {e}")

    async def _reconcile_open_orders(self):
        """REST 미체결 ←→ 내부 목록 동기화 (clOrdId 우선 분류)."""
        fetched = await self.trader.fetch_open_orders(self.symbol)
        ex_ids = {o["id"] for o in fetched}

        # 로컬 → 거래소에 없으면 제거
        for oid in list(self.open_dca_orders.keys()):
            if oid not in ex_ids:
                self.open_dca_orders.pop(oid, None)
        for oid in list(self.open_tp_orders.keys()):
            if oid not in ex_ids:
                self.open_tp_orders.pop(oid, None)

        # 거래소 → 로컬에 없으면 흡수
        local_ids = set(self.open_dca_orders.keys()) | set(self.open_tp_orders.keys())
        before = len(local_ids)
        for o in fetched:
            if o["id"] in local_ids:
                continue
            cid = o.get("clientOrderId") or o.get("clOrdId") or ""
            if cid.startswith("TP-"):
                self.open_tp_orders[o["id"]] = {"price": o.get("price"), "amount": o.get("amount")}
            elif cid.startswith("LEG-"):
                self.open_dca_orders[o["id"]] = {"price": o.get("price"), "amount": o.get("amount")}
            else:
                # fallback: side로 추정
                if (o.get("side") or "").lower() == "buy":
                    self.open_tp_orders[o["id"]] = {"price": o.get("price"), "amount": o.get("amount")}
                else:
                    self.open_dca_orders[o["id"]] = {"price": o.get("price"), "amount": o.get("amount")}
        after = len(set(self.open_dca_orders.keys()) | set(self.open_tp_orders.keys()))
        if after != before:
            self.metrics["reconcile_drift"] += 1

    # ---------- 취소 유틸 ----------
    async def _cancel_open_dca_orders(self):
        if not self.open_dca_orders:
            return
        ids = list(self.open_dca_orders.keys())
        for oid in ids:
            try:
                await self.trader.cancel_order(oid, self.symbol)
            except Exception as e:
                print(f"[cancel DCA ERROR] {self.symbol} oid={oid}: {e}")
            await asyncio.sleep(BATCH_PAUSE)
        self.open_dca_orders.clear()

    async def _cancel_open_tp_orders(self):
        if not self.open_tp_orders:
            return
        ids = list(self.open_tp_orders.keys())
        for oid in ids:
            try:
                await self.trader.cancel_order(oid, self.symbol)
            except Exception as e:
                print(f"[cancel TP ERROR] {self.symbol} oid={oid}: {e}")
            await asyncio.sleep(BATCH_PAUSE)
        self.open_tp_orders.clear()

    async def _cancel_all_open_orders(self):
        await self._cancel_open_tp_orders()
        await self._cancel_open_dca_orders()
