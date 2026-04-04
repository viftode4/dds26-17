# Known Issues & Optimization History

## Context

Stress testing target: **10k checkouts/second**. Baseline: ~3k/s with p50 latency 2-3s and p95 ~15s.
Primary test config: `docker-compose-medium.yml` (4 instances each of order/stock/payment).

---

## Issues Found & Fixed

### 1. HAProxy Cascade Failure

**Symptom:** Under load, one service instance would fall behind, HAProxy's `leastconn` would pile more connections onto it, the asyncio event loop would saturate, `/health` checks would time out, the server would be marked DOWN, and the remaining servers would cascade the same way.

**Root cause:** No per-server `maxconn` limit and no `timeout queue` / `timeout check` in HAProxy config.

**Fix applied to all `haproxy*.cfg`:**
- Added `timeout queue 30s` and `timeout check 10s` to `defaults`
- Added `maxconn 2000` per order server, `maxconn 1000` per stock/payment server

---

### 2. Excessive Idle Redis Connections

**Symptom:** order-db had ~4283 connected clients; stock-db and payment-db had ~15 each. order-db CPU was disproportionately high even at low request rates.

**Root cause:** `common/config.py` had hardcoded `max_connections=1024` (later bumped to 2048 in an earlier fix attempt). Since Python async releases connections between `await` points, actual concurrent DB connections are very low (~7-10 per instance at 3k/s). A pool of 1024+ per instance is wasteful — idle connections each send a health-check PING every 30s, generating hundreds of background PINGs/second to the DB.

The discrepancy between order-db (4283 clients) and stock/payment-db (~15) is because stock and payment services call `pool.disconnect()` before every transaction (intentional for Redis Sentinel re-resolution after failover), which destroys idle connections. Order service does not, so all 4 × 1024 pool slots stay open.

**Fix:** Changed defaults in `common/config.py` to `REDIS_MASTER_POOL_SIZE=64`, `REDIS_REPLICA_POOL_SIZE=32`. These are env-var driven so per-compose tuning is possible.

---

### 3. DB CPU Throttling

**Symptom:** Databases running at 200% of their allocated CPU limit.

**Root cause (stock/payment):** `pool.disconnect()` before every transaction = thousands of full TCP+auth reconnects per second at 3k/s req rate.

**Root cause (order):** Health-check PING storm from 4283 idle connections.

**Fix:** Vertical scaling of all DB masters (more CPU, RAM, `--io-threads 8`, `--maxclients`). Also added a 2nd read replica per DB cluster so `slave_for()` round-robins reads across two replicas, halving read load on each.

---

## YAML Anchor Env Override Bug (Workaround Applied)

In compose files that use YAML anchors (`x-order-service: &order-service`), concrete service definitions that include their own `environment:` block **completely replace** the anchor's `environment:` — they do not merge. This means env vars added to anchors (e.g., `REDIS_MASTER_POOL_SIZE`) are silently dropped for any service that overrides `environment:`.

Affected files: `docker-compose-medium.yml`, `docker-compose-large.yml`, `docker-compose-6cpu.yml`.

**Workaround:** Each concrete service block in medium/large/6cpu now explicitly sets pool size env vars (`REDIS_MASTER_POOL_SIZE`, `REDIS_REPLICA_POOL_SIZE`). The anchor's env vars are still silently dropped, but the per-service overrides cover it. A proper fix would use `env_file:` at the anchor level.

---

### 4. `min-replicas-to-write 1` Startup Race

**Symptom:** On cold boot (`docker compose up`), stock and payment services crash with `NOREPLICAS` errors during `FUNCTION LOAD`, then self-heal via `restart: on-failure` after ~5-10 seconds.

**Root cause:** `stock-db` and `payment-db` are configured with `--min-replicas-to-write 1` in all compose files. On startup, the replicas haven't synced yet, so the master rejects writes. `order-db` does **not** have this setting and starts cleanly.

**Impact:** Low — services self-heal after replicas sync. However, `docker compose up` exits with an error because the gateway's `depends_on` expects healthy services. Workaround: wait for restarts to complete, then `docker start` the gateway manually if needed.

---

### 5. WSL2 / Docker Desktop Performance Ceiling

**Symptom:** Redis SET benchmark shows ~4.6k ops/sec on WSL2 vs 50-100k+ on native Linux.

**Root cause:** Redis AOF writes go through the Hyper-V virtualization layer on Windows/macOS. Every `appendfsync everysec` flush is significantly slower than on bare metal.

**Recommendation:** Use `small` or `6cpu` configs for Docker Desktop / WSL2. The `medium` and `large` configs are designed for dedicated Linux machines and will be oversubscribed on virtualized environments.

---

## Changes Made (Summary)

| File | Change |
|---|---|
| `common/config.py` | Pool defaults: master=512, replica=256; env-var driven (compose files override via `REDIS_MASTER_POOL_SIZE` / `REDIS_REPLICA_POOL_SIZE` env vars) |
| `haproxy.cfg` / `haproxy-small.cfg` / `haproxy-medium.cfg` / `haproxy-large.cfg` / `haproxy-6cpu.cfg` | `timeout queue 30s`, `timeout check 10s`; per-server `maxconn` |
| All `docker-compose-*.yml` | DB masters: more CPU/RAM/io-threads/maxclients; 2nd read replica per cluster; `depends_on` updated |

---

## What Has NOT Been Tested Yet

- **Medium config at target concurrency** — tested locally on 16-CPU WSL2 (works but oversubscribed due to virtualization overhead)
- **Large config at target concurrency** — untested; requires ~90 CPU dedicated Linux machine
- **Throughput ceiling on native Linux** — all benchmarks so far are on Docker Desktop / WSL2
- **Sentinel failover under sustained load** on the optimize branch (unit tests pass; integration failover tests have known issues — see `test_analysis_report.md` Section 4.4)

---

## Next Steps When Picking Up

1. Run benchmarks on a native Linux machine with the default or medium config to establish true throughput ceiling
2. Re-run the full test suite (`112 tests`) on the optimize branch and update `test_analysis_report.md` with results
3. Fix the `min-replicas-to-write` startup race (Issue #4) — either remove the flag, add a startup delay, or use `WAIT` in the entrypoint
4. Test Sentinel failover under sustained load (the two failing tests in `test_sentinel_failover.py` need the fixes described in the test analysis report)
