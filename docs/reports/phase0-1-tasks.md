# گزارش گام ۱ — اسکلت پروژه، قراردادها و بریف وظایف فاز ۰ و ۱

تاریخ: ۲۰۲۶-۱۰-۰۱ · نویسنده: ارکستریتور (Opus) · وضعیت: **منتظر تأیید پرهام پیش از واگذاری به ساب‌ایجنت‌ها**

---

## ۱. چه چیزی تا اینجا ساخته شد (خودم، بدون ساب‌ایجنت)

| فایل | توضیح |
|---|---|
| `pyproject.toml` | پایتون ۳.۱۲ با uv، ruff (شامل قواعد `DTZ` و `ANN`)، mypy با `strict` روی `core/`, `risk/`, `execution/`، pytest |
| `src/tbot/core/types.py` | تمام قراردادهای هسته، به صورت dataclass فریزشده و خودِاعتبارسنج |
| `src/tbot/core/config.py` | اسکیمای پیکربندی (pydantic v2) + کلاس `Secrets` برای `.env` + دروازهٔ ایمنی معاملهٔ واقعی |
| `config/default.yaml` | پیکربندی پیش‌فرض فاز ۰ (کارمزد، لغزش، مسیر داده، تاریخ هولداوت) |
| `tests/core/` | ۴۸ تست برای قراردادها و پیکربندی |
| `docs/SPEC.md` | سند قرارداد: تایپ‌ها، اسکیمای داده، معیارهای از پیش ثبت‌شدهٔ گیت‌ها، لاگ تصمیم‌ها |
| `.github/workflows/ci.yml` | CI: بررسی نبود `.env`/فایل کلید در مخزن، ruff، mypy، pytest |
| `.gitignore`، `.env.example` | هیچ راز و هیچ دادهٔ بازاری وارد مخزن نمی‌شود |
| `research/EXPERIMENTS.md`، `research/HOLDOUT_LOG.md` | دفتر ثبت آزمایش‌ها و دفتر باز کردن هولداوت |

وضعیت فعلی بررسی‌ها: `uv run pytest` → ۴۸ تست سبز · `uv run ruff check .` → تمیز · `uv run mypy` → بدون خطا.

### تغییرهایی که نسبت به پیش‌نویس §۵ در `CLAUDE.md` اضافه کردم (همه افزایشی، نه تغییر تصمیم)

۱. پروتکل `Clock`: هیچ‌جای `core/`, `risk/`, `execution/` اجازهٔ `datetime.now()` ندارد؛ زمان تزریق می‌شود.
   دلیل: تطابق بک‌تست و لایو و قابل‌تست‌بودن.
۲. تایپ `SymbolFilters` با متدهای رُندکردن (tick / step / min-notional) تا بک‌تست دقیقاً همان قواعدی را اعمال کند که صرافی اعمال می‌کند.
۳. `RefusalReason` به صورت enum ماشین‌خوان، نه متن آزاد — برای شمارش و هشدار روی ردها.
۴. `OrderState`, `OrderStatus`, `OrderAck`, `Fill` کامل مشخص شدند.
۵. `Approved.order = None` یعنی «به هدف رسیده‌ایم، سفارشی لازم نیست» (به‌جای افزودن نوع سوم).
۶. `TargetIntent.scaled(m)` تنها راه مجاز اعمال ضریب رژیم.
۷. `BarWindow.as_of` به‌عنوان لنگر علیت که تست‌ها می‌توانند روی آن assert بزنند.

---

## ۲. هفت نکته که باید پیش از شروع ساخت بدانی (بخشی نیاز به تصمیم تو دارد)

**۱) هولداوت را از «۱۲ ماه آخر» به یک تاریخ ثابت تبدیل کردم: `2025-10-01T00:00:00Z`.**
«۱۲ ماه آخر» یک پنجرهٔ غلتان است؛ هر ماه که بگذرد یک ماه از هولداوت بی‌سر‌و‌صدا وارد دادهٔ تحقیق می‌شود و خاصیت
«یک‌بارمصرف بودن» از بین می‌رود. تاریخ ثابت این مشکل را حذف می‌کند. قفل دوگانه هم گذاشتم:
هم آرگومان `allow_holdout=True` و هم متغیر محیطی `TBOT_UNSEAL_HOLDOUT=G4` لازم است، و هر بار در
`research/HOLDOUT_LOG.md` ثبت می‌شود. **نیاز به تأیید تو دارد.**

