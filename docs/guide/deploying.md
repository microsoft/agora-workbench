# Deploying your server

Agora Workbench servers are deployed as Docker containers. The repository provides a shared base image and deployment tooling for both local development and Azure Container Apps.

## Local development with Docker

### 1. Build the base image

The base image includes the Agora Workbench runtime and common dependencies.
First, scaffold the deployment files:

```bash
agora-workbench-deploy init --target docker
```

Then build the base image:

```bash
docker build -f deployment/docker/base.Dockerfile -t mcp-server-base:local .
```

### 2. Create your server's Dockerfile

Extend the base image with your domain-specific server:

```dockerfile
FROM mcp-server-base:local

# Copy domain tools package
COPY chemistry_tools/ /app/chemistry_tools/

# Copy server module
COPY server/ /app/server/

# Install domain tools into the execution environment
RUN pip install --no-deps /app/chemistry_tools/

EXPOSE 8000
CMD ["python", "-m", "server.chemistry_server"]
```

### 3. Run with Docker Compose

```yaml
# docker-compose.yml
services:
  chemistry:
    build:
      context: .
      dockerfile: Dockerfile
    ports:
      - "8000:8000"
    environment:
      - HOST=0.0.0.0
      - PORT=8000
```

```bash
docker compose up
```

### Warming the environment

For faster cold starts, warm the Python environment at build time:

```dockerfile
RUN python -m server.chemistry_server --warm
```

This pre-builds the conda/uv/pip environment so it's ready when the container starts.

## Azure Container Apps deployment

Run `agora-workbench-deploy init --target azure` to scaffold the Bicep templates and deploy scripts.

### Prerequisites

- Azure CLI (`az`) authenticated
- A container registry (ACR) for pushing images
- Environment variables in `docker/.env.server`

### Deploy steps

```bash
cd deployment/azure/

# Configure your deployment
cp ../docker/.env.server.example ../docker/.env.server
# Edit .env.server with your Azure resource details

# Build and push
az acr build --registry <your-acr> --image myserver:latest ..

# Deploy via Bicep
az deployment group create \
  --resource-group <your-rg> \
  --template-file main.bicep \
  --parameters containerImage=<your-acr>.azurecr.io/myserver:latest
```

### Environment variables for deployment

| Variable | Description |
|----------|-------------|
| `HOST` | Bind address (default: `0.0.0.0`) |
| `PORT` | Listen port (default: `8000`) |
| `ENTRA_CLIENT_ID` | Entra ID app registration client ID |
| `ENTRA_TENANT_ID` | Azure AD tenant ID |
| `AZURE_CLIENT_ID` | Managed identity client ID |
| `CODE_OUTPUT_TRUNCATION_THRESHOLD` | Output truncation limit |
| `PARALLEL_EXECUTE_MAX_CONCURRENCY` | Max parallel executions |

## Production considerations

### Health checks

Every server exposes `/health` for liveness/readiness probes:

```yaml
# Container Apps health probe
healthProbes:
  - type: liveness
    httpGet:
      path: /health
      port: 8000
    initialDelaySeconds: 10
    periodSeconds: 30
```

### Resource limits

Set execution timeouts and concurrency limits appropriate for your workload:

```python
config = ServerConfig(
    ...,
    max_timeout=300,          # 5 min max per execution
    default_timeout=120,      # 2 min default
    parallel_max_concurrency=4,  # max 4 concurrent executions
    output_truncation_threshold=50_000,
)
```

### Scaling

MCP servers maintain session state and must be available to receive tool calls at any time, so scale-to-zero is not appropriate. Configure a fixed replica count based on your expected concurrency. Each replica runs its own Python environment and session pool independently.

## Multi-server deployment

For deployments with multiple domain servers plus a router, deploy each as a separate Container App and configure the router's upstream URLs to point to the internal endpoints:

```bash
# Each domain server
UPSTREAM_CHEMISTRY_URL=https://chemistry-server.internal.azurecontainerapps.io
UPSTREAM_GIS_URL=https://gis-server.internal.azurecontainerapps.io
```

See [Server networks](server-networks.md) for the router/gateway configuration.
