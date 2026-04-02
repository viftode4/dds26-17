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

## YAML Anchor Env Override Bug (Unresolved)

In compose files that use YAML anchors (`x-order-service: &order-service`), concrete service definitions that include their own `environment:` block **completely replace** the anchor's `environment:` — they do not merge. This means env vars added to anchors (e.g., `REDIS_MASTER_POOL_SIZE`) are silently dropped for any service that overrides `environment:`.

Affected files: `docker-compose-medium.yml`, `docker-compose-large.yml`, `docker-compose-6cpu.yml`.

Workaround: Pool size defaults in `config.py` are now set to the right values for medium/large configs (64/32). A proper fix would use `env_file:` at the anchor level or set the vars directly in each concrete service block.

---

## Changes Made (Summary)

| File | Change |
|---|---|
| `common/config.py` | Pool defaults: master=64, replica=32; env-var driven |
| `haproxy.cfg` / `haproxy-small.cfg` / `haproxy-medium.cfg` / `haproxy-large.cfg` / `haproxy-6cpu.cfg` | `timeout queue 30s`, `timeout check 10s`; per-server `maxconn` |
| All `docker-compose-*.yml` | DB masters: more CPU/RAM/io-threads/maxclients; 2nd read replica per cluster; `depends_on` updated |

---

## What Has NOT Been Tested Yet

- Medium stack with all fixes applied simultaneously (pool fix + HAProxy fix + scaled DBs + 2nd replicas)
- Throughput above 3k/s after fixes
- Whether `pool.disconnect()` churn in stock/payment is still a bottleneck after the other fixes

---

## Next Steps When Picking Up

1. `docker compose -f docker-compose-medium.yml up -d` to pick up all pending changes
2. Verify `docker exec <project>-order-db-1 redis-cli -a redis INFO clients` → `connected_clients` should be ~250 (was 4283)
3. Run stress test and check if p50/p95 latency improved
4. If stock/payment-db CPU is still high, the `pool.disconnect()` reconnect churn may need a different Sentinel re-resolution strategy