**۲) گیت G1 در یک روز بسته نمی‌شود.** معیار «اختلاف قیمت Binance و Tabdeal اندازه‌گیری شود» به دادهٔ ضبط‌شدهٔ
واقعی نیاز دارد. پیشنهاد می‌کنم G1 را دو تکه کنیم:
G1a (دادهٔ Binance: کامل، چک‌سام‌شده، بدون شکاف توضیح‌داده‌نشده) که همین هفته بسته می‌شود، و
G1b (ضبط‌کنندهٔ Tabdeal: حداقل ۷ روز پیوسته با ≥۹۹٪ ساعت‌های کامل + گزارش basis) که یک هفته بعد بسته می‌شود.
فاز ۲ می‌تواند بعد از G1a شروع شود و منتظر G1b نماند.

**۳) دو وابستگی خارج از فهرست §۷ اضافه کردم: `pydantic-settings` و `PyYAML`.**
در pydantic v2 کلاس `BaseSettings` به بستهٔ جدا منتقل شده و فرمت پیکربندی ما YAML است. هر دو کوچک و استاندارد‌اند.
در `docs/SPEC.md` با شناسهٔ D-006 ثبت شد. **نیاز به تأیید تو دارد.**

**۴) پیش‌فرض کارمزد ۲۰ bps در هر سمت (مجموع ۰.۴٪ رفت‌و‌برگشت) عمداً بدبینانه است.**
این عدد مستقیماً تعیین می‌کند که استراتژی در فاز ۳ «لبه» دارد یا نه. تا وقتی کارمزد واقعی حساب تو را ندانیم همین
می‌ماند؛ اگر کارمزد واقعی کمتر بود، نتیجه فقط بهتر می‌شود. اگر از پنل Tabdeal کارمزد پلهٔ حساب خودت را بفرستی،
همان را می‌گذاریم (و در لاگ تصمیم‌ها ثبت می‌کنیم).

**۵) پروب باید از سرور ترکیه اجرا شود، نه از لپ‌تاپ.** اگر از ایران/جای دیگر اجرا شود ممکن است خطای شبکه یا مسدودی
بگیری و نتیجه گمراه‌کننده باشد. ضمناً بخش خصوصی پروب (موجودی حساب) فقط وقتی کار می‌کند که کلید API ساخته شده و
IP سرور در whitelist باشد — یعنی **اول IP ثابت سرور، بعد ساخت کلید**.

**۶) بخش خصوصی G0 بدون کلید بسته نمی‌شود.** اگر هنوز کلید نساخته‌ای، پروب فقط بخش عمومی را اجرا می‌کند و G0 را
«مشروط» می‌بندیم: بقیهٔ کار (فاز ۱ و ۲) به کلید نیاز ندارد، پس بلوکه نمی‌شویم.

**۷) کلاینت Tabdeal در این فاز فقط خواندنی ساخته می‌شود.** هیچ متد سفارشی (ارسال/لغو/OCO) حتی به‌صورت
کد مرده در مخزن نخواهد بود؛ `reviewer` همین را به‌عنوان BLOCKER چک می‌کند.

---

## ۳. بریف وظایف — فاز ۰ و ۱

ترتیب اجرا: **T1، T2، T4 موازی** → سپس **T3** (وابسته به خروجی T1) → سپس **T5 (بازبینی)** روی هر دیف.

### T1 — پروب فقط‌خواندنیِ Tabdeal

```
TASK: Build a strictly read-only Tabdeal probe that answers every open question in SPEC section 9.
PHASE: 0        AGENT: exchange-integrator
CONTEXT: CLAUDE.md sections 3.6, 5, 6 · docs/SPEC.md sections 2, 4, 9
ALLOWED FILES:
  scripts/tabdeal_probe.py
  src/tbot/execution/tabdeal_client.py        (READ-ONLY surface only)
  tests/scripts/test_tabdeal_probe.py
  tests/execution/test_tabdeal_client.py
CONTRACTS USED: SymbolFilters, MarketState, Timeframe from src/tbot/core/types.py (do not modify);
  ExchangeConfig and Secrets from src/tbot/core/config.py (do not modify)
ACCEPTANCE:
  - Public checks: ping; time (report clock skew vs local in ms); exchangeInfo for BTCUSDT and ETHUSDT
    (status, tick size, step size, min qty, min notional -> build a SymbolFilters for each);
    depth (best bid/ask, spread in bps and %, cumulative base+quote depth within 0.1% / 0.5% / 1% of mid);
    trades (count returned, min/max trade id, time span of the window in seconds, whether the response
    length equals the requested limit -> "saturated" flag, and a recommended polling interval with a 3x
    safety margin).
  - Repeated sampling: --samples N --interval S for depth, so the median spread over >= 30 samples can be
    reported (G0 criterion).
  - Private check runs ONLY if both TBOT_TABDEAL_API_KEY and TBOT_TABDEAL_API_SECRET are set: account
    balances only. Signing: X-MBX-APIKEY header, HMAC-SHA256 over the url-encoded query, integer-ms
    timestamp, fresh parameter dict per request, recvWindow from config. Reads use the /r/api/v1 prefix.
  - Client-side token bucket at exchange.requests_per_second and exponential backoff with jitter on
    429/5xx/timeouts; report every retry and every rate-limit header observed.
  - Output: a JSON report to --out (default research/reports/tabdeal_probe_<UTC>.json) plus a short human
    summary on stdout. Secrets, signatures and headers must never be printed or written to the report.
  - Tests: respx-mocked responses only; cover signature construction against a known vector, skew
    computation, spread/depth math, saturated detection, 429 backoff, and missing-credentials path.
  - uv run pytest / ruff check . / mypy all green.
OUT OF SCOPE: any order, cancel, OCO, userDataStream or withdrawal endpoint — not even unused helpers,
  constants or URLs; WebSocket; writing to config; touching core/types.py or docs/SPEC.md.
REPORT BACK: files changed, test results, the real values discovered for SPEC section 9 open questions
  (if the probe was run), rate-limit observations, and any Tabdeal behaviour that contradicts CLAUDE.md
  section 6.
```

