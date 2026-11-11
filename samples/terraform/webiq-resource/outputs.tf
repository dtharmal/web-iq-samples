# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

output "webiq_resource_id" {
  description = "Azure resource ID of the Web IQ resource. Connect it in the Web IQ portal to get a Web IQ AppID."
  value       = azapi_resource.webiq_account.id
}

output "webiq_account_name" {
  description = "Name of the Web IQ resource."
  value       = azapi_resource.webiq_account.name
}
