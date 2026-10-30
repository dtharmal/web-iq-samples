# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

resource "random_string" "suffix" {
  length  = 5
  upper   = false
  special = false
}

locals {
  base_name = "${var.name_prefix}-${random_string.suffix.result}"
  common_tags = merge(
    {
      sample = "webiq-apim-managed-identity"
    },
    var.tags
  )
}

resource "azurerm_resource_group" "this" {
  name     = coalesce(var.resource_group_name, "${local.base_name}-rg")
  location = var.location
  tags     = local.common_tags
}

resource "azurerm_api_management" "this" {
  name                = "${local.base_name}-apim"
  location            = azurerm_resource_group.this.location
  resource_group_name = azurerm_resource_group.this.name
  publisher_name      = var.publisher_name
  publisher_email     = var.publisher_email
  sku_name            = var.apim_sku_name
  tags                = local.common_tags

  identity {
    type = "SystemAssigned"
  }

  protocols {
    http2_enabled = true
  }

  security {
    frontend_ssl30_enabled = false
    frontend_tls10_enabled = false
    frontend_tls11_enabled = false
    backend_ssl30_enabled  = false
    backend_tls10_enabled  = false
    backend_tls11_enabled  = false
  }
}
