mod cache;
mod config;
mod metrics;
mod storage;

use axum::{
    body::Body,
    extract::{MatchedPath, Path, State},
    http::{header, HeaderValue, Request, StatusCode},
    middleware::{self, Next},
    response::{IntoResponse, Response},
    routing::{get, post},
    Json, Router,
};
use serde::Deserialize;
use serde_json::{json, Value};
use sqlx::{postgres::PgPoolOptions, PgPool};
use std::{sync::Arc, time::{Duration, Instant}};
use tracing_subscriber::EnvFilter;

use cache::{snapshot_key, Cache};
use config::Config;
use metrics::Metrics;
use storage::{OrderFailure, Store};

#[derive(Clone)]
struct AppState { settings: Config, db: Store, cache: Cache, metrics: Metrics }

struct ApiError { status: StatusCode, message: String }
impl ApiError {
    fn new(status: StatusCode, message: impl Into<String>) -> Self { Self { status, message: message.into() } }
    fn db(error: sqlx::Error) -> Self {
        tracing::error!(%error, "PostgreSQL operation failed");
        Self::new(StatusCode::SERVICE_UNAVAILABLE, "database operation failed")
    }
}
impl IntoResponse for ApiError {
    fn into_response(self) -> Response { (self.status, Json(json!({"detail": self.message}))).into_response() }
}
fn verify_id(id: i32) -> Result<i32, ApiError> {
    if !(1..=10_000).contains(&id) { return Err(ApiError::new(StatusCode::NOT_FOUND, "product not found")); }
    Ok(id)
}

