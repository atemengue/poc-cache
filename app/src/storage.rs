use crate::metrics::Metrics;
use serde::{Deserialize, Serialize};
use sqlx::{Acquire, PgPool, Row, pool::PoolConnection, Postgres};
use std::time::{Duration, Instant};

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct ProductSnapshot {
    pub id: i32,
    pub name: String,
    pub category: String,
    pub price_cents: i32,
    pub stock: i32,
    pub revision: i64,
    pub interactions: i64,
    pub views: i64,
    pub favorites: i64,
    pub purchases: i64,
    pub demand_score: i64,
}
#[derive(Debug, Serialize)]
pub struct OrderResult {
    pub order_id: i64,
    pub product_id: i32,
    pub remaining_stock: i32,
    pub revision: i64,
    pub status: &'static str,
}
#[derive(Debug, Serialize)]
pub struct OrderStatus {
    pub id: i64,
    pub product_id: i32,
    pub quantity: i32,
    pub status: String,
}
#[derive(Debug)]
pub enum OrderFailure { NotFound, InsufficientStock, Db(sqlx::Error) }
impl From<sqlx::Error> for OrderFailure {
    fn from(error: sqlx::Error) -> Self { Self::Db(error) }
}
#[derive(Clone)]
pub struct Store {
    pub pool: PgPool,
    pub extra_delay: Duration,
    pub metrics: Metrics,
}
impl Store {
    async fn acquire(&self) -> Result<PoolConnection<Postgres>, sqlx::Error> {
        let started = Instant::now();
        let result = self.pool.acquire().await;
        let outcome = match &result { Ok(_) => "ok", Err(sqlx::Error::PoolTimedOut) => "timeout", Err(_) => "error" };
        self.metrics.db_acquire.with_label_values(&[outcome]).observe(started.elapsed().as_secs_f64());
        result
    }
    fn statement(&self, operation: &str) { self.metrics.sql_statements.with_label_values(&[operation]).inc(); }
    pub async fn health(&self) -> Result<(), sqlx::Error> {
        let result = async {
            let mut conn = self.acquire().await?;
            self.statement("health");
            sqlx::query("SELECT 1").execute(&mut *conn).await?;
            Ok(())
        }.await;
        if let Err(ref err) = result { self.metrics.db_error("health", err); }
        result
    }
    pub async fn snapshot(&self, id: i32, origin: &str) -> Result<Option<ProductSnapshot>, sqlx::Error> {
        let start = Instant::now();
        self.metrics.snapshot_loads.with_label_values(&[origin]).inc();
        self.metrics.db_queries.with_label_values(&["read_pulse"]).inc();
        let result = async {
            if !self.extra_delay.is_zero() { tokio::time::sleep(self.extra_delay).await; }
            let mut conn = self.acquire().await?;
            self.statement("read_pulse");
            // Intentional expensive read model. The flash-sale product (id=1) has
            // ~70% of all order_events; every DB-only call recomputes the aggregate.
            let row = sqlx::query(r#"
                SELECT p.id, p.name, p.category, p.price_cents, p.stock, p.revision,
                  COUNT(e.id)::bigint AS interactions,
                  COUNT(e.id) FILTER (WHERE e.kind='view')::bigint AS views,
                  COUNT(e.id) FILTER (WHERE e.kind='favorite')::bigint AS favorites,
                  COUNT(e.id) FILTER (WHERE e.kind='purchase')::bigint AS purchases
                FROM products p LEFT JOIN order_events e ON e.product_id=p.id
                WHERE p.id=$1 GROUP BY p.id
            "#).bind(id).fetch_optional(&mut *conn).await?;
            Ok(row.map(|r| {
                let views: i64 = r.get("views");
                let favorites: i64 = r.get("favorites");
                let purchases: i64 = r.get("purchases");
                ProductSnapshot {
                    id: r.get("id"), name: r.get("name"), category: r.get("category"),
                    price_cents: r.get("price_cents"), stock: r.get("stock"),
                    revision: r.get("revision"), interactions: r.get("interactions"),
                    views, favorites, purchases,
                    demand_score: views + 3 * favorites + 5 * purchases,
                }
            }))
        }.await;
        self.metrics.db_duration.with_label_values(&["read_pulse"]).observe(start.elapsed().as_secs_f64());
        if let Err(ref err) = result { self.metrics.db_error("read_pulse", err); }
        result
    }
    pub async fn revision(&self, id: i32) -> Result<Option<i64>, sqlx::Error> {
        self.metrics.db_queries.with_label_values(&["read_revision"]).inc();
        let result = async {
            let mut conn = self.acquire().await?;
            self.statement("read_revision");
            sqlx::query_scalar("SELECT revision FROM products WHERE id=$1").bind(id)
                .fetch_optional(&mut *conn).await
        }.await;
        if let Err(ref err) = result { self.metrics.db_error("read_revision", err); }
        result
    }
    pub async fn add_activity(&self, id: i32, kind: &str) -> Result<Option<i64>, sqlx::Error> {
        let start = Instant::now();
        self.metrics.db_queries.with_label_values(&["write_engagement"]).inc();
        let result = async {
            let mut conn = self.acquire().await?;
            let mut tx = conn.begin().await?;
            self.statement("write_engagement");
            let rev: Option<i64> = sqlx::query_scalar("UPDATE products SET revision=revision+1 WHERE id=$1 RETURNING revision")
                .bind(id).fetch_optional(&mut *tx).await?;
            if let Some(revision) = rev {
                self.statement("write_engagement");
                sqlx::query("INSERT INTO order_events(product_id,kind) VALUES($1,$2)")
                    .bind(id).bind(kind).execute(&mut *tx).await?;
                tx.commit().await?;
                Ok(Some(revision))
            } else {
                // Drop uncommitted transaction (no effect).
                Ok(None)
            }
        }.await;
        self.metrics.db_duration.with_label_values(&["write_engagement"]).observe(start.elapsed().as_secs_f64());
        if let Err(ref err) = result { self.metrics.db_error("write_engagement", err); }
        result
    }
    pub async fn place_order(&self, id: i32, qty: i32) -> Result<OrderResult, OrderFailure> {
        let started = Instant::now();
        self.metrics.db_queries.with_label_values(&["place_order"]).inc();
        let result = self.place_order_inner(id, qty).await;
        self.metrics.db_duration.with_label_values(&["place_order"]).observe(started.elapsed().as_secs_f64());
        if let Err(OrderFailure::Db(ref err)) = result { self.metrics.db_error("place_order", err); }
        result
    }
    async fn place_order_inner(&self, id: i32, qty: i32) -> Result<OrderResult, OrderFailure> {
        let mut conn = self.acquire().await?;
        let mut tx = conn.begin().await?;
        self.statement("place_order");
        // One conditional UPDATE protects stock against overselling. Cache is never
        // consulted to make this decision.
        let stock_rev: Option<(i32, i64)> = sqlx::query_as(
            "UPDATE products SET stock=stock-$2, revision=revision+1 WHERE id=$1 AND stock >= $2 RETURNING stock,revision"
        ).bind(id).bind(qty).fetch_optional(&mut *tx).await?;
        let (stock, revision) = match stock_rev {
            Some(row) => row,
            None => {
                self.statement("place_order");
                let exists: Option<i32> = sqlx::query_scalar("SELECT id FROM products WHERE id=$1")
                    .bind(id).fetch_optional(&mut *tx).await?;
                return Err(if exists.is_some() { OrderFailure::InsufficientStock } else { OrderFailure::NotFound });
            }
        };
        self.statement("place_order");
        let order_id: i64 = sqlx::query_scalar("INSERT INTO orders(product_id,quantity) VALUES($1,$2) RETURNING id")
            .bind(id).bind(qty).fetch_one(&mut *tx).await?;
        self.statement("place_order");
        sqlx::query("INSERT INTO order_events(product_id,kind) VALUES($1,'purchase')")
            .bind(id).execute(&mut *tx).await?;
        tx.commit().await?;
        Ok(OrderResult { order_id, product_id: id, remaining_stock: stock, revision, status: "created" })
    }
    pub async fn order(&self, id: i64) -> Result<Option<OrderStatus>, sqlx::Error> {
        self.metrics.db_queries.with_label_values(&["read_order"]).inc();
        let result = async {
            let mut conn = self.acquire().await?;
            self.statement("read_order");
            let row = sqlx::query("SELECT id,product_id,quantity,status FROM orders WHERE id=$1")
                .bind(id).fetch_optional(&mut *conn).await?;
            Ok(row.map(|r| OrderStatus { id: r.get("id"), product_id: r.get("product_id"),
                                        quantity: r.get("quantity"), status: r.get("status") }))
        }.await;
        if let Err(ref err) = result { self.metrics.db_error("read_order", err); }
        result
    }
}
