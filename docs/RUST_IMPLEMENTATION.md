# Rust implementation notes

The PoC uses Rust for **all HTTP request handling and cache algorithms**. Python is only the orchestration CLI (`./poc` and `scripts/benchmark_campaign.py`). k6 remains JavaScript; SQL seeds remain PostgreSQL scripts.

## Source tree

```text
orderflow-cache-poc/
├── poc                         # Python CLI, executable shebang
├── scripts/benchmark_campaign.py  # E01–E22 orchestration, report generation
├── app/
│   ├── Cargo.toml
│   └── src/
│       ├── main.rs             # Axum API / error handling / middleware
│       ├── config.rs           # Typed env validation
│       ├── storage.rs          # SQLx DB queries and transactions
│       ├── cache.rs            # Redis cache mechanisms and Rust tests
│       └── metrics.rs          # Prometheus metric families
├── db/schema.sql
├── db/init.sh                  # Required by postgres image initialization
├── Dockerfile                  # Rust builder + Rust test target + runtime
├── docker-compose.yml
├── benchmarks/load.js          # k6 fixed-arrival workload
├── infra/                      # Nginx, Prometheus, Grafana
├── tests/                      # CLI/campaign contract tests (Python stdlib)
└── results/
```

The application is one executable binary, not a collection of microservices. Multiple replicas run the same executable and share one Redis or two explicit Redis shards.

## Compile, test, inspect

With Docker (local Rust compiler not required):

```bash
./poc build
./poc test-rust
./poc up --replicas 2
./poc check
```

Without Docker, with a local Rust toolchain and running PostgreSQL/Redis:

```bash
cargo test --locked --manifest-path app/Cargo.toml
DATABASE_URL=postgres://demo:demo@localhost:5432/orderflow \
REDIS_1_URL=redis://localhost:6379/0 REDIS_2_URL=redis://localhost:6380/0 \
cargo run --release --locked --manifest-path app/Cargo.toml
```

Rust tests validate deterministic cache keys, sharding, L1 TTL, and L1 eviction behavior. The Python tests validate the CLI, report generator, and API/metric source contract; they do **not** substitute for runtime or integration tests.

The multi-stage Dockerfile first caches Rust dependencies with a placeholder entry point, then compiles the real release binary and copies it into a Debian slim runtime with `curl` for direct replica consistency probes. The Docker `test` target runs `cargo test --release`, and The CLI builds the `test` target under a separate Docker image tag so it does not overwrite the runnable Compose image. Running the campaign changes environment variables and recreates app containers, *not the compiled binary*.

## Runtime instrumentation

Axum route middleware records request count/duration, SQLx reports read/query timing, cache code records L1/L2 hit/miss/error, Redis operation status, invalidation outcomes and protected-mode coordination. Metric names match the Grafana provisioning and Python campaign queries from the previous version. Avoid expanding metric label cardinality with individual product IDs.

## Trade-off and limitations

Cache connection managers initialize lazily, within the Redis operation timeout, and reconnect after runtime Redis interruptions; `health` deliberately tests only PostgreSQL. Neither Redis instance is required for application startup. Cache failures are best-effort fallbacks to PostgreSQL. In L1/L2, one replica's local memory does not receive a write invalidation from another replica. Two-way application sharding does not provide Redis Cluster semantics or high availability. Successful HTTP 200s during Redis failure are not proof of acceptable throughput.

Retain `app/Cargo.lock` for repeatable dependency resolution; Docker and CLI test builds enforce it with `--locked`. Before reporting performance results, run `./poc test-rust`, `./poc up`, `./poc check` and the smoke campaign on the target Docker host.

## Live integration tests

The opt-in `tests/test_runtime.py` suite checks concurrent stock reservation, protected-mode miss coalescing, startup without Redis, Redis reconnection, and short-TTL jitter. It changes product 10000 and stops Redis, so it requires a separate Compose project ending in `-validation`. Do not run it concurrently with campaigns.

```bash
export COMPOSE_PROJECT_NAME=orderflow-cache-validation
export GATEWAY_PORT=18080 PROMETHEUS_PORT=19090 GRAFANA_PORT=13000
./poc up --replicas 2
ORDERFLOW_RUNTIME_TESTS=1 python3 -m unittest discover -s tests -p test_runtime.py -v
./poc campaign --profile smoke --skip-start --rate 40 --duration 15
./poc down
```
