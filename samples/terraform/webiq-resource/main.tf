# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

locals {
  # Same API version as the Web IQ Azure CLI deployment guide.
  webiq_api_version = "2026-06-01-preview"

  resource_group_id = (
    var.create_resource_group
    ? azapi_resource.resource_group[0].id
    : "/subscriptions/${var.subscription_id}/resourceGroups/${var.resource_group_name}"
  )

  common_tags = merge(
    {
      sample = "webiq-resource"
    },
    var.tags
  )
}

resource "azapi_resource" "resource_group" {
  count = var.create_resource_group ? 1 : 0

  type      = "Microsoft.Resources/resourceGroups@2024-03-01"
  name      = var.resource_group_name
  parent_id = "/subscriptions/${var.subscription_id}"
  location  = var.location
  tags      = local.common_tags
}

# There is no native AzureRM resource for Web IQ yet, so AzAPI calls the
# Microsoft.WebIQ resource provider directly.
resource "azapi_resource" "webiq_account" {
  type      = "Microsoft.WebIQ/accounts@${local.webiq_api_version}"
  name      = var.account_name
  parent_id = local.resource_group_id
  location  = "global"

  body = {
    kind = "WebIQ.Grounding"
    sku = {
      name = "G3"
    }
    properties = {
      statisticsEnabled = false
    }
  }

  # The AzAPI provider's embedded schema doesn't include this preview API version.
  schema_validation_enabled = false
}
