# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

mock_provider "azurerm" {
  mock_resource "azurerm_resource_group" {
    defaults = {
      id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/webiq-test-rg"
    }
  }

  mock_resource "azurerm_log_analytics_workspace" {
    defaults = {
      id = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/webiq-test-rg/providers/Microsoft.OperationalInsights/workspaces/webiq-test-logs"
    }
  }

  mock_resource "azurerm_application_insights" {
    defaults = {
      id                = "/subscriptions/00000000-0000-0000-0000-000000000000/resourceGroups/webiq-test-rg/providers/Microsoft.Insights/components/webiq-test-appi"
      connection_string = "InstrumentationKey=00000000-0000-0000-0000-000000000000;IngestionEndpoint=https://eastus2-0.in.applicationinsights.azure.com/"
    }
  }
}

mock_provider "random" {}

variables {
  subscription_id        = "00000000-0000-0000-0000-000000000000"
  publisher_name         = "Sample publisher"
  publisher_email        = "publisher@example.com"
  entra_tenant_id        = "00000000-0000-0000-0000-000000000001"
  api_audience           = "00000000-0000-0000-0000-000000000002"
  api_required_scope     = "access_as_user"
  api_allowed_client_ids = ["00000000-0000-0000-0000-000000000003"]
}

run "telemetry_disabled_by_default" {
  command = plan

  assert {
    condition     = length(azurerm_api_management_api_diagnostic.webiq) == 0
    error_message = "The sample must not enable request/error/trace collection without explicit opt-in."
  }
}

run "telemetry_opt_in_keeps_bodies_and_credentials_out" {
  command = plan

  variables {
    enable_telemetry = true
  }

  assert {
    condition = alltrue([
      for message in [
        azurerm_api_management_api_diagnostic.webiq[0].frontend_request[0],
        azurerm_api_management_api_diagnostic.webiq[0].frontend_response[0],
        azurerm_api_management_api_diagnostic.webiq[0].backend_request[0],
        azurerm_api_management_api_diagnostic.webiq[0].backend_response[0],
      ] : message.body_bytes == 0 && message.headers_to_log == toset(["content-type"])
    ])
    error_message = "Opting into telemetry must not enable body or credential/header collection."
  }
}

run "reject_empty_client_allowlist" {
  command = plan

  variables {
    api_allowed_client_ids = []
  }

  expect_failures = [var.api_allowed_client_ids]
}

run "reject_invalid_client_identifier" {
  command = plan

  variables {
    api_allowed_client_ids = ["<approved-client-id>"]
  }

  expect_failures = [var.api_allowed_client_ids]
}

run "reject_multitenant_issuer" {
  command = plan

  variables {
    entra_tenant_id = "organizations"
  }

  expect_failures = [var.entra_tenant_id]
}
