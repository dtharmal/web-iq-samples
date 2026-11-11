# Copyright (c) Microsoft Corporation.
# Licensed under the MIT License.

variable "subscription_id" {
  description = "Azure subscription where the Web IQ resource is created. Web IQ requires a paid Azure subscription."
  type        = string

  validation {
    condition     = can(regex("^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$", var.subscription_id))
    error_message = "subscription_id must be the subscription's GUID."
  }
}

variable "resource_group_name" {
  description = "Resource group for the Web IQ resource. It must already exist unless create_resource_group is true."
  type        = string

  validation {
    condition     = length(trimspace(var.resource_group_name)) > 0 && !can(regex("[<>]", var.resource_group_name))
    error_message = "resource_group_name must be set to your resource group's name, not a placeholder."
  }
}

variable "create_resource_group" {
  description = "Create the resource group. When true, terraform destroy deletes the resource group and everything in it."
  type        = bool
  default     = false
}

variable "location" {
  description = "Azure region for the resource group when create_resource_group is true. The Web IQ resource itself uses the global location."
  type        = string
  default     = "eastus2"
}

variable "account_name" {
  description = "Name of the Web IQ resource. It must be globally unique."
  type        = string

  validation {
    condition     = length(trimspace(var.account_name)) > 0 && !can(regex("[<>]", var.account_name))
    error_message = "account_name must be set to a globally unique name, not a placeholder."
  }
}

variable "tags" {
  description = "Additional tags applied to the resource group when create_resource_group is true."
  type        = map(string)
  default     = {}
}
