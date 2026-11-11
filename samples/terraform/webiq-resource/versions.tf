# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

terraform {
  required_version = ">= 1.9.0"

  required_providers {
    azapi = {
      source  = "Azure/azapi"
      version = "~> 2.0"
    }
  }
}

provider "azapi" {
  subscription_id = var.subscription_id

  # Register Microsoft.WebIQ once per subscription with the Azure CLI instead.
  skip_provider_registration = true
}
