//! Seven benchmark modes. Post-commit invalidation is deliberately best-effort;
//! L1 is per-process and two-way sharding is NOT Redis Cluster/high availability.
use crate::{metrics::Metrics, storage::ProductSnapshot};
use rand::Rng;
use redis::{aio::ConnectionManager, FromRedisValue};
use std::{collections::HashMap, sync::Arc, time::{Duration, Instant}};
use tokio::sync::Mutex;

pub fn snapshot_key(id: i32) -> String { format!("snapshot:v1:product:{id}") }
pub fn shard_for(key: &str) -> usize { (crc32fast::hash(key.as_bytes()) % 2) as usize }

struct L1 {
    values: HashMap<String, (Instant, ProductSnapshot)>,
    ttl: Duration,
    max_entries: usize,
}
impl L1 {
    fn new(ttl: Duration) -> Self { Self { values: HashMap::new(), ttl, max_entries: 10_000 } }
    fn get(&mut self, key: &str) -> Option<ProductSnapshot> {
        if let Some((deadline, item)) = self.values.get(key) {
            if *deadline > Instant::now() { return Some(item.clone()); }
        }
        self.values.remove(key);
        None
    }
    fn set(&mut self, key: &str, value: &ProductSnapshot) {
        if self.ttl.is_zero() { return; }
        if self.values.len() >= self.max_entries && !self.values.contains_key(key) {
            self.values.retain(|_, (deadline, _)| *deadline > Instant::now());
            if self.values.len() >= self.max_entries {
                if let Some(oldest) = self.values.iter().min_by_key(|(_, (t, _))| *t)
                    .map(|(k, _)| k.clone()) { self.values.remove(&oldest); }
            }
        }
        self.values.insert(key.to_owned(), (Instant::now() + self.ttl, value.clone()));
    }
    fn delete(&mut self, key: &str) { self.values.remove(key); }
}

enum Lookup { Hit(ProductSnapshot), Miss, Unavailable }

#[derive(Clone)]
pub struct Cache {
    mode: String,
    ttl: u64,
    timeout: Duration,
    clients: [redis::Client; 2],
    nodes: [Arc<Mutex<Option<ConnectionManager>>>; 2],
    l1: Arc<Mutex<L1>>,
    metrics: Metrics,
}
impl Cache {
    pub async fn new(mode: String, ttl: u64, l1_ttl: Duration, timeout: Duration,
                     url1: &str, url2: &str, metrics: Metrics) -> Result<Self, redis::RedisError> {
        let client1 = redis::Client::open(url1)?;
        let client2 = redis::Client::open(url2)?;
        Ok(Self { mode, ttl, timeout, clients: [client1, client2],
                  nodes: [Arc::new(Mutex::new(None)), Arc::new(Mutex::new(None))],
                  l1: Arc::new(Mutex::new(L1::new(l1_ttl))), metrics })
    }
    fn node(&self, key: &str) -> usize { if self.mode == "sharded" { shard_for(key) } else { 0 } }

