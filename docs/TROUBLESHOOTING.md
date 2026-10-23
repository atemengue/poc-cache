# Troubleshooting and experimental validity

| Symptom | Likely cause | Check / mitigation |
|---|---|---|
| API not healthy | DB initialization still running, bad ports | `docker compose logs db app`; `docker compose ps`; allow seed time |
| `relation products does not exist` | Old/incomplete DB volume | Inspect DB logs; for disposable data `./poc down --volumes` and restart |
| `k6 summary missing` | Load container failed or mount path invalid | Read `results/campaign_*/...log`; use `--outdir` under `results` |
| Redis becomes unavailable | E10 deliberately stopped Redis | `docker compose up -d redis1 redis2`; script restores it in `finally` |
| Baseline returns errors | DB/host saturated | Lower `--rate`, increase machine capacity; don't report failed offered load as achieved |
| k6 dropped iterations | Too few VUs or overloaded generator | Increase `--vus/--max-vus`; run k6 on separate hardware if necessary |
| 429/502/504 | Host/gateway/backend capacity issues | `docker compose ps`; check CPU, `docker compose logs gateway app`; reduce offered rate |
| Disk or memory exhausted | DB seeding, Docker volume, Redis LRU | `docker system df`; configure SEED_ROWS and RAM; never benchmark on a swap-thrashing host |
| No Grafana app series | Prometheus target discovery not refreshed | Wait ~10s, Prometheus `/targets`, verify `app:8000/metrics` via Docker network |
| False low hit ratio | Redis freshly flushed, short TTL, broad keyspace | Check workload hot fraction, TTL and warmup; distinguish request skew from actual hits |
| Observed no improvement | DB aggregate cheaper on powerful host / k6 bottleneck / cold cache | Raise SEED_ROWS, compare warm vs DB, inspect actual CPU and read latencies |
| L1 consistency looks perfect | Traffic did not warm another replica's L1 | Repeat E17 or prime both replicas and verify Nginx distribution; short L1 TTL also reduces stale window |
| Docker CPU range error | Host has fewer CPUs than app limit | Set `APP_CPUS=0.5` or remove CPU limit for local demo; document changed host controls |
| Port already used | Another service binds 8080/3000/9090 | Set `GATEWAY_PORT`, `GRAFANA_PORT`, `PROMETHEUS_PORT` in `.env` |

The experiment runner sets cache parameters in the subprocess environment. These override `.env` for the recreated app. If you manually change `.env`, recreate only the app for changes to take effect. `SEED_ROWS` matters only when `dbdata` is initialized from scratch. `docker compose up --build` does not regenerate an existing DB volume.

## Limitations to disclose

1. The default test uses Docker on one host, not a multi-VM network. Sharing a Redis instance between app replicas is still distributed caching; cross-host network effects are not characterized.
2. Workload skew defaults to 90% of requests accessing a single product. This is favorable to caches by design; test more even distributions as counterexamples.
3. The product activity aggregation can be optimized with materialized views, background rollups or maintained counters. Those are valid alternatives not implemented here.
4. All Redis nodes are ephemeral and unreplicated. Two-way sharding is not Redis Cluster; it adds failure domains.
5. The DB/cache invalidation ordering is not atomic. Concurrency-induced stale repopulation remains possible.
6. The synthetic DB delay in E13 is an educational control, not observed PostgreSQL saturation.
7. CPU limits of each app are configurable and default to 1 CPU. DB, Nginx, Prometheus, Grafana, Redis, and k6 share host resources; large loads can be confounded by the host itself.
8. The script queries Prometheus after each run; a very short run may not have enough scrapes for a stable counter increase. Check raw k6 metrics and per-target health.
9. The scenario uses a sequential consistency probe; it does not prove correctness under concurrent writes, outages, key eviction or network partitions.
10. Results vary by PostgreSQL warm-buffer state, CPU load, disk, seed sizes and source code changes. Repeat matched tests and report dispersion.
