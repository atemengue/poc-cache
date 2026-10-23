CREATE TABLE products (id INT PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL, price_cents INT NOT NULL, stock INT NOT NULL, revision BIGINT NOT NULL DEFAULT 0);
CREATE TABLE order_events (id BIGSERIAL PRIMARY KEY, product_id INT NOT NULL REFERENCES products(id), kind TEXT NOT NULL CHECK (kind IN ('view','favorite','purchase')), created_at TIMESTAMPTZ NOT NULL DEFAULT now());
CREATE INDEX order_events_product_idx ON order_events(product_id);
CREATE TABLE orders (id BIGSERIAL PRIMARY KEY, product_id INT NOT NULL REFERENCES products(id), quantity INT NOT NULL, status TEXT NOT NULL DEFAULT 'created', created_at TIMESTAMPTZ NOT NULL DEFAULT now());
INSERT INTO products(id,name,category,price_cents,stock) SELECT i, CASE WHEN i=1 THEN 'Flash Sale Laptop' ELSE 'Product #'||i END, 'Electronics', 99900, 1000 FROM generate_series(1,10000) i;