    async fn command<T: FromRedisValue>(&self, key: &str, op: &str, cmd: redis::Cmd) -> Option<T> {
        let started = Instant::now();
        let idx = self.node(key);
        // Connect lazily within the same timeout as the command. Redis is optional
        // at startup, and an unused shard must not prevent DB fallback.
        let result = tokio::time::timeout(self.timeout, async {
            let mut node = self.nodes[idx].lock().await;
            if node.is_none() {
                *node = Some(self.clients[idx].get_connection_manager().await?);
            }
            let mut conn = node.as_ref().expect("initialized connection").clone();
            drop(node);
            cmd.query_async::<_, T>(&mut conn).await
        }).await;
        let success = matches!(result, Ok(Ok(_)));
        let outcome = match &result { Ok(Ok(_)) => "ok", Ok(Err(_)) => "error", Err(_) => "timeout" };
        self.metrics.redis_duration.with_label_values(&[op, outcome, if idx == 0 { "1" } else { "2" }])
            .observe(started.elapsed().as_secs_f64());
        self.metrics.cache_redis.with_label_values(&[op, if success { "ok" } else { "error" },
                                                       if idx == 0 { "1" } else { "2" }]).inc();
        match result {
            Ok(Ok(value)) => Some(value),
            Ok(Err(err)) => { tracing::debug!(operation=op, error=%err, "Redis unavailable"); None },
            Err(_) => { tracing::debug!(operation=op, "Redis timeout"); None },
        }
    }
    async fn get(&self, key: &str) -> Lookup {
        if self.mode == "l1l2" {
            if let Some(value) = self.l1.lock().await.get(key) {
                self.metrics.cache_lookup.with_label_values(&["l1", "hit"]).inc();
                return Lookup::Hit(value);
            }
            self.metrics.cache_lookup.with_label_values(&["l1", "miss"]).inc();
        }
        let mut cmd = redis::cmd("GET");
        cmd.arg(key);
        match self.command::<Option<String>>(key, "get", cmd).await {
            None => {
                self.metrics.cache_lookup.with_label_values(&["l2", "error"]).inc();
                Lookup::Unavailable
            }
            Some(None) => {
                self.metrics.cache_lookup.with_label_values(&["l2", "miss"]).inc();
                Lookup::Miss
            }
            Some(Some(raw)) => match serde_json::from_str::<ProductSnapshot>(&raw) {
                Ok(value) => {
                    self.metrics.cache_lookup.with_label_values(&["l2", "hit"]).inc();
                    if self.mode == "l1l2" { self.l1.lock().await.set(key, &value); }
                    Lookup::Hit(value)
                }
                Err(_) => {
                    self.metrics.cache_lookup.with_label_values(&["l2", "invalid"]).inc();
                    Lookup::Miss
                }
            }
        }
    }
    pub async fn set(&self, key: &str, value: &ProductSnapshot) {
        let ttl_ms = self.ttl.saturating_mul(1000);
        let jitter = rand::thread_rng().gen_range(0..=ttl_ms / 10);
        let mut cmd = redis::cmd("SET");
        cmd.arg(key).arg(serde_json::to_string(value).expect("serializable snapshot"))
            .arg("PX").arg(ttl_ms.saturating_add(jitter));
        let written = self.command::<String>(key, "set", cmd).await.is_some();
        if written && self.mode == "l1l2" { self.l1.lock().await.set(key, value); }
    }
    pub async fn invalidate(&self, key: &str) {
        // This deletes only the current process's L1; other replicas may serve stale values.
        self.l1.lock().await.delete(key);
        let mut cmd = redis::cmd("DEL"); cmd.arg(key);
        let ok = self.command::<i32>(key, "delete", cmd).await.is_some();
        self.metrics.cache_invalidation.with_label_values(&[if ok {"ok"} else {"error"}]).inc();
    }
    pub async fn get_or_load<F, Fut>(&self, key: &str, loader: F) -> Result<(Option<ProductSnapshot>, &'static str), sqlx::Error>
    where F: Fn() -> Fut, Fut: std::future::Future<Output=Result<Option<ProductSnapshot>, sqlx::Error>> {
        if self.mode == "db" { return Ok((loader().await?, "db")); }
        match self.get(key).await {
            Lookup::Hit(value) => return Ok((Some(value), "cache")),
            Lookup::Unavailable => {
                self.metrics.cache_fallback.with_label_values(&["redis_unavailable"]).inc();
                return Ok((loader().await?, "db_fallback"));
            }
            Lookup::Miss => {}
        }
        if self.mode == "protected" { return self.singleflight(key, loader).await; }
        let fresh = loader().await?;
        if let Some(ref value) = fresh { self.set(key, value).await; }
        Ok((fresh, "db_miss"))
    }
    async fn singleflight<F, Fut>(&self, key: &str, loader: F) -> Result<(Option<ProductSnapshot>, &'static str), sqlx::Error>
    where F: Fn() -> Fut, Fut: std::future::Future<Output=Result<Option<ProductSnapshot>, sqlx::Error>> {
        let lock_key = format!("lock:{key}");
        let token = format!("{}-{}", rand::random::<u64>(), rand::random::<u64>());
        let mut cmd = redis::cmd("SET");
        cmd.arg(&lock_key).arg(&token).arg("NX").arg("PX").arg(10_000);
        match self.command::<Option<String>>(&lock_key, "set", cmd).await {
            None => {
                self.metrics.cache_fallback.with_label_values(&["lock_unavailable"]).inc();
                Ok((loader().await?, "db_fallback"))
            }
            Some(Some(_)) => {
                self.metrics.cache_wait.with_label_values(&["leader"]).inc();
                // Keep lock cleanup even if DB call errors.
                let result = async {
                    if let Lookup::Hit(hit) = self.get(key).await { return Ok((Some(hit), "cache")); }
                    let fresh = loader().await?;
                    if let Some(ref value) = fresh { self.set(key, value).await; }
                    Ok((fresh, "db_miss"))
                }.await;
                let unlock = "if redis.call('GET',KEYS[1]) == ARGV[1] then return redis.call('DEL',KEYS[1]) else return 0 end";
                let mut cmd = redis::cmd("EVAL");
                cmd.arg(unlock).arg(1).arg(&lock_key).arg(token);
                let _ = self.command::<i32>(&lock_key, "eval", cmd).await;
                result
            }
            Some(None) => {
                let started = Instant::now();
                self.metrics.cache_wait.with_label_values(&["follower"]).inc();
                for _ in 0..75 {
                    tokio::time::sleep(Duration::from_millis(25)).await;
                    match self.get(key).await {
                        Lookup::Hit(hit) => {
                            self.metrics.cache_wait.with_label_values(&["coalesced"]).inc();
                            self.metrics.follower_duration.with_label_values(&["coalesced"]).observe(started.elapsed().as_secs_f64());
                            return Ok((Some(hit), "cache_wait"));
                        }
                        Lookup::Unavailable => break,
                        Lookup::Miss => {}
                    }
                }
                self.metrics.cache_wait.with_label_values(&["timeout_or_error"]).inc();
                self.metrics.follower_duration.with_label_values(&["fallback"]).observe(started.elapsed().as_secs_f64());
                self.metrics.cache_fallback.with_label_values(&["lock_timeout_or_error"]).inc();
                Ok((loader().await?, "db_fallback"))
            }
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    #[tokio::test]
    async fn construction_does_not_require_redis() {
        let cache = tokio::time::timeout(Duration::from_millis(100), Cache::new(
            "db".into(), 30, Duration::from_secs(1), Duration::from_millis(20),
            "redis://127.0.0.1:1/0", "redis://127.0.0.1:1/0", Metrics::new().unwrap(),
        )).await.expect("construction must not connect").unwrap();
        let (_, source) = cache.get_or_load("unused", || async { Ok(None) }).await.unwrap();
        assert_eq!(source, "db");
    }
    #[test] fn stable_shard() {
        let key = snapshot_key(12);
        assert_eq!(shard_for(&key), shard_for(&key));
        assert!(shard_for(&key) <= 1);
    }
    #[test] fn key_format() { assert_eq!(snapshot_key(1), "snapshot:v1:product:1"); }
    #[test] fn l1_expiry() {
        let mut cache = L1::new(Duration::from_millis(10));
        let product = ProductSnapshot { id: 1, name: "x".into(), category: "y".into(),
            price_cents: 1, stock: 1, revision: 0, interactions: 0, views: 0,
            favorites: 0, purchases: 0, demand_score: 0 };
        cache.set("test", &product);
        assert!(cache.get("test").is_some());
        std::thread::sleep(Duration::from_millis(15));
        assert!(cache.get("test").is_none());
    }
    #[test] fn l1_capacity() {
        let mut cache = L1::new(Duration::from_secs(60)); cache.max_entries = 2;
        let p = ProductSnapshot { id: 1, name: "x".into(), category: "y".into(), price_cents: 1,
            stock: 1, revision: 0, interactions: 0, views: 0, favorites: 0, purchases: 0, demand_score: 0 };
        cache.set("a", &p); cache.set("b", &p); cache.set("c", &p);
        assert_eq!(cache.values.len(), 2);
    }
}
