# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

output "gateway_url" {
  description = "Public endpoint for the Web IQ web-search operation."
  value       = "${azurerm_api_management.this.gateway_url}/webiq/search/web"
}

output "apim_managed_identity_principal_id" {
  description = "Object/principal ID of the APIM managed identity. Use it to look up the Application (Client) ID required for Web IQ Portal binding."
  value       = azurerm_api_management.this.identity[0].principal_id
}

output "application_insights_name" {
  description = "Application Insights resource receiving APIM request, dependency, exception, and trace telemetry."
  value       = azurerm_application_insights.this.name
}
