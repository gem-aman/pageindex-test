# Terraform Modules Guide

This guide documents all Terraform modules used to provision our cloud infrastructure on Azure. For an overview of the Azure resources being managed, see [Azure Infrastructure Guide](azure-infrastructure.md).

## Prerequisites

Before running any Terraform module:

1. Ensure you have Terraform >= 1.5.0 installed
2. Azure CLI authenticated (`az login`)
3. Access to the Terraform state storage account
4. Required secrets available in Vault — see [Vault Secrets Management](vault-secrets.md#terraform-service-principal-credentials)

### Backend Configuration

State is stored in Azure Blob Storage:

```hcl
terraform {
  backend "azurerm" {
    resource_group_name  = "rg-terraform-state"
    storage_account_name = "stterraformstate"
    container_name       = "tfstate"
    key                  = "platform.terraform.tfstate"
  }
}
```

The storage account credentials are managed in Vault — see [Vault Secrets Management](vault-secrets.md#terraform-state-backend).

## Resource Group Module

Creates and configures Azure resource groups with appropriate tags.

```hcl
module "resource_group" {
  source      = "./modules/resource_group"
  name        = "rg-platform-${var.environment}"
  location    = var.location
  tags        = local.common_tags
}
```

### Inputs

| Variable | Type | Description | Default |
|----------|------|-------------|---------|
| name | string | Resource group name | - |
| location | string | Azure region | "eastus" |
| tags | map(string) | Resource tags | {} |

## Networking Module

Provisions VNet, subnets, NSGs, and peering for all environments.

```hcl
module "networking" {
  source              = "./modules/networking"
  resource_group_name = module.resource_group.name
  vnet_address_space  = ["10.0.0.0/16"]
  
  subnets = {
    aks     = { address_prefix = "10.0.1.0/24", nsg_rules = local.aks_nsg_rules }
    sql     = { address_prefix = "10.0.2.0/24", nsg_rules = local.sql_nsg_rules }
    appgw   = { address_prefix = "10.0.3.0/24", nsg_rules = local.appgw_nsg_rules }
  }
}
```

### Private Endpoints

SQL Database and Key Vault use private endpoints to ensure traffic stays within the VNet. See [Azure Infrastructure Guide](azure-infrastructure.md#azure-sql-database) for database specifics.

## AKS Cluster Module

Deploys Azure Kubernetes Service with the configuration specified in [Azure Infrastructure Guide](azure-infrastructure.md#azure-kubernetes-service-aks).

```hcl
module "aks" {
  source              = "./modules/aks"
  resource_group_name = module.resource_group.name
  cluster_name        = "aks-platform-${var.environment}"
  kubernetes_version  = "1.28"
  
  default_node_pool = {
    vm_size    = var.aks_vm_size
    node_count = var.aks_node_count
    max_pods   = var.aks_max_pods
  }
  
  network_profile = {
    network_plugin = "azure"
    network_policy = "calico"
    subnet_id      = module.networking.subnet_ids["aks"]
  }
  
  key_vault_id = module.key_vault.id
}
```

### RBAC Configuration

The AKS module configures RBAC and creates a managed identity that:
- Pulls images from ACR (see [ACR Module](#acr-module))
- Reads secrets from Key Vault (see [Vault Secrets Management](vault-secrets.md#vault-to-azure-keyvault-sync))

### Jenkins Integration

The AKS cluster kubeconfig is stored in Vault for Jenkins access — see [Jenkins CI/CD Pipeline](jenkins-pipeline.md#kubernetes-deployment) and [Vault Secrets Management](vault-secrets.md#kubeconfig-storage).

## ACR Module

Creates Azure Container Registry for Docker image storage.

```hcl
module "acr" {
  source              = "./modules/acr"
  resource_group_name = module.resource_group.name
  name                = "acrplatform${var.environment}"
  sku                 = "Premium"
  
  georeplications = [
    { location = "westeurope", tags = {} }
  ]
}
```

ACR login credentials are stored in Vault for the Jenkins build pipeline — see [Vault Secrets Management](vault-secrets.md#acr-credentials) and [Jenkins CI/CD Pipeline](jenkins-pipeline.md#docker-build-and-push).

## Key Vault Module

Provisions Azure Key Vault for secrets management.

```hcl
module "key_vault" {
  source              = "./modules/key_vault"
  resource_group_name = module.resource_group.name
  name                = "kv-platform-${var.environment}"
  
  access_policies = {
    aks_identity     = { object_id = module.aks.identity_id, permissions = ["get", "list"] }
    jenkins_sp       = { object_id = var.jenkins_sp_object_id, permissions = ["get", "list", "set", "delete"] }
    terraform_sp     = { object_id = var.terraform_sp_object_id, permissions = ["all"] }
  }
}
```

The Key Vault is populated by the Vault sync process — see [Vault Secrets Management](vault-secrets.md#vault-to-azure-keyvault-sync).

## SQL Database Module

Deploys Azure SQL Server and databases. Schema migrations are managed by Flyway — see [Flyway Database Migrations](flyway-migrations.md).

```hcl
module "sql" {
  source              = "./modules/sql"
  resource_group_name = module.resource_group.name
  server_name         = "sql-platform-${var.environment}"
  
  databases = {
    app_db = {
      sku_name = var.sql_sku
      max_size = var.sql_max_size_gb
    }
  }
  
  admin_login    = data.vault_generic_secret.sql_admin.data["username"]
  admin_password = data.vault_generic_secret.sql_admin.data["password"]
  
  subnet_id = module.networking.subnet_ids["sql"]
}
```

Admin credentials come from Vault — see [Vault Secrets Management](vault-secrets.md#azure-sql-credentials).

## Monitoring Module

Deploys Azure Monitor resources as described in [Azure Infrastructure Guide](azure-infrastructure.md#monitoring-and-alerting).

```hcl
module "monitoring" {
  source              = "./modules/monitoring"
  resource_group_name = module.resource_group.name
  
  log_analytics_workspace = {
    retention_days = var.environment == "prod" ? 90 : 30
  }
  
  alert_rules = local.alert_rules
  action_group_id = azurerm_monitor_action_group.pagerduty.id
}
```

## Disaster Recovery Module

Configures DR resources for production failover — see [Azure Infrastructure Guide](azure-infrastructure.md#disaster-recovery).

```hcl
module "disaster_recovery" {
  source              = "./modules/disaster_recovery"
  resource_group_name = module.resource_group.name
  
  sql_failover_group = {
    server_id         = module.sql.server_id
    partner_server_id = module.sql_secondary.server_id
  }
  
  velero_backup = {
    storage_account_id = module.backup_storage.id
    schedule           = "0 2 * * *"
  }
}
```

## CI/CD Integration

Terraform is executed through the Jenkins pipeline — see [Jenkins CI/CD Pipeline](jenkins-pipeline.md#terraform-plan-and-apply). The pipeline:

1. Runs `terraform plan` on pull requests
2. Runs `terraform apply` on merge to main
3. Stores plan output as Jenkins artifacts
4. Notifies via Slack on success/failure

### Required Secrets

All secrets used by Terraform are managed in Vault:
- Azure Service Principal credentials — [Vault Secrets Management](vault-secrets.md#terraform-service-principal-credentials)
- SQL admin password — [Vault Secrets Management](vault-secrets.md#azure-sql-credentials)
- ACR credentials — [Vault Secrets Management](vault-secrets.md#acr-credentials)
