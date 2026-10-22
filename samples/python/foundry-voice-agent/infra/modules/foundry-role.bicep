// Copyright (c) Microsoft Corporation.
// Licensed under the MIT License.

param foundryAccountName string
param foundryProjectName string
param principalId string
param siteId string

resource foundryProject 'Microsoft.CognitiveServices/accounts/projects@2025-09-01' existing = {
  name: '${foundryAccountName}/${foundryProjectName}'
}

// Azure AI User: lets the web app identity open native voice sessions in this project only.
var azureAiUserRole = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  '53ca6127-db72-4b80-b1b0-d745d6d5456d'
)

resource voiceRole 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  name: guid(foundryProject.id, siteId, azureAiUserRole)
  scope: foundryProject
  properties: {
    principalId: principalId
    principalType: 'ServicePrincipal'
    roleDefinitionId: azureAiUserRole
  }
}

output roleAssignmentId string = voiceRole.id
