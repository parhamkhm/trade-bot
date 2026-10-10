"""Is a long BTC perpetual cheaper than spot once funding is paid? (SPEC D-061 evidence)

Compares, on Binance history before the sealed holdout:

* the funding a long BTCUSDT USD-M perpetual pays (Binance 8 h funding, from data.binance.vision),
  both always-long and only while an SMA100 proxy (close > SMA100 on 1d, long/flat, weight 1, no stop
  and no vol sizing -- S0's signal only, not S0 itself) is in the market;
* the fee saving of trading the perpetual instead of spot at LBank VIP 0
  (spot 10/10 bps, perpetual taker 6 / maker 2 bps per side; Parham, 2026-10-10), at the
  turnover that proxy generated.

This is a cost comparison, not a strategy trial: no return, Sharpe or drawdown of SMA100 is
computed, so it does not enter the trial count in research/EXPERIMENTS.md. Funding after
``CANONICAL_HOLDOUT_START`` is never downloaded (the funding overlay will use it as a signal).

Run::

    uv run python -m research.funding_vs_spot_fees
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pandas as pd

from tbot.core.types import Timeframe
from tbot.data.binance_loader import CANONICAL_HOLDOUT_START, load_frame

BASE = "https://data.binance.vision/data/futures/um/monthly/fundingRate/BTCUSDT"
FIRST_MONTH = (2020, 1)  # first monthly file Binance publishes for BTCUSDT
RAW_DIR = Path("data/raw/binance_funding/BTCUSDT")
REPORT_DIR = Path("research/reports")
SMA_DAYS = 100
SPOT_BPS = {"taker": 10.0, "maker": 10.0}  # LBank spot VIP 0
PERP_BPS = {"taker": 6.0, "maker": 2.0}  # LBank perpetual VIP 0


def _months() -> list[tuple[int, int]]:
    year, month = FIRST_MONTH
    out = []
    while datetime(year, month, 1, tzinfo=UTC) < CANONICAL_HOLDOUT_START:
        out.append((year, month))
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return out


def _fetch_month(client: httpx.Client, year: int, month: int) -> pd.DataFrame:
    name = f"BTCUSDT-fundingRate-{year}-{month:02d}.zip"
    path = RAW_DIR / name
    if not path.exists():
        body = client.get(f"{BASE}/{name}").raise_for_status().content
        expected = client.get(f"{BASE}/{name}.CHECKSUM").raise_for_status().text.split()[0]
        if hashlib.sha256(body).hexdigest() != expected:
            raise ValueError(f"{name}: checksum mismatch")
        RAW_DIR.mkdir(parents=True, exist_ok=True)
        path.write_bytes(body)
    with zipfile.ZipFile(path) as zf:
        return pd.read_csv(io.BytesIO(zf.read(zf.namelist()[0])))


def load_funding() -> pd.DataFrame:
    with httpx.Client(timeout=60) as client:
        df = pd.concat([_fetch_month(client, y, m) for y, m in _months()], ignore_index=True)
    # calc_time carries 0-47 ms of positive jitter (e.g. ...200002): truncate to the second, so an event
    # can never be pushed past a bar close and pick up the position decided at that close
    ts = pd.to_datetime(df["calc_time"], unit="ms", utc=True).dt.floor("s")
    df["ts"] = ts.astype("datetime64[us, UTC]")
    df = df.drop_duplicates("ts").sort_values("ts").reset_index(drop=True)
    df = df.loc[df["ts"] < CANONICAL_HOLDOUT_START]
    return df[["ts", "funding_interval_hours", "last_funding_rate"]].rename(
        columns={"last_funding_rate": "rate"}
    )


def sma_positions() -> pd.DataFrame:
    """SMA100 long/flat on 1d closes; the position decided at close ``ts`` is held until the next close."""
    bars = load_frame("BTCUSDT", Timeframe.D1, caller="research.funding_vs_spot_fees", reason="turnover only")
    bars["sma"] = bars["close"].rolling(SMA_DAYS, min_periods=SMA_DAYS).mean()
    bars = bars.dropna(subset=["sma"])
    bars["ts"] = bars["ts"].astype("datetime64[us, UTC]")
    bars["pos"] = (bars["close"] > bars["sma"]).astype(int)
    return bars[["ts", "pos"]].reset_index(drop=True)


def _sides(pos: pd.Series) -> int:
    """Position changes (one side each), counting an initial entry from flat."""
    return int(pos.diff().abs().fillna(pos.iloc[0] if len(pos) else 0).sum())


def analyse(funding: pd.DataFrame, pos: pd.DataFrame) -> dict[str, object]:
    # funding at instant f is paid by the position held just before f (decided at the last close < f)
    merged = pd.merge_asof(funding, pos, on="ts", allow_exact_matches=False)
    start, end = funding["ts"].iloc[0], CANONICAL_HOLDOUT_START
    pos = pos.loc[(pos["ts"] >= start) & (pos["ts"] < end)]
    years = (end - start).total_seconds() / (365 * 86400)
    sides = _sides(pos["pos"])

    merged["year"] = merged["ts"].dt.year
    pos_year = pos.assign(year=pos["ts"].dt.year)
    by_year = []
    for year, grp in merged.groupby("year"):
        p = pos_year.loc[pos_year["year"] == year, "pos"]
        partial = grp["ts"].max() < pd.Timestamp(year=int(year), month=12, day=31, tz="UTC")
        by_year.append(
            {
                "year": f"{year} (partial)" if partial else str(year),
                "funding_always_long_pct": round(100 * grp["rate"].sum(), 2),
                "funding_while_s0_long_pct": round(100 * (grp["rate"] * grp["pos"]).sum(), 2),
                "s0_time_in_market_pct": round(100 * p.mean(), 1),
                "s0_sides": _sides(pos["pos"].loc[pos_year["year"] <= year])
                - _sides(pos["pos"].loc[pos_year["year"] < year]),
                "share_of_8h_periods_positive_pct": round(100 * (grp["rate"] > 0).mean(), 1),
            }
        )

    always = 100 * merged["rate"].sum() / years
    while_long = 100 * (merged["rate"] * merged["pos"]).sum() / years
    sides_per_year = sides / years
    scenarios = {}
    for leg in ("taker", "maker"):
        saving_bps = SPOT_BPS["taker"] - PERP_BPS[leg]  # spot is always taken at 10 bps
        saving_pct = sides_per_year * saving_bps / 100
        scenarios[f"perp_{leg}"] = {
            "fee_saving_per_side_bps": saving_bps,
            "fee_saving_pct_per_year": round(saving_pct, 2),
            "net_cost_of_perp_vs_spot_pct_per_year": round(while_long - saving_pct, 2),
            "breakeven_sides_per_year": round(while_long * 100 / saving_bps, 0),
        }
    return {
        "sample": {"start": start.isoformat(), "end_exclusive": end.isoformat(), "years": round(years, 3)},
        "n_funding_events": len(merged),
        "funding_always_long_pct_per_year": round(always, 2),
        "funding_while_s0_long_pct_per_year": round(while_long, 2),
        "share_of_8h_periods_positive_pct": round(100 * float((merged["rate"] > 0).mean()), 2),
        "s0": {
            "rule": f"SMA{SMA_DAYS} proxy: close > SMA{SMA_DAYS} (1d), long/flat, weight 1.0, no stop, "
            "no vol sizing",
            "sides": sides,
            "sides_per_year": round(sides_per_year, 1),
            "time_in_market_pct": round(100 * pos["pos"].mean(), 1),
        },
        "scenarios": scenarios,
        "by_year": by_year,
    }


def _markdown(res: dict[str, object]) -> str:
    s0, sample = res["s0"], res["sample"]
    assert isinstance(s0, dict) and isinstance(sample, dict)
    lines = [
        "# Funding vs spot fees: BTC long perpetual vs spot (Binance funding, pre-holdout)",
        "",
        f"Sample {sample['start'][:10]} to {sample['end_exclusive'][:10]} (exclusive), "
        f"{sample['years']} years; {res['n_funding_events']} funding events, longs paid in "
        f"{res['share_of_8h_periods_positive_pct']} % of them. {s0['rule']}.",
        "",
        f"- Funding paid by an always-long perpetual: **{res['funding_always_long_pct_per_year']} % / year**",
        "- Funding paid only while the SMA100 proxy is long: "
        f"**{res['funding_while_s0_long_pct_per_year']} % / year** "
        f"(time in market {s0['time_in_market_pct']} %, {s0['sides_per_year']} sides / year)",
        "",
        "| perpetual leg | saving / side (bps) | fee saving % / yr | net extra cost of perp % / yr"
        " | breakeven sides / yr |",
        "|---|---|---|---|---|",
    ]
    scenarios = res["scenarios"]
    assert isinstance(scenarios, dict)
    for leg, sc in scenarios.items():
        lines.append(
            f"| {leg} | {sc['fee_saving_per_side_bps']} | {sc['fee_saving_pct_per_year']} | "
            f"{sc['net_cost_of_perp_vs_spot_pct_per_year']} | {sc['breakeven_sides_per_year']:.0f} |"
        )
    lines += [
        "",
        "| year | funding always long % | funding while proxy long % | proxy in market % | proxy sides"
        " | 8 h periods > 0 % |",
        "|---|---|---|---|---|---|",
    ]
    by_year = res["by_year"]
    assert isinstance(by_year, list)
    for row in by_year:
        lines.append(
            f"| {row['year']} | {row['funding_always_long_pct']} | {row['funding_while_s0_long_pct']} | "
            f"{row['s0_time_in_market_pct']} | {row['s0_sides']} | "
            f"{row['share_of_8h_periods_positive_pct']} |"
        )
    return "\n".join(lines) + "\n"


def main() -> None:
    res = analyse(load_funding(), sma_positions())
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    stem = REPORT_DIR / f"funding_vs_spot_fees_{datetime.now(UTC):%Y%m%d}"
    stem.with_suffix(".json").write_text(json.dumps(res, indent=2), encoding="utf-8", newline="\n")
    stem.with_suffix(".md").write_text(_markdown(res), encoding="utf-8", newline="\n")
    print(_markdown(res))


if __name__ == "__main__":
    main()