### T2 — دانلود و انبار دادهٔ Binance + گزارش کیفیت

```
TASK: Download, verify and store Binance klines, and produce a data-quality report.
PHASE: 1        AGENT: data-engineer
CONTEXT: CLAUDE.md section 3.1 · docs/SPEC.md sections 5.1, 5.2, 5.3, 7 (G1)
ALLOWED FILES:
  scripts/download_binance.py
  src/tbot/data/binance_loader.py, src/tbot/data/store.py, src/tbot/data/quality.py
  tests/data/*
CONTRACTS USED: Bar, Timeframe from core/types.py; DataConfig from core/config.py (do not modify either)
ACCEPTANCE:
  - Downloads monthly kline ZIPs for BTCUSDT and ETHUSDT, timeframes 1h/4h/1d, from 2018-01 to the latest
    complete month, from data.binance.vision; verifies each file against its published .CHECKSUM (SHA-256)
    and fails loudly on mismatch. Re-running is idempotent and re-downloads nothing already verified.
  - Writes Parquet exactly as specified in SPEC 5.1 (Hive partitions, decimal128(38,12), ts = bar CLOSE
    time in UTC) plus the _dataset.json sidecar.
  - Loader API: load_bars(symbol, timeframe, start=None, end=None, allow_holdout=False) -> list[Bar] and
    load_frame(..., as_float=True) -> pandas.DataFrame. Default MUST exclude ts >= data.holdout_start.
    Unsealing requires allow_holdout=True AND env TBOT_UNSEAL_HOLDOUT=G4, and appends to
    research/HOLDOUT_LOG.md; otherwise raise a clear error. Add a test proving the default excludes it
    and a test proving the refusal.
  - Quality report (SPEC 5.3) written as JSON + Markdown to research/reports/: coverage per month, missing
    bars with timestamps, duplicates, out-of-order timestamps, zero-volume bars, high==low bars, |return|
    outliers; and a 1h -> 4h/1d resampling reconciliation (label='right', closed='right') within 1e-9.
  - Tests use small local fixtures (a handful of rows), never the network. Cover: checksum mismatch,
    gap detection, duplicate detection, DST-free UTC handling, holdout exclusion, idempotent re-run.
  - uv run pytest / ruff check . / mypy all green.
OUT OF SCOPE: strategies, indicators, the Tabdeal recorder, any network call in tests, modifying
  core/types.py, core/config.py or docs/SPEC.md.
REPORT BACK: files changed, actual data coverage (first/last bar per symbol/timeframe, row counts),
  the list of gaps found with your classification, and anything that would block gate G1a.
```

### T3 — ضبط‌کنندهٔ معاملات Tabdeal و ساخت کندل (بعد از T1)

