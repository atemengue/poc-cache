# Compile native Rust binary. A separate dependency layer makes source edits
# and subsequent classroom builds much faster than a full cold Rust build.
FROM rust:1.88-bookworm AS build
WORKDIR /build
COPY app/Cargo.toml app/Cargo.lock ./
RUN mkdir -p src && printf 'fn main() {}\n' > src/main.rs && cargo build --release --locked
COPY app/src ./src
RUN touch src/main.rs && cargo build --release --locked

# Independent test target; `./poc test-rust` builds this using its own tag,
# rather than replacing the runnable Docker Compose image.
FROM build AS test
RUN cargo test --release --locked

FROM debian:bookworm-slim AS runtime
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
COPY --from=build /build/target/release/orderflow-cache /usr/local/bin/orderflow-cache
ENV RUST_LOG=info
EXPOSE 8000
USER 65534:65534
CMD ["/usr/local/bin/orderflow-cache"]
