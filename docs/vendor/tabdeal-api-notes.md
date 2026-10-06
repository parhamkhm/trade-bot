# Tabdeal API notes (reference for Phase 5b only)

> **Status.** Reference material for the future `TabdealBroker` / OMS (phase 5b). **No order code exists or may be
> written before phase 5b** (see CLAUDE.md §3.6 and the order-code guardrail). Nothing here is implemented.
>
> **Provenance.** Unless marked *measured*, every item below comes from Parham's review of the official Tabdeal API
> docs and fee table (2026-10-06). It has **not** been verified against the live API. Items marked *measured* were
> observed from the Turkey server (91.228.186.132) with read-only public calls, with the date given.

## 1. Security types

| Type | Requires | Examples |
|---|---|---|
| `NONE` | nothing (public) | `ping`, `time`, `exchangeInfo`, `depth`, `trades` |
| `USER` | `X-MBX-APIKEY` header only | user-level reads (listed in the docs; exact set to confirm) |
| `TRADE` | API key **and** signature | `order` (new / query / cancel), `openOrders`, `allOrders`, `orderList`, OCO |

Order-**read** endpoints (`GET order`, `openOrders`, `allOrders`, `orderList`) are `TRADE`-type. **Unknown:** whether a
read-only key may call them. The G0 private probe calls only the balance (`account`) read. If order reads need a
trading-capable key, reconciliation in phase 5b must be designed around that (open question for the key policy in
CLAUDE.md §3.6).

## 2. Signing

- Header `X-MBX-APIKEY: <key>`.
- HMAC-SHA256 over the URL-encoded query string **with `timestamp` (integer ms) appended**. The hex signature is
  appended as the `signature` parameter.
- This matches `src/tbot/execution/tabdeal_client.py`: integer ms timestamp, `recvWindow` from config, fresh
  parameters and a fresh signature on every attempt.

## 3. Orders (phase 5b design input)

- **Order types:** `LIMIT`, `MARKET`, `STOP_LOSS_LIMIT`. *Measured 2026-10-05:* `exchangeInfo` for BTCUSDT lists
  exactly `["LIMIT", "STOP_LOSS_LIMIT", "MARKET"]`, with `ocoAllowed: true`.
- **OCO:** `POST /api/v1/order/oco` with `listClientOrderId`, `limitClientOrderId` and `stopClientOrderId`.
- **Protective stops rest on the exchange.** Plan phase 5b so that the protective stop is an exchange-side order
  (stop-limit or OCO leg). It then survives bot crashes, server downtime and network loss, unlike a stop held in
  the bot's memory.
- **Stop-limit gap risk (must be documented to the user and modelled).** A `STOP_LOSS_LIMIT` triggers a *limit*
  order. In a fast move or a gap through the limit price it may fill partly or not at all, leaving the position
  open below the stop. Mitigations to evaluate in 5b:
  - a limit price set beyond the stop by a buffer sized from recorded order-book depth;
  - a watchdog that replaces an unfilled triggered stop with a market sell;
  - backtests that model the stop as "may not fill within the bar".

## 4. Idempotency (OMS)

- New orders take `newClientOrderId`; query and cancel take `origClientOrderId`.
- The OMS generates a unique `client_order_id` per intent (CLAUDE.md §3.6). On timeout it **queries by that id**
  and never blindly resends.

## 5. Cancelling everything (kill switch)

- `DELETE /api/v1/openOrders` cancels all open orders for a symbol, **including OCO legs**, but it is **not
  atomic**.
- The kill switch must loop: cancel all → re-query open orders → retry until none remain (bounded retries with
  backoff), then move to `HALTED` and alert. "Cancel returned 200" is not proof of a flat order book.

## 6. Fills and commission

- Fills include `commission` and `commissionAsset`. Always record the **actual** commission and its asset per fill;
  never assume the fee rate. The fee table (D-047, tier 1: taker 35 / maker 33 bps) is a backtest assumption only.
- Reconcile the recorded commission against the expected rate. A persistent deviation means a tier change or a
  wrong assumption, and is flagged.

## 7. Limits and pagination

| Endpoint | Limit |
|---|---|
| `allOrders` | `limit` max 1000, default 50 |
| `paginatedOpenOrders` | `page_size` max 500, default 100 |
| `nonExpiredAllOrders` | covers only the **last 24 h**. The docs list it as `DELETE`, which is probably a documentation error for a read. **Verify read-only (on a key with no trade permission, or with a harmless request) before relying on it** |
| `/trades` (public) | *measured 2026-10-04:* `limit=1000` accepted, ≈ 29 h of BTCUSDT trades; ids are global across markets (D-037) |
| `/depth` (public) | *measured 2026-10-05:* `limit=500` returns the whole book; `limit=1000` returns the same |
| `exchangeInfo` (public) | *measured 2026-10-05:* a bare JSON list of 1047 markets, not `{"symbols": [...]}` (D-045) |

## 8. Missing from the docs (open)

- Exact response shape of the balance / account endpoint (the probe's private part will record it — balances
  only).
- **Rate limits.** Not documented. The client uses a token bucket at 5 req/s plus backoff on 429 and 5xx, and the
  probe records any rate-limit headers it sees.
- **API-key permission levels.** Which permissions exist and which endpoints each allows. Until known, the G0
  probe treats key permissions as `unknown` unless the account response says otherwise (exit code 3), and
  Parham confirms in the Tabdeal UI that the key has **no trade and no withdrawal** permission.
- Whether USDT-quoted markets use the same fee table (D-047).