async fn health(State(state): State<Arc<AppState>>) -> Result<Json<Value>, ApiError> {
    state.db.health().await.map_err(ApiError::db)?;
    Ok(Json(json!({"status":"ok","cache_mode": state.settings.mode})))
}
async fn debug_config(State(state): State<Arc<AppState>>) -> Json<Value> {
    Json(json!({"cache_mode":state.settings.mode, "ttl_seconds":state.settings.ttl,
               "l1_ttl_seconds":state.settings.l1_ttl.as_secs_f64(),
               "db_query_extra_ms":state.settings.db_delay.as_secs_f64()*1000.0,
               "runtime":"rust", "note":"Laboratory only; not a production security interface"}))
}
async fn snapshot(Path(id): Path<i32>, State(state): State<Arc<AppState>>) -> Result<Response, ApiError> {
    let id = verify_id(id)?;
    let key = snapshot_key(id);
    let (value, source) = state.cache.get_or_load(&key, || state.db.snapshot(id, "request")).await.map_err(ApiError::db)?;
    let value = value.ok_or_else(|| ApiError::new(StatusCode::NOT_FOUND, "product not found"))?;
    let mut response = Json(value).into_response();
    response.headers_mut().insert("X-Cache-Source", HeaderValue::from_static(source));
    response.headers_mut().insert("X-Cache-Mode", HeaderValue::from_str(&state.settings.mode)
                                  .expect("validated cache mode"));
    let hostname = std::env::var("HOSTNAME").unwrap_or_else(|_| "unknown".to_string());
    if let Ok(h) = HeaderValue::from_str(&hostname) { response.headers_mut().insert("X-App-Instance", h); }
    Ok(response)
}
async fn revision(Path(id): Path<i32>, State(state): State<Arc<AppState>>) -> Result<Json<Value>, ApiError> {
    let id = verify_id(id)?;
    let r = state.db.revision(id).await.map_err(ApiError::db)?
        .ok_or_else(|| ApiError::new(StatusCode::NOT_FOUND, "product not found"))?;
    Ok(Json(json!({"id": id,"revision":r,"source":"PostgreSQL"})))
}
#[derive(Deserialize)]
struct Activity { #[serde(default = "favorite")] kind: String }
fn favorite() -> String { "favorite".to_string() }
async fn activity(Path(id): Path<i32>, State(state): State<Arc<AppState>>,
                  Json(payload): Json<Activity>) -> Result<Json<Value>, ApiError> {
    let id = verify_id(id)?;
    if !["view", "favorite", "purchase"].contains(&payload.kind.as_str()) {
        return Err(ApiError::new(StatusCode::UNPROCESSABLE_ENTITY, "kind must be view, favorite, or purchase"));
    }
    let r = state.db.add_activity(id, &payload.kind).await.map_err(ApiError::db)?
        .ok_or_else(|| ApiError::new(StatusCode::NOT_FOUND, "product not found"))?;
    let key = snapshot_key(id);
    match state.settings.mode.as_str() {
        "invalidate" | "protected" | "l1l2" | "sharded" => state.cache.invalidate(&key).await,
        "write_through" => {
            if let Ok(Some(fresh)) = state.db.snapshot(id, "refresh").await { state.cache.set(&key, &fresh).await; }
            else { state.cache.invalidate(&key).await; }
        }
        _ => {} // 'aside' intentionally goes stale until TTL; 'db' bypasses cache.
    }
    Ok(Json(json!({"id":id,"revision":r,"kind":payload.kind})))
}
#[derive(Deserialize)]
struct OrderRequest { product_id: i32, #[serde(default = "one")] quantity: i32 }
fn one() -> i32 { 1 }
async fn place_order(State(state): State<Arc<AppState>>,
                     Json(body): Json<OrderRequest>) -> Result<Json<storage::OrderResult>, ApiError> {
    let id = verify_id(body.product_id)?;
    if body.quantity < 1 { return Err(ApiError::new(StatusCode::UNPROCESSABLE_ENTITY, "quantity must be positive")); }
    let response = state.db.place_order(id, body.quantity).await.map_err(|failure| match failure {
        OrderFailure::NotFound => ApiError::new(StatusCode::NOT_FOUND, "product not found"),
        OrderFailure::InsufficientStock => ApiError::new(StatusCode::CONFLICT, "insufficient_stock"),
        OrderFailure::Db(err) => ApiError::db(err),
    })?;
    // Database commit precedes best-effort invalidation. Cache cannot oversell stock.
    if state.settings.mode != "db" { state.cache.invalidate(&snapshot_key(id)).await; }
    Ok(Json(response))
}
async fn order(Path(id): Path<i64>, State(state): State<Arc<AppState>>)
               -> Result<Json<storage::OrderStatus>, ApiError> {
    let value = state.db.order(id).await.map_err(ApiError::db)?
        .ok_or_else(|| ApiError::new(StatusCode::NOT_FOUND, "order not found"))?;
    Ok(Json(value))
}
async fn scrape(State(state): State<Arc<AppState>>) -> Result<Response, ApiError> {
    let size = state.db.pool.size();
    let idle = state.db.pool.num_idle() as u32;
    for (label, value) in [("size", size), ("idle", idle), ("used", size.saturating_sub(idle)), ("limit", state.settings.db_pool_size)] {
        state.metrics.db_pool.with_label_values(&[label]).set(i64::from(value));
    }
    let body = state.metrics.render().map_err(|err| ApiError::new(StatusCode::INTERNAL_SERVER_ERROR, err))?;
    Ok(([(header::CONTENT_TYPE, "text/plain; version=0.0.4; charset=utf-8")], body).into_response())
}
async fn docs() -> axum::response::Html<&'static str> {
    axum::response::Html(r#"<!doctype html><html><head><title>OrderFlow Rust API</title></head>
    <body><h1>OrderFlow Rust API</h1><p>Flash-sale cache PoC</p><ul>
    <li>GET /products/1/snapshot — expensive cached aggregation</li>
    <li>GET /products/1/revision — authoritative revision</li>
    <li>POST /products/1/activity JSON {&quot;kind&quot;:&quot;favorite&quot;} — mutation</li>
    <li>POST /orders JSON {&quot;product_id&quot;:1,&quot;quantity&quot;:1} — atomic stock reservation</li>
    <li>GET /orders/1 — order status</li><li>GET /health, /debug/config, /metrics</li>
    </ul><p>See README.md for JSON shapes and benchmark operations.
    Interactive OpenAPI/Swagger UI is not bundled.</p></body></html>"#)
}
async fn observe(State(state): State<Arc<AppState>>, request: Request<Body>, next: Next) -> Response {
    let started = Instant::now();
    let method = request.method().as_str().to_owned();
    // Prefer Axum's normalized path. Retain bounded route names if middleware
    // executes before MatchedPath is populated (version-dependent behavior).
    let path = request.extensions().get::<MatchedPath>()
        .map(|p| p.as_str().to_owned()).unwrap_or_else(|| {
            let raw = request.uri().path();
            if raw.starts_with("/products/") && raw.ends_with("/snapshot") {
                "/products/:id/snapshot".into()
            } else if raw.starts_with("/products/") && raw.ends_with("/revision") {
                "/products/:id/revision".into()
            } else if raw.starts_with("/products/") && raw.ends_with("/activity") {
                "/products/:id/activity".into()
            } else if raw.starts_with("/orders/") {
                "/orders/:id".into()
            } else if ["/orders", "/health", "/metrics", "/docs", "/debug/config"].contains(&raw) {
                raw.to_string()
            } else {
                "unmatched".into()
            }
        });
    let response = next.run(request).await;
    if path != "/metrics" {
        let status = response.status().as_u16().to_string();
        let operation = match path.as_str() {
            "/products/:id/snapshot" => "snapshot", "/products/:id/activity" => "activity",
            "/products/:id/revision" => "revision", "/orders" => "orders",
            "/orders/:id" => "order_lookup", "/health" => "health", _ => "other",
        };
        let outcome = if response.status().is_server_error() { "server_error" }
            else if response.status() == StatusCode::CONFLICT && operation == "orders" { "business_rejection" }
            else if response.status().is_client_error() { "client_error" } else { "success" };
        state.metrics.http_outcomes.with_label_values(&[operation, outcome]).inc();
        state.metrics.http_requests.with_label_values(&[&method, &path, &status]).inc();
        state.metrics.http_duration.with_label_values(&[&method, &path, outcome]).observe(started.elapsed().as_secs_f64());
    }
    response
}

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    tracing_subscriber::fmt().with_env_filter(EnvFilter::try_from_default_env()
        .unwrap_or_else(|_| EnvFilter::new("info"))).init();
    let config = Config::from_env()?;
    let metrics = Metrics::for_strategy(&config.mode)?;
    metrics.app_info.with_label_values(&[&std::env::var("DEPLOYMENT_NAME").unwrap_or_else(|_| "orderflow-cache".into())]).set(1);
    let pg: PgPool = PgPoolOptions::new().max_connections(config.db_pool_size)
        .acquire_timeout(Duration::from_secs(10)).connect(&config.database_url).await?;
    let store = Store { pool: pg, extra_delay: config.db_delay, metrics: metrics.clone() };
    let cache = Cache::new(config.mode.clone(), config.ttl, config.l1_ttl, config.redis_timeout,
                           &config.redis_1_url, &config.redis_2_url, metrics.clone()).await?;
    let state = Arc::new(AppState { settings: config, db: store, cache, metrics });
    let router = Router::new()
        .route("/health", get(health))
        .route("/debug/config", get(debug_config))
        .route("/products/:id/snapshot", get(snapshot))
        .route("/products/:id/revision", get(revision))
        .route("/products/:id/activity", post(activity))
        .route("/orders", post(place_order))
        .route("/orders/:id", get(order))
        .route("/metrics", get(scrape))
        .route("/docs", get(docs))
        .layer(middleware::from_fn_with_state(state.clone(), observe))
        .with_state(state);
    let listener = tokio::net::TcpListener::bind("0.0.0.0:8000").await?;
    tracing::info!("OrderFlow Rust API listening on :8000");
    axum::serve(listener, router).await?;
    Ok(())
}
