# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

resource "azurerm_api_management_api" "webiq" {
  name                  = "webiq"
  resource_group_name   = azurerm_resource_group.this.name
  api_management_name   = azurerm_api_management.this.name
  revision              = "1"
  display_name          = "Web IQ Gateway"
  description           = "Validates user tokens, requires department attribution, redacts sensitive query data, and calls Web IQ with managed identity."
  path                  = "webiq"
  protocols             = ["https"]
  service_url           = var.webiq_base_url
  subscription_required = false

  import {
    content_format = "openapi"
    content_value  = file("${path.module}/openapi.yaml")
  }
}

resource "azurerm_api_management_api_policy" "webiq" {
  api_name            = azurerm_api_management_api.webiq.name
  api_management_name = azurerm_api_management.this.name
  resource_group_name = azurerm_resource_group.this.name

  xml_content = templatefile("${path.module}/policies/web-search.xml.tftpl", {
    entra_tenant_id        = var.entra_tenant_id
    api_allowed_client_ids = var.api_allowed_client_ids
    api_audience           = replace(replace(replace(var.api_audience, "&", "&amp;"), "<", "&lt;"), ">", "&gt;")
    api_required_scope     = replace(replace(replace(var.api_required_scope, "&", "&amp;"), "<", "&lt;"), ">", "&gt;")
    webiq_token_resource   = replace(replace(replace(replace(var.webiq_token_resource, "&", "&amp;"), "<", "&lt;"), ">", "&gt;"), "\"", "&quot;")
  })
}
