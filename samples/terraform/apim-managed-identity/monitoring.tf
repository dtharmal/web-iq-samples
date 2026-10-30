# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

resource "azurerm_log_analytics_workspace" "this" {
  name                = "${local.base_name}-logs"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  sku                 = "PerGB2018"
  retention_in_days   = var.log_retention_days
  tags                = local.common_tags
}

resource "azurerm_application_insights" "this" {
  name                         = "${local.base_name}-appi"
  location                     = azurerm_resource_group.this.location
  resource_group_name          = azurerm_resource_group.this.name
  workspace_id                 = azurerm_log_analytics_workspace.this.id
  application_type             = "web"
  local_authentication_enabled = false
  tags                         = local.common_tags
}

resource "azurerm_role_assignment" "application_insights" {
  scope                = azurerm_application_insights.this.id
  role_definition_name = "Monitoring Metrics Publisher"
  principal_id         = azurerm_api_management.this.identity[0].principal_id
  principal_type       = "ServicePrincipal"
}

resource "azurerm_api_management_logger" "application_insights" {
  name                = "application-insights"
  api_management_name = azurerm_api_management.this.name
  resource_group_name = azurerm_resource_group.this.name
  resource_id         = azurerm_application_insights.this.id
  buffered            = true

  application_insights {
    connection_string  = azurerm_application_insights.this.connection_string
    identity_client_id = "SystemAssigned"
  }

  depends_on = [azurerm_role_assignment.application_insights]
}

resource "azurerm_api_management_api_diagnostic" "webiq" {
  count = var.enable_telemetry ? 1 : 0

  identifier                = "applicationinsights"
  resource_group_name       = azurerm_resource_group.this.name
  api_management_name       = azurerm_api_management.this.name
  api_name                  = azurerm_api_management_api.webiq.name
  api_management_logger_id  = azurerm_api_management_logger.application_insights.id
  sampling_percentage       = var.logging_sampling_percentage
  always_log_errors         = true
  log_client_ip             = false
  verbosity                 = "information"
  http_correlation_protocol = "W3C"
  operation_name_format     = "Name"

  frontend_request {
    body_bytes = 0
    headers_to_log = [
      "content-type",
    ]
  }

  frontend_response {
    body_bytes = 0
    headers_to_log = [
      "content-type",
    ]
  }

  backend_request {
    body_bytes = 0
    headers_to_log = [
      "content-type",
    ]
  }

  backend_response {
    body_bytes = 0
    headers_to_log = [
      "content-type",
    ]
  }
}
