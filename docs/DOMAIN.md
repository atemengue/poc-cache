# Domain mapping to the supplied event-driven diagram

| Diagram component | PoC scope |
|---|---|
| Order Placement | Implemented: `POST /orders` |
| Inventory | Implemented: atomic PostgreSQL stock reservation |
| Payment | Context only; not simulated |
| Warehouse | Context only; not simulated |
| Notification | Context only; not simulated |
| Order Fulfillment | Context only; not simulated |
| Shipping | Context only; not simulated |

**Cache-critical scenario:** a flash-sale laptop is queried thousands of times per second. The product snapshot aggregates 70% of a large event history around product #1. A DB-only read recomputes that aggregate on every request. Redis caches the projection; invalidation and single-flight mitigate stale results and stampedes. Cached availability is informational; purchase correctness depends on the conditional SQL stock decrement. The PoC intentionally exposes the gap between cache speed and data freshness.

Order events in the original diagram (`order-created`, `payment-applied`, `inventory-updated`, `order-fulfilled`, `order-shipped`) are not represented by an event broker in this minimal PoC. Adding a broker would confound the caching experiment with messaging reliability.
