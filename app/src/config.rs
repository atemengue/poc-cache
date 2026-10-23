use std::{env, time::Duration};

#[derive(Clone, Debug)]
pub struct Config {
    pub database_url: String,
    pub redis_1_url: String,
    pub redis_2_url: String,
    pub mode: String,
    pub ttl: u64,
    pub l1_ttl: Duration,
    pub db_delay: Duration,
    pub db_pool_size: u32,
    pub redis_timeout: Duration,
}
fn value(name: &str, default: &str) -> String {
    env::var(name).unwrap_or_else(|_| default.to_string())
}
fn parsed<T: std::str::FromStr>(name: &str, default: &str) -> Result<T, String> {
    value(name, default).parse().map_err(|_| format!("Invalid {name}"))
}
impl Config {
    pub fn from_env() -> Result<Self, String> {
        let mode = value("CACHE_MODE", "aside");
        if !["db", "aside", "invalidate", "write_through", "protected", "l1l2", "sharded"]
            .contains(&mode.as_str()) {
            return Err(format!("Unsupported CACHE_MODE: {mode}"));
        }
        let ttl: u64 = parsed("CACHE_TTL_SEC", "30")?;
        let l1_ttl: f64 = parsed("L1_TTL_SEC", "1")?;
        let db_delay: f64 = parsed("DB_QUERY_EXTRA_MS", "0")?;
        let redis_timeout: f64 = parsed("CACHE_TIMEOUT_SEC", "0.15")?;
        let db_pool_size: u32 = parsed("DB_POOL_SIZE", "12")?;
        if ttl < 1 || db_pool_size < 1 || !l1_ttl.is_finite() || l1_ttl < 0.0 ||
            !db_delay.is_finite() || db_delay < 0.0 || !redis_timeout.is_finite() || redis_timeout <= 0.0 {
            return Err("Invalid TTL/pool/delay/timeout configuration".into());
        }
        Ok(Self {
            database_url: value("DATABASE_URL", "postgres://demo:demo@localhost:5432/orderflow"),
            redis_1_url: value("REDIS_1_URL", "redis://localhost:6379/0"),
            redis_2_url: value("REDIS_2_URL", "redis://localhost:6380/0"),
            mode, ttl,
            l1_ttl: Duration::from_secs_f64(l1_ttl),
            db_delay: Duration::from_secs_f64(db_delay / 1000.0),
            db_pool_size,
            redis_timeout: Duration::from_secs_f64(redis_timeout),
        })
    }
}
