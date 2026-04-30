# =============================================================================
# Custom openLCA IPC Server Docker image
#
# Replaces ghcr.io/greendelta/olca-ipc-server with an org-controlled image
# that uses Microsoft Container Registry (MCR) base images per org policy.
#
# Based on: https://github.com/GreenDelta/olca-ipc-container
# License:  The olca-ipc library is licensed under the MPL 2.0.
#           See https://mvnrepository.com/artifact/org.openlca/olca-ipc
#
# Build:
#   docker build -t olca-ipc-server:local .
#
# Run:
#   docker run -p 8080:8080 -v /your/openlca-data:/app/data olca-ipc-server:local
#   # Load a specific database in read-only mode:
#   docker run -p 8080:8080 -v /your/openlca-data:/app/data olca-ipc-server:local \
#     -db mydb --readonly
# =============================================================================

# Stage: Resolve Maven dependencies (JARs from Maven Central — no private feeds)
FROM mcr.microsoft.com/openjdk/jdk:17-ubuntu AS mvn-builder

# Install Maven via apt (package manager verifies signatures)
RUN apt-get update && apt-get install -y --no-install-recommends maven \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Copy the Maven descriptor and resolve all runtime JARs into target/lib
COPY pom.xml .
RUN mvn package -q --batch-mode

# =============================================================================
# Stage: Runtime image
# =============================================================================
FROM mcr.microsoft.com/openjdk/jdk:17-ubuntu

# Run as a non-root user to reduce attack surface for this network-facing service.
RUN groupadd --system olca && useradd --system --gid olca --no-create-home olca

# Create required directories and hand ownership to the service user:
#   /app/lib  — server JARs
#   /app/data — openLCA database workspace (mounted by the user at runtime)
RUN mkdir -p /app/lib /app/data && chown -R olca:olca /app

# Copy resolved JARs from the build stage
COPY --from=mvn-builder /build/target/lib /app/lib

# Copy the startup script
COPY run.sh /app/run.sh
RUN chmod +x /app/run.sh && chown olca:olca /app/run.sh

USER olca
WORKDIR /app

# The data directory holds openLCA databases; users mount their workspace here.
VOLUME ["/app/data"]

# JSON-RPC / HTTP port
EXPOSE 8080

ENTRYPOINT ["/app/run.sh"]
