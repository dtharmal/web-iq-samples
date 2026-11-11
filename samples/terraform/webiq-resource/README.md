# Web IQ Resource

Create a Microsoft Web IQ resource (`Microsoft.WebIQ/accounts`) in your Azure
subscription with Terraform. The resource is where Web IQ usage is billed, and
it's what you connect in the Web IQ portal to get a Web IQ AppID and API keys.
The sample uses the same resource definition as
[Deploy Microsoft Web IQ with the Azure CLI](https://learn.microsoft.com/en-us/web-iq/get-started/deploy-azure-cli).

There is no native AzureRM resource for Web IQ yet, so the sample uses the
[AzAPI provider](https://registry.terraform.io/providers/Azure/azapi/latest/docs),
which calls the Azure Resource Manager API directly. The Web IQ API version is a
preview version that isn't in the AzAPI provider's embedded schema, so schema
validation is turned off for this resource.

This is an educational sample. Terraform creates only the Azure resource; it
doesn't create the Web IQ AppID or API keys, and it never reads or outputs keys.

## Prerequisites

- Terraform 1.9 or later and the Azure CLI
- A paid Azure subscription where you can create resources. Azure credits don't work for Web IQ.
- Web IQ enabled for the subscription. If it isn't, see [Troubleshooting](#web-iq-is-not-enabled-for-your-subscription).
- The `Microsoft.WebIQ` resource provider registered in the subscription; see [Register the resource provider](#register-the-resource-provider)


## Get started

Run all commands from this sample's directory.

### Windows (PowerShell)

```powershell
Copy-Item terraform.tfvars.example terraform.tfvars
az login
az account set --subscription "<azure-subscription-id>"
```

### macOS/Linux (Bash)

```bash
cp terraform.tfvars.example terraform.tfvars
az login
az account set --subscription "<azure-subscription-id>"
```

### Register the resource provider

Register the `Microsoft.WebIQ` resource provider once per subscription. The
command waits until the provider is registered. It works in PowerShell and Bash:

```text
az provider register --namespace Microsoft.WebIQ --wait
```

The sample doesn't register resource providers itself: the AzAPI provider is
configured with `skip_provider_registration = true`.

### Configure and deploy

Replace every angle-bracket placeholder in `terraform.tfvars`: the subscription
ID, the resource group, and a globally unique name for the Web IQ resource.

By default the resource group must already exist, and Terraform manages only
the Web IQ resource. Set `create_resource_group = true` to have Terraform create
the resource group in `location` as well. The Web IQ resource always uses the
`global` location.

Keep configured variables, state, and saved plans out of source control; the
repository ignores them.

Run in either shell:

```text
terraform init -input=false -lockfile=readonly
terraform plan -out main.tfplan
terraform apply main.tfplan
```

Then get the resource ID:

```text
terraform output -raw webiq_resource_id
```

## Connect the resource in the Web IQ portal

Terraform doesn't cover this step. Sign up or sign in to the
[Web IQ portal](https://webiq.microsoft.ai/profiles) with the account that owns
the Azure subscription, connect your resource ID, and create the API keys. The
portal creates a Web IQ AppID for your resource. The AppID starts with a default
rate limit of 15 queries per second (QPS); your current limit is shown in the
Web IQ portal.

Don't put API keys in `terraform.tfvars`, Terraform variables, outputs, or
state. Store them in a secret manager and read them from there in your
application; see [Authentication](https://learn.microsoft.com/en-us/web-iq/get-started/authentication)
and the [Quickstart](https://learn.microsoft.com/en-us/web-iq/get-started/quickstart).

Your use of Web IQ is subject to the
[Web IQ Terms of Use](https://www.microsoft.com/en-us/webiq/webiq-legal). Please
review them before you begin.

## Troubleshooting

If `terraform apply` fails, read the full error text it returns, because it
usually names the exact cause.

### Web IQ is not enabled for your subscription

Web IQ must be enabled for your Azure subscription before you can deploy the
resource. If it isn't, the deployment fails with an error stating that the
feature is not enabled for the selected subscription. Contact your Microsoft
account representative or sales executive to request access, and then run
`terraform apply` again.

### The resource provider isn't registered

Run the command in [Register the resource provider](#register-the-resource-provider),
and then run `terraform apply` again.

### The API version is not valid

The sample uses API version `2026-06-01-preview`, set in `webiq_api_version` in
`main.tf`. If the provider doesn't support that version, the deployment fails,
and Azure usually lists the supported versions in the error message. To see
which versions the provider supports, run:

```text
az provider show --namespace Microsoft.WebIQ --query "resourceTypes[?resourceType=='accounts'].apiVersions" --output tsv
```

## Validation

Run in either shell without provisioning Azure resources:

```text
terraform fmt -check -recursive
terraform init -backend=false -input=false -lockfile=readonly
terraform validate
terraform test
```

Tests use a mocked AzAPI provider. They check the resource type, API version,
location, and request body against the Azure CLI deployment guide, the resource
group behavior, input validation, and that outputs hold only identifiers. They
don't prove that the deployment succeeds in your subscription.

## Clean up

Run `terraform destroy` in either shell. This deletes the Web IQ resource. If
you set `create_resource_group = true`, it also deletes the resource group and
**all its resources**; do not use that setting with an existing or shared
resource group.


## Further guidance

- [Deploy Microsoft Web IQ with the Azure CLI](https://learn.microsoft.com/en-us/web-iq/get-started/deploy-azure-cli): the same deployment with Azure CLI commands, plus how to check the resource's billing cost.
- [Deploy Microsoft Web IQ with the Azure portal](https://learn.microsoft.com/en-us/web-iq/get-started/deploy-azure-portal): the same deployment in the browser, including the Web IQ portal steps.
- [Quickstart](https://learn.microsoft.com/en-us/web-iq/get-started/quickstart): make your first Web IQ API calls with your API key.
