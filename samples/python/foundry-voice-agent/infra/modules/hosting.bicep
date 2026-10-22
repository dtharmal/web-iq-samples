// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

param location string
param planName string
param appName string
param tags object
param allowedIpCidrs array
param maxConversations int
param projectEndpoint string
param agentName string
param agentVersion string
param tenantId string
@secure()
param webiqApiKey string
@secure()
param authVerifier string

resource plan 'Microsoft.Web/serverfarms@2024-11-01' = {
  name: planName
  location: location
  tags: tags
  kind: 'linux'
  sku: {
    name: 'B1'
    tier: 'Basic'
    capacity: 1
  }
  properties: {
    reserved: true
    perSiteScaling: false
  }
}

var ipRules = [for (cidr, index) in allowedIpCidrs: {
  name: 'Allowed client ${index + 1}'
  ipAddress: cidr
  action: 'Allow'
  priority: 100 + index
}]

resource site 'Microsoft.Web/sites@2024-11-01' = {
  name: appName
  location: location
  tags: tags
  kind: 'app,linux'
  identity: {
    type: 'SystemAssigned'
  }
  properties: {
    serverFarmId: plan.id
    httpsOnly: true
    publicNetworkAccess: 'Enabled'
    siteConfig: {
      linuxFxVersion: 'PYTHON|3.14'
      appCommandLine: 'python -m app.main'
      alwaysOn: true
      healthCheckPath: '/login'
      numberOfWorkers: 1
      webSocketsEnabled: true
      minTlsVersion: '1.2'
      scmMinTlsVersion: '1.2'
      ftpsState: 'Disabled'
      httpLoggingEnabled: false
      detailedErrorLoggingEnabled: false
      requestTracingEnabled: false
      ipSecurityRestrictions: ipRules
      ipSecurityRestrictionsDefaultAction: empty(allowedIpCidrs) ? 'Allow' : 'Deny'
      scmIpSecurityRestrictionsUseMain: true
    }
  }
}

resource appSettings 'Microsoft.Web/sites/config@2024-11-01' = {
  parent: site
  name: 'appsettings'
  properties: {
    APP_MODE: 'hosted'
    PUBLIC_ORIGIN: 'https://${site.properties.defaultHostName}'
    PORT: '8000'
    MAX_CONVERSATIONS: string(maxConversations)
    PERF_LOG_ENABLED: '1'
    FOUNDRY_PROJECT_ENDPOINT: projectEndpoint
    FOUNDRY_VOICE_AGENT_NAME: agentName
    FOUNDRY_VOICE_AGENT_VERSION: agentVersion
    AZURE_TENANT_ID: tenantId
    WEBIQ_ENDPOINT: 'https://api.microsoft.ai/v3/mcp'
    WEBIQ_API_KEY: webiqApiKey
    DEMO_AUTH_VERIFIER: authVerifier
    SCM_DO_BUILD_DURING_DEPLOYMENT: 'true'
    ENABLE_ORYX_BUILD: 'true'
    ORYX_DISABLE_COMPRESSION: 'true'
    WEBSITES_CONTAINER_START_TIME_LIMIT: '1800'
  }
}

// Entra-capable zip deployment needs no reusable publishing password.
resource scmAuth 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-11-01' = {
  parent: site
  name: 'scm'
  properties: {
    allow: false
  }
}
resource ftpAuth 'Microsoft.Web/sites/basicPublishingCredentialsPolicies@2024-11-01' = {
  parent: site
  name: 'ftp'
  properties: {
    allow: false
  }
}

output appUrl string = 'https://${site.properties.defaultHostName}'
output appId string = site.id
output principalId string = site.identity.principalId