```
TASK: Record Tabdeal public trades continuously and build 1h candles from them.
PHASE: 1        AGENT: data-engineer
CONTEXT: CLAUDE.md section 6 · docs/SPEC.md section 5.4 · the T1 probe report (polling interval, limits)
ALLOWED FILES:
  src/tbot/data/tabdeal_recorder.py, src/tbot/data/candles.py
  scripts/record_tabdeal.py
  tests/data/test_tabdeal_recorder.py, tests/data/test_candles.py
CONTRACTS USED: Bar, Timeframe from core/types.py; DataConfig, ExchangeConfig from core/config.py
ACCEPTANCE:
  - Polls public /trades at the interval recommended by the probe report, with the same token bucket and
    backoff policy as the probe; dedupes by trade id (INSERT OR IGNORE); persists to SQLite exactly as in
    SPEC 5.4 (trades / poll_log / gaps tables, price and qty as TEXT).
  - Detects saturation (response length == limit) and id discontinuities, writes a gaps row, and logs a
    warning. Never forward-fills: an hour with no trades produces NO bar plus a gap row.
  - Builds 1h candles with label='right', closed='right'; a candle is written only after its close time
    plus a configurable grace period; carries complete=False when a gap or saturation overlaps it.
  - Runs as a long-lived service: structured JSON logs, graceful SIGTERM shutdown, resumes from the last
    stored trade id after a restart without duplicating or skipping, and writes a heartbeat file.
  - Tests use recorded JSON fixtures: restart resumption, duplicate trades, out-of-order ids, an empty
    hour, a saturated response, and a candle whose boundary falls exactly on an hour edge.
  - uv run pytest / ruff check . / mypy all green.
OUT OF SCOPE: private endpoints, orders, WebSocket, strategy code, modifying core/ or docs/SPEC.md.
REPORT BACK: files changed, test results, measured write rate and database growth per day, and the
  recommended polling interval you actually implemented with the reason.
```

### T4 — راه‌اندازی سرور و استقرار

```
TASK: Make the Turkey VPS reproducible and run the recorder as a managed service.
PHASE: 0-1        AGENT: infra-ops
CONTEXT: CLAUDE.md sections 2, 3.6, 7 · docs/SPEC.md sections 4, 5.4
ALLOWED FILES:
  deploy/SERVER_SETUP.md, deploy/Dockerfile, deploy/docker-compose.yml,
  deploy/systemd/tbot-recorder.service, deploy/.dockerignore
  src/tbot/monitoring/logging.py, tests/monitoring/test_logging.py
CONTRACTS USED: none (do not import core/types.py)
ACCEPTANCE:
  - deploy/SERVER_SETUP.md: copy-paste steps for a beginner on Ubuntu 24.04 — non-root user with sudo,
    SSH hardening (key-only), ufw (allow 22 only, deny inbound rest), timezone UTC, chrony/systemd-timesyncd
    with an explicit verification command showing offset < 1s (signatures depend on the clock), git, uv,
    Docker + compose plugin, cloning the private repo, creating .env from .env.example (chmod 600),
    how to find the server's static IP for the Tabdeal whitelist, how to run the probe, how to start and
    inspect the recorder, and where logs and the SQLite file live. Every command must be tested as written.
  - Dockerfile: python:3.12-slim base, uv-based install, non-root runtime user, no secrets baked in,
    .env passed at runtime, healthcheck on the recorder heartbeat file.
  - docker-compose.yml: a `recorder` service with restart: unless-stopped, env_file .env, a named volume
    for data/, log rotation (max-size/max-file), and resource limits. `docker compose config` must validate.
  - systemd unit that manages the compose project and starts on boot.
  - src/tbot/monitoring/logging.py: structlog JSON logging with a processor that redacts any value coming
    from Secrets and any key matching api_key/secret/token/signature; tested.
OUT OF SCOPE: Telegram (phase 5), the trading bot service itself, firewall changes on Parham's real server
  (document them, do not run them), CI changes.
REPORT BACK: files changed, the exact commands you verified, anything in CLAUDE.md section 2 that the
  server setup contradicts, and the open items Parham must do by hand.
```

### T5 — بازبینی (قبل از merge هر دیف)

```
TASK: Review each of T1-T4 diffs before merge.
PHASE: 0-1        AGENT: reviewer (read-only)
ACCEPTANCE: findings ranked BLOCKER / MAJOR / MINOR with file:line and a concrete fix. Must explicitly
  confirm: (1) no order/cancel/withdrawal code path exists anywhere; (2) no secret, signature or header
  can reach logs, reports or tests; (3) the default data loader cannot return holdout bars; (4) no naive
  datetime and no non-UTC timestamp; (5) tests make no network calls.
```

---

## ۴. چیزی که از تو لازم دارم

۱. **تأیید سه مورد تصمیمی**: تاریخ ثابت هولداوت (`2025-10-01`)، شکستن G1 به G1a/G1b، و دو وابستگی جدید.
۲. **IP ثابت سرور ترکیه** (برای whitelist) — و بعد از آن ساخت کلید API با این مشخصات:
   فقط خواندن، **بدون مجوز برداشت**، محدود به همان IP.
۳. **کارمزد واقعی پلهٔ حساب تو در Tabdeal** اگر در پنل قابل دیدن است.
۴. **اوکی برای شروع واگذاری** (گام ۲). تا وقتی نگویی، هیچ ساب‌ایجنتی اجرا نمی‌شود.

پس از تأییدت، T1/T2/T4 را موازی اجرا می‌کنم، بعد T3، بعد بازبینی، و گزارش نهایی را در
`docs/reports/phase0-1-report.md` می‌نویسم.
