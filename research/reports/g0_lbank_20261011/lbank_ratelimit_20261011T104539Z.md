# LBank rate-limit discovery (public endpoint, stepped, stop at first throttle)

> measured from srv6950132412 (docker, image tbot-lbank-recorder @ 8a7d528) at 2026-10-11T10:45:39.117650+00:00

- hostname: `srv6950132412 (docker, image tbot-lbank-recorder @ 8a7d528)`
- UTC start / end: `2026-10-11T10:45:39.117650+00:00` / `2026-10-11T10:46:10.106722+00:00`
- python: `3.12.15` (Linux-7.0.0-14-generic-x86_64-with-glibc2.36)
- script git commit: `8a7d528ec3d8dbb5bebb5a2ea8ba02f7adb761d8`
- mode: `ratelimit`, data source: **live**
- argv: `--ratelimit --out-dir /out --host-label srv6950132412 (docker, image tbot-lbank-recorder @ 8a7d528)`
- local clock sync: NOT checked by this script - the operator records chrony/timedatectl status separately

Endpoint: `depth` -> `https://api.lbkex.com/v2/depth.do` {'symbol': 'btc_usdt', 'size': 20}

## Result: last safe rate = 0.891820596061584 req/s; stopped early = True; interrupted = False

| rate rps | sent | achieved rps | reached | statuses | p50 ms | p95 ms | clean |
|---|---|---|---|---|---|---|---|
| 1.000 | 27 | 0.892 | False | {'200': 27} | 289.741 | 1751.041 | True |

## Rate not reached (latency-limited, not a ban)

- target 1.0 rps, achieved 0.892 rps

- latency-limited: achieved rate fell short of 90% of the requested rate. The client is strictly serial by design and never adds concurrency to try to reach it, so this says nothing about what the exchange itself would tolerate -- only that THIS client, at THIS measured per-request latency, could not exercise that rate to test it.

## Caveat

A run that completes proves only that these rates were tolerated for the step length; it does not prove there is no limit. Limits may be per endpoint or per window. This ceiling covers requests made by THIS PROBE PROCESS only: the LBank recorder (src/tbot/data/lbank_recorder.py) runs continuously on the same host/IP and adds its own steady-state traffic (approximately 0.15 req/s from its own polling schedule) on top of whatever this probe measures, which this probe neither controls nor accounts for -- the exchange sees a higher combined rate from this IP than the figures above show in isolation.
