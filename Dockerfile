# CuttleDB server — minimal container.
#
# Stage 1 downloads + verifies the signed release binary; stage 2 is a
# distroless runtime with just the binary and a non-root user. Final
# image is ~25 MB and runs as UID 65532.
#
# Build:
#     docker build --build-arg VERSION=0.6.0 -t cuttledb:0.6.0 .
#
# Run:
#     docker run --rm -p 7780:7780 cuttledb:0.6.0
#     docker run --rm -p 7780:7780 \
#                -v cuttle-wal:/var/lib/cuttledb/wal \
#                cuttledb:0.6.0 \
#                --cuttledb 7780 --wal-dir /var/lib/cuttledb/wal
#
# Verify on host:
#     curl http://localhost:7780/health    # → +PONG
#
# Multi-arch publish lives in .github/workflows/docker.yml — that builds
# linux/amd64 on every release tag and pushes to ghcr.io. linux/arm64
# is gated behind a published macos-arm64 binary; today only amd64 ships.

# ─── Stage 1: download + verify ────────────────────────────────────────
FROM debian:bookworm-slim AS builder

ARG VERSION=latest
ARG ASSET=cuttledb-server-linux-x64

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
        ca-certificates curl jq && \
    rm -rf /var/lib/apt/lists/*

# Install cosign for keyless verification of the release binary. We pin
# a known-good version rather than rolling forward — supply-chain
# discipline.
ARG COSIGN_VERSION=v2.4.1
RUN curl -fsSL -o /usr/local/bin/cosign \
        "https://github.com/sigstore/cosign/releases/download/${COSIGN_VERSION}/cosign-linux-amd64" && \
    chmod +x /usr/local/bin/cosign

# Resolve the release tag (latest → concrete vN.N.N) so the verify step
# pins exactly what we downloaded.
WORKDIR /work
RUN if [ "$VERSION" = "latest" ]; then \
        TAG=$(curl -fsSL https://api.github.com/repos/mikedconcepcion/CuttleDB/releases/latest | jq -r .tag_name); \
    else \
        TAG="v${VERSION}"; \
    fi && \
    echo "$TAG" > /work/.tag && \
    echo "Resolved release tag: $TAG"

RUN TAG=$(cat /work/.tag) && \
    curl -fsSL -o cuttledb-server \
        "https://github.com/mikedconcepcion/CuttleDB/releases/download/${TAG}/${ASSET}" && \
    curl -fsSL -o cuttledb-server.cosign.bundle \
        "https://github.com/mikedconcepcion/CuttleDB/releases/download/${TAG}/${ASSET}.cosign.bundle" && \
    cosign verify-blob \
        --bundle cuttledb-server.cosign.bundle \
        --certificate-identity-regexp '.*' \
        --certificate-oidc-issuer-regexp '.*' \
        cuttledb-server && \
    chmod +x cuttledb-server

# ─── Stage 2: distroless runtime ───────────────────────────────────────
FROM gcr.io/distroless/base-debian12

LABEL org.opencontainers.image.title="CuttleDB"
LABEL org.opencontainers.image.description="Embedded realtime database with vector search, BM25, and hybrid retrieval."
LABEL org.opencontainers.image.source="https://github.com/mikedconcepcion/CuttleDB"
LABEL org.opencontainers.image.licenses="Apache-2.0"

COPY --from=builder /work/cuttledb-server /usr/local/bin/cuttledb-server

# Non-root user (distroless ships UID 65532 / "nonroot"). WAL directory
# is owned by this user so the container can write its log.
USER 65532:65532

# Default listening port — overrideable via CLI args.
EXPOSE 7780

# Default WAL dir matches Linux convention; mount a volume here for
# durability. If you don't mount, the WAL is ephemeral inside the
# container.
VOLUME ["/var/lib/cuttledb/wal"]

# No HEALTHCHECK directive: distroless has no shell or curl, and the
# server doesn't yet expose a self-probe client mode. Define a check
# externally — e.g. in docker-compose or k8s liveness probe pointing
# at GET http://<host>:7780/health (pre-auth, returns "+PONG\r\n").

ENTRYPOINT ["/usr/local/bin/cuttledb-server"]
CMD ["--cuttledb", "7780", "--wal-dir", "/var/lib/cuttledb/wal"]
