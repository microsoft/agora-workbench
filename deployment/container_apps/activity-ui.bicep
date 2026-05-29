// ---------------------------------------------------------------------------
// Deploy the Activity UI monitoring sidecar as an Azure Container App.
//
// This is a lightweight FastAPI app that receives events from MCP servers and
// streams them to browsers via SSE. It differs from MCP servers (main.bicep):
//   - No conda/volumes/heavy compute
//   - Single replica (in-memory event buffer)
//   - EasyAuth for all endpoints (browser login + service-to-service Bearer)
//   - MCP servers authenticate via managed identity token acquisition
//
// Prerequisites (same as main.bicep — created once via setup.sh):
//   - Resource group, ACR, ACA Managed Environment, Managed Identity
//   - Entra ID app registration for the activity UI (browser login + API audience)
//   - MCP servers' managed identity authorized to acquire tokens for this app
//
// Usage:
//   az deployment group create \
//     --resource-group <rg> \
//     --template-file  activity-ui.bicep \
//     --parameters     parameters/activity-ui.bicepparam
// ---------------------------------------------------------------------------

targetScope = 'resourceGroup'

// ── Existing infrastructure references ──────────────────────────────────────

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

@description('Entra ID application client ID for browser EasyAuth.')
param entraClientId string = ''

@description('Entra ID tenant ID.')
param entraTenantId string = ''

@secure()
@description('Client secret for the Entra app registration (EasyAuth browser login).')
param entraClientSecret string = ''

// ── Container parameters ────────────────────────────────────────────────────

@description('Full container image reference including tag.')
param containerImage string = ''

@description('Internal port the activity UI listens on.')
param containerPort int = 8030

@description('CPU cores to allocate.')
param cpu string = '0.25'

@description('Memory in Gi to allocate.')
param memory string = '0.5Gi'

// ── Variables ───────────────────────────────────────────────────────────────

var appName = 'activity-ui'

var appSecrets = [
  { name: 'entra-client-secret', value: entraClientSecret }
]

var appEnv = [
  { name: 'ACTIVITY_UI_HOST', value: '0.0.0.0' }
  { name: 'ACTIVITY_UI_PORT', value: string(containerPort) }
  { name: 'AZURE_CLIENT_ID', value: identityClientId }
]

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
      secrets: appSecrets
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
          resources: {
            cpu: json(cpu)
            memory: memory
          }
          env: appEnv
          probes: [
            {
              type: 'Liveness'
              httpGet: {
                path: '/health'
                port: containerPort
                scheme: 'HTTP'
              }
              initialDelaySeconds: 5
              periodSeconds: 30
              timeoutSeconds: 5
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: {
                path: '/health'
                port: containerPort
                scheme: 'HTTP'
              }
              initialDelaySeconds: 2
              periodSeconds: 10
              timeoutSeconds: 5
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

// ── EasyAuth (Entra ID) ─────────────────────────────────────────────────────
// Protects all endpoints. Browser users get redirected to Entra login.
// MCP servers authenticate via Bearer token (managed identity → activity UI
// app registration audience). Health probes are excluded.

resource authConfig 'Microsoft.App/containerApps/authConfigs@2024-03-01' = {
  parent: app
  name: 'current'
  properties: {
    platform: {
      enabled: true
    }
    globalValidation: {
      unauthenticatedClientAction: 'RedirectToLoginPage'
      excludedPaths: [
        '/health'
        '/healthz'
      ]
    }
    identityProviders: {
      azureActiveDirectory: {
        registration: {
          clientId: entraClientId
          clientSecretSettingName: 'entra-client-secret'
          openIdIssuer: 'https://login.microsoftonline.com/${entraTenantId}/v2.0'
        }
        validation: {
          allowedAudiences: [
            'api://${entraClientId}'
          ]
        }
      }
    }
  }
}

// ── Outputs ─────────────────────────────────────────────────────────────────

@description('FQDN of the deployed Activity UI.')
output fqdn string = app.properties.configuration.ingress.fqdn

@description('Full URL for ACTIVITY_UI_URL (set this in MCP server env vars).')
output activityUiUrl string = 'https://${app.properties.configuration.ingress.fqdn}'
