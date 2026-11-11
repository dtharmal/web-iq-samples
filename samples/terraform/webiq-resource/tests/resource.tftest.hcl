# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

mock_provider "azapi" {
  mock_resource "azapi_resource" {
    defaults = {
      id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/webiq-test-rg/providers/Microsoft.WebIQ/accounts/webiq-test-account"
    }
  }
}

variables {
  subscription_id     = "00000000-0000-0000-0000-000000000000"
  resource_group_name = "webiq-test-rg"
  account_name        = "webiq-test-account"
}

run "webiq_account_matches_deployment_guide" {
  command = plan

  assert {
    condition     = azapi_resource.webiq_account.type == "Microsoft.WebIQ/accounts@2026-06-01-preview"
    error_message = "The Web IQ resource must use the resource type and API version from the Azure CLI deployment guide."
  }

  assert {
    condition     = azapi_resource.webiq_account.location == "global"
    error_message = "The Web IQ resource must use the global location."
  }

  assert {
    condition = (
      azapi_resource.webiq_account.body.kind == "WebIQ.Grounding" &&
      azapi_resource.webiq_account.body.sku.name == "G3" &&
      azapi_resource.webiq_account.body.properties.statisticsEnabled == false
    )
    error_message = "The request body must match the Azure CLI deployment guide."
  }
}

run "existing_resource_group_is_not_managed" {
  command = plan

  assert {
    condition     = length(azapi_resource.resource_group) == 0
    error_message = "The sample must not create or manage a resource group unless create_resource_group is true."
  }

  assert {
    condition     = azapi_resource.webiq_account.parent_id == "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/webiq-test-rg"
    error_message = "The Web IQ resource must be created in the configured resource group."
  }
}

run "create_resource_group_opt_in" {
  command = plan

  variables {
    create_resource_group = true
  }

  assert {
    condition     = length(azapi_resource.resource_group) == 1 && azapi_resource.resource_group[0].location == "eastus2"
    error_message = "Setting create_resource_group must create the resource group in the configured region."
  }
}

run "outputs_hold_identifiers_only" {
  # Mocked apply: no Azure resources are created.
  command = apply

  assert {
    condition     = output.webiq_resource_id == "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/webiq-test-rg/providers/Microsoft.WebIQ/accounts/webiq-test-account"
    error_message = "webiq_resource_id must be the Azure resource ID that is connected in the Web IQ portal."
  }

  assert {
    condition     = output.webiq_account_name == "webiq-test-account"
    error_message = "webiq_account_name must be the Web IQ resource name."
  }

  assert {
    condition = alltrue([
      for value in [output.webiq_resource_id, output.webiq_account_name] :
      !can(regex("(?i)(apikey|api-key|secret|password|token|key=)", value))
    ])
    error_message = "Outputs must not contain API keys or other secrets."
  }
}

run "reject_placeholder_account_name" {
  command = plan

  variables {
    account_name = "<globally-unique-account-name>"
  }

  expect_failures = [var.account_name]
}

run "reject_invalid_subscription_id" {
  command = plan

  variables {
    subscription_id = "<azure-subscription-id>"
  }

  expect_failures = [var.subscription_id]
}
