# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

variable "subscription_id" {
  description = "Azure subscription where the sample resources are deployed."
  type        = string
}

variable "location" {
  description = "Azure region for the sample resources."
  type        = string
  default     = "eastus2"
}

variable "resource_group_name" {
  description = "Optional resource group name. A generated name is used when omitted."
  type        = string
  default     = null
  nullable    = true
}

variable "name_prefix" {
  description = "Short prefix used for generated resource names."
  type        = string
  default     = "webiq"

  validation {
    condition     = can(regex("^[a-z0-9-]{2,20}$", var.name_prefix))
    error_message = "name_prefix must contain 2-20 lowercase letters, numbers, or hyphens."
  }
}

variable "publisher_name" {
  description = "API Management publisher name."
  type        = string
}

variable "publisher_email" {
  description = "API Management publisher email."
  type        = string
}

variable "apim_sku_name" {
  description = "API Management SKU. Developer_1 is suitable only for a sample environment."
  type        = string
  default     = "Developer_1"
}

variable "entra_tenant_id" {
  description = "Microsoft Entra tenant that issues frontend user access tokens."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", var.entra_tenant_id))
    error_message = "entra_tenant_id must be a single tenant's GUID, not common or organizations."
  }
}

variable "api_allowed_client_ids" {
  description = "Application (client) IDs of approved frontend clients. This is separate from the gateway API's audience."
  type        = set(string)

  validation {
    condition = length(var.api_allowed_client_ids) > 0 && alltrue([
      for id in var.api_allowed_client_ids :
      can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", id))
    ])
    error_message = "api_allowed_client_ids must contain at least one approved client's GUID."
  }
}

variable "api_audience" {
  description = "Expected audience in frontend user access tokens, such as api://<application-client-id>."
  type        = string
}

variable "api_required_scope" {
  description = "Delegated scope required in the frontend user's scp claim, such as access_as_user."
  type        = string
}

variable "enable_telemetry" {
  description = "Opt in to API-level Application Insights request, dependency, error, and custom trace telemetry."
  type        = bool
  default     = false
}

variable "webiq_base_url" {
  description = "Web IQ API base URL."
  type        = string
  default     = "https://api.microsoft.ai/v3"
}

variable "webiq_token_resource" {
  description = "Microsoft Entra resource used by APIM to obtain a managed identity token for Web IQ."
  type        = string
  default     = "https://api.microsoft.ai"
}

variable "logging_sampling_percentage" {
  description = "Percentage of API requests sampled by Application Insights."
  type        = number
  default     = 100

  validation {
    condition     = var.logging_sampling_percentage >= 0 && var.logging_sampling_percentage <= 100
    error_message = "logging_sampling_percentage must be between 0 and 100."
  }
}

variable "log_retention_days" {
  description = "Log Analytics retention period."
  type        = number
  default     = 30
}

variable "tags" {
  description = "Additional tags applied to the sample resources."
  type        = map(string)
  default     = {}
}
