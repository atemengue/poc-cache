use prometheus::{opts, Encoder, HistogramOpts, HistogramVec, IntCounterVec, IntGaugeVec, Registry, TextEncoder};

#[derive(Clone)]
pub struct Metrics {
    registry: Registry,
    pub http_requests: IntCounterVec,
    pub http_duration: HistogramVec,
    pub cache_lookup: IntCounterVec,
    pub cache_fallback: IntCounterVec,
    pub cache_wait: IntCounterVec,
    pub cache_invalidation: IntCounterVec,
    pub cache_redis: IntCounterVec,
    pub db_queries: IntCounterVec,
    pub db_duration: HistogramVec,
    pub app_info: IntGaugeVec,
    pub db_pool: IntGaugeVec,
    pub db_acquire: HistogramVec,
    pub db_errors: IntCounterVec,
    pub sql_statements: IntCounterVec,
    pub snapshot_loads: IntCounterVec,
    pub redis_duration: HistogramVec,
    pub follower_duration: HistogramVec,
    pub http_outcomes: IntCounterVec,
}
impl Metrics {
    #[cfg(test)]
    pub fn new() -> Result<Self, prometheus::Error> {
        Self::for_strategy("test")
    }
    pub fn for_strategy(strategy: &str) -> Result<Self, prometheus::Error> {
        let registry = Registry::new_custom(None, Some(std::collections::HashMap::from([
            ("strategy".into(), strategy.into()), ("service".into(), "app".into()),
        ])))?;
        let http_requests = IntCounterVec::new(opts!("http_requests_total", "Requests"), &["method", "route", "status"])?;
        let http_duration = HistogramVec::new(HistogramOpts::new("http_request_duration_seconds", "HTTP duration")
            .buckets(vec![0.001, 0.0025, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1., 2., 5., 10., 30.]), &["method", "route", "outcome"])?;
        let cache_lookup = IntCounterVec::new(opts!("cache_lookup_total", "Cache lookups"), &["layer", "result"])?;
        let cache_fallback = IntCounterVec::new(opts!("cache_fallback_total", "Cache fallback"), &["reason"])?;
        let cache_wait = IntCounterVec::new(opts!("cache_wait_total", "Single-flight coordination"), &["result"])?;
        let cache_invalidation = IntCounterVec::new(opts!("cache_invalidation_total", "Cache invalidations"), &["result"])?;
        let cache_redis = IntCounterVec::new(opts!("cache_redis_operations_total", "Redis operation outcomes"), &["operation", "status", "node"])?;
        let db_queries = IntCounterVec::new(opts!("db_queries_total", "DB operations"), &["operation"])?;
        let db_duration = HistogramVec::new(HistogramOpts::new("db_query_duration_seconds", "DB operation time")
            .buckets(vec![0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1., 2., 5., 10.]), &["operation"])?;
        let app_info = IntGaugeVec::new(opts!("orderflow_app_info", "Application configuration"), &["project"])?;
        let db_pool = IntGaugeVec::new(opts!("db_pool_connections", "Pool connections by state (used includes acquired connections)"), &["state"])?;
        let db_acquire = HistogramVec::new(HistogramOpts::new("db_pool_acquire_duration_seconds", "Connection acquisition wait")
            .buckets(vec![0.0001, 0.001, 0.005, 0.01, 0.05, 0.1, 0.25, 0.5, 1., 2., 5., 10.]), &["outcome"])?;
        let db_errors = IntCounterVec::new(opts!("db_errors_total", "Failed database operations; acquisition failures counted once per operation"), &["operation", "kind"])?;
        let sql_statements = IntCounterVec::new(opts!("db_sql_statements_total", "Explicit application SQL attempts, excluding transaction control and driver queries"), &["operation"])?;
        let snapshot_loads = IntCounterVec::new(opts!("db_snapshot_loads_total", "Snapshot loader invocations by origin"), &["origin"])?;
        let redis_duration = HistogramVec::new(HistogramOpts::new("cache_redis_duration_seconds", "Redis command latency including lazy connection and timeout")
            .buckets(vec![0.0001, 0.0005, 0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.15, 0.25, 0.5, 1.]), &["operation", "outcome", "node"])?;
        let follower_duration = HistogramVec::new(HistogramOpts::new("cache_follower_wait_seconds", "Follower polling time before cache hit or fallback")
            .buckets(vec![0.025, 0.05, 0.1, 0.25, 0.5, 1., 2., 5., 10., 20.]), &["outcome"])?;
        let http_outcomes = IntCounterVec::new(opts!("http_outcomes_total", "Business request outcomes; 409 order conflicts are expected"), &["operation", "outcome"])?;
        for node in ["1", "2"] {
            for operation in ["get", "set", "delete", "eval"] {
                for status in ["ok", "error"] { cache_redis.with_label_values(&[operation, status, node]); }
                for outcome in ["ok", "error", "timeout"] { redis_duration.with_label_values(&[operation, outcome, node]); }
            }
        }
        for layer in ["l1", "l2"] {
            for result in ["hit", "miss", "error", "invalid"] { cache_lookup.with_label_values(&[layer, result]); }
        }
        for result in ["leader", "follower", "coalesced", "timeout_or_error"] { cache_wait.with_label_values(&[result]); }
        for reason in ["redis_unavailable", "lock_unavailable", "lock_timeout_or_error"] { cache_fallback.with_label_values(&[reason]); }
        for outcome in ["coalesced", "fallback"] { follower_duration.with_label_values(&[outcome]); }
        for result in ["ok", "error"] { cache_invalidation.with_label_values(&[result]); }
        for origin in ["request", "refresh"] { snapshot_loads.with_label_values(&[origin]); }
        for outcome in ["ok", "error", "timeout"] { db_acquire.with_label_values(&[outcome]); }
        for operation in ["snapshot", "activity", "orders", "revision", "order_lookup", "health", "other"] {
            for outcome in ["success", "business_rejection", "client_error", "server_error"] {
                http_outcomes.with_label_values(&[operation, outcome]);
            }
        }
        for operation in ["read_pulse", "read_revision", "write_engagement", "place_order", "read_order", "health"] {
            sql_statements.with_label_values(&[operation]);
            for kind in ["pool_timeout", "query"] { db_errors.with_label_values(&[operation, kind]); }
        }
        for m in [
            Box::new(http_requests.clone()) as Box<dyn prometheus::core::Collector>,
            Box::new(http_duration.clone()), Box::new(cache_lookup.clone()),
            Box::new(cache_fallback.clone()), Box::new(cache_wait.clone()),
            Box::new(cache_invalidation.clone()), Box::new(cache_redis.clone()),
            Box::new(db_queries.clone()), Box::new(db_duration.clone()),
            Box::new(app_info.clone()), Box::new(db_pool.clone()), Box::new(db_acquire.clone()),
            Box::new(db_errors.clone()), Box::new(sql_statements.clone()), Box::new(snapshot_loads.clone()),
            Box::new(redis_duration.clone()), Box::new(follower_duration.clone()), Box::new(http_outcomes.clone()),
        ] {
            registry.register(m)?;
        }
        Ok(Self { registry, http_requests, http_duration, cache_lookup, cache_fallback,
                   cache_wait, cache_invalidation, cache_redis, db_queries, db_duration,
                   app_info, db_pool, db_acquire, db_errors, sql_statements, snapshot_loads,
                   redis_duration, follower_duration, http_outcomes })
    }
    pub fn db_error(&self, operation: &str, error: &sqlx::Error) {
        let kind = if matches!(error, sqlx::Error::PoolTimedOut) { "pool_timeout" } else { "query" };
        self.db_errors.with_label_values(&[operation, kind]).inc();
    }
    pub fn render(&self) -> Result<String, String> {
        let encoder = TextEncoder::new();
        let mut bytes = Vec::new();
        encoder.encode(&self.registry.gather(), &mut bytes).map_err(|e| e.to_string())?;
        String::from_utf8(bytes).map_err(|e| e.to_string())
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn export_distinguishes_outcomes_and_pool_timeouts() {
        let metrics = Metrics::for_strategy("invalidate").unwrap();
        metrics.http_duration.with_label_values(&["POST", "/orders", "business_rejection"]).observe(0.02);
        metrics.db_error("read_pulse", &sqlx::Error::PoolTimedOut);
        let families = metrics.registry.gather();
        let errors = families.iter().find(|family| family.get_name() == "db_errors_total").unwrap();
        let timeout = errors.get_metric().iter().find(|metric| metric.get_counter().get_value() == 1.0).unwrap();
        assert!(timeout.get_label().iter().any(|label| label.get_name() == "kind" && label.get_value() == "pool_timeout"));
        assert!(timeout.get_label().iter().any(|label| label.get_name() == "strategy" && label.get_value() == "invalidate"));
        let duration = families.iter().find(|family| family.get_name() == "http_request_duration_seconds").unwrap();
        assert_eq!(duration.get_metric()[0].get_histogram().get_sample_count(), 1);
        assert!(duration.get_metric()[0].get_label().iter().any(|label| label.get_name() == "outcome" && label.get_value() == "business_rejection"));
    }
}
