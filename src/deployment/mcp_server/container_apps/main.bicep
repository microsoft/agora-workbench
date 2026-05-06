// ---------------------------------------------------------------------------
// Deploy a single MCP code execution server as an Azure Container App.
//
// Assumes the following already exist (created once via setup.sh):
//   - Resource group
//   - Azure Container Registry (ACR)
//   - ACA Managed Environment (+ Log Analytics workspace)
//   - User-assigned managed identity (with ACR pull role)
//
// Usage:
//   az deployment group create \
//     --resource-group <rg> \
//     --template-file  main.bicep \
//     --parameters     parameters/office.bicepparam
// ---------------------------------------------------------------------------

targetScope = 'resourceGroup'

// ── Existing infrastructure references ──────────────────────────────────────
// These have empty defaults so .bicepparam files compile without them.
// deploy.sh always overrides them from .env values.

@description('Azure region for the Container App.')
param location string = resourceGroup().location

@description('Resource ID of the existing ACA managed environment.')
param environmentId string = ''

@description('Resource ID of the existing user-assigned managed identity.')
param identityId string = ''

@description('Client ID of the existing user-assigned managed identity.')
param identityClientId string = ''

@description('ACR login server (e.g. "myacr.azurecr.io").')
param registryServer string = ''

// ── Auth ────────────────────────────────────────────────────────────────────

@description('Entra ID (Azure AD) application client ID for the MCP server.')
param entraClientId string = ''

@description('Entra ID (Azure AD) tenant ID.')
param entraTenantId string = ''

// ── Per-server parameters ───────────────────────────────────────────────────

@description('Short name of the server to deploy (e.g. "office").')
param serverName string

@description('Full container image reference including tag.')
param containerImage string = ''

@description('Internal port the server listens on.')
param containerPort int = 8000

@description('CPU cores to allocate (ACA consumption plan).')
param cpu string = '1.0'

@description('Memory in Gi to allocate (ACA consumption plan).')
param memory string = '2Gi'

@description('Minimum number of replicas (0 = scale to zero).')
param minReplicas int = 1

@description('Maximum number of replicas.')
param maxReplicas int = 3

@description('Additional environment variables as key-value pairs.')
param extraEnvVars object = {}

// ── Environment variables ───────────────────────────────────────────────────

var baseEnv = [
  { name: 'PORT',                value: string(containerPort) }
  { name: 'HOST',                value: '0.0.0.0' }
  { name: 'ENTRA_CLIENT_ID',    value: entraClientId }
  { name: 'ENTRA_TENANT_ID',    value: entraTenantId }
  { name: 'AZURE_CLIENT_ID',    value: identityClientId }
  { name: 'OBO_SIMULATION_MODE', value: 'false' }
]

var extraEnvArray = [for key in objectKeys(extraEnvVars): {
  name: key
  value: string(extraEnvVars[key])
}]

var allEnv = concat(baseEnv, extraEnvArray)

var appName = '${serverName}-server'

// ── Container App ───────────────────────────────────────────────────────────

resource app 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${identityId}': {}
    }
  }
  properties: {
    managedEnvironmentId: environmentId
    workloadProfileName: 'Consumption'
    configuration: {
      activeRevisionsMode: 'Single'
      ingress: {
        external: true
        targetPort: containerPort
        transport: 'http'
        allowInsecure: false
      }
      registries: [
        {
          server: registryServer
          identity: identityId
        }
      ]
    }
    template: {
      containers: [
        {
          name: appName
          image: containerImage
          command: [
            'python'
            '-m'
            'domains.${serverName}.server.${serverName}_server'
          ]
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          env: allEnv
          probes: [
            {
              type: 'Startup'
              httpGet: {
                path: '/health'
                port: containerPort
                scheme: 'HTTP'
              }
              periodSeconds: 10
              timeoutSeconds: 5
              failureThreshold: 30
            }
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: containerPort
                scheme: 'HTTP'
              }
              initialDelaySeconds: 60
              periodSeconds: 30
              timeoutSeconds: 10
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/health'
                port: containerPort
                scheme: 'HTTP'
              }
              initialDelaySeconds: 30
              periodSeconds: 10
              timeoutSeconds: 5
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        minReplicas: minReplicas
        maxReplicas: maxReplicas
        rules: [
          {
            name: 'http-scaling'
            http: {
              metadata: {
                concurrentRequests: '10'
              }
            }
          }
        ]
      }
    }
  }
}

// ── Outputs ─────────────────────────────────────────────────────────────────

@description('FQDN of the deployed Container App.')
output fqdn string = app.properties.configuration.ingress.fqdn
