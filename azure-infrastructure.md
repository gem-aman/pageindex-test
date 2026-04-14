# Azure Infrastructure Guide

This document covers the Azure infrastructure setup for our platform. All Azure resources are provisioned using Terraform — see [Terraform Modules Guide](terraform-modules.md) for the IaC approach.

## Resource Groups

All resources are organized into resource groups per environment:

- `rg-platform-dev` — Development environment
- `rg-platform-staging` — Staging environment
- `rg-platform-prod` — Production environment

Resource groups are created via the `resource_group` Terraform module documented in [Terraform Modules Guide](terraform-modules.md#resource-group-module).

## Azure SQL Database

We use Azure SQL Database for relational data storage. Database migrations are handled by Flyway — see [Flyway Database Migrations](flyway-migrations.md) for details.

### SQL Server Configuration

- SKU: `GP_Gen5_2` (General Purpose, Gen5, 2 vCores)
- Max size: 32 GB
- Backup retention: 7 days (dev), 35 days (prod)
- TDE (Transparent Data Encryption): Enabled
- Admin credentials are stored in Azure Key Vault — see [Vault Secrets Management](vault-secrets.md#azure-sql-credentials)

### Connection Strings

Connection strings follow this pattern:

```
Server=tcp:{server_name}.database.windows.net,1433;Database={db_name};Authentication=Active Directory Default;
```

For applications, connection strings are injected via environment variables managed through the deployment pipeline — see [Jenkins CI/CD Pipeline](jenkins-pipeline.md#environment-variable-injection).

## Azure Kubernetes Service (AKS)

Our applications run on AKS. The cluster is provisioned via Terraform — see [Terraform Modules Guide](terraform-modules.md#aks-cluster-module).

### Cluster Specifications

| Environment | Node Count | VM Size | Max Pods |
|------------|-----------|---------|----------|
| Dev | 2 | Standard_D2s_v3 | 30 |
| Staging | 3 | Standard_D4s_v3 | 50 |
| Prod | 5 | Standard_D8s_v3 | 110 |

### Networking

- CNI: Azure CNI
- Network Policy: Calico
- Ingress Controller: NGINX (deployed via Helm)
- Private cluster: Yes (prod only)
- VNet integration details in [Terraform Modules Guide](terraform-modules.md#networking-module)

### Secrets Integration

AKS pods access secrets through the CSI Secrets Store Driver, which syncs secrets from Azure Key Vault. The Key Vault is managed by Terraform and secrets are populated by our Vault-to-Azure sync process — see [Vault Secrets Management](vault-secrets.md#vault-to-azure-keyvault-sync).

## Azure Container Registry (ACR)

Docker images are stored in ACR. The CI pipeline builds and pushes images — see [Jenkins CI/CD Pipeline](jenkins-pipeline.md#docker-build-and-push).

### ACR Configuration

- SKU: Premium
- Geo-replication: Enabled (East US, West Europe)
- Content Trust: Enabled
- Admin user: Disabled (use managed identity)
- Provisioned via [Terraform Modules Guide](terraform-modules.md#acr-module)

## Azure Key Vault

Key Vault stores application secrets, certificates, and encryption keys.

### Access Policies

Access is granted via Azure RBAC:
- AKS Managed Identity: `Key Vault Secrets User`
- Jenkins Service Principal: `Key Vault Secrets Officer`
- Terraform Service Principal: `Key Vault Administrator`

Secrets are synced from HashiCorp Vault — see [Vault Secrets Management](vault-secrets.md) for the synchronization mechanism.

## Monitoring and Alerting

### Azure Monitor

- Log Analytics Workspace configured per environment
- Container Insights enabled on AKS
- Application Insights for application-level telemetry

### Alert Rules

Critical alerts are configured for:
- AKS node CPU > 80%
- SQL Database DTU > 90%
- Key Vault throttling events
- ACR storage > 80% capacity

Alert notifications go through Azure Action Groups connected to PagerDuty. Alert infrastructure is managed via Terraform — see [Terraform Modules Guide](terraform-modules.md#monitoring-module).

## Disaster Recovery

### Backup Strategy

- Azure SQL: Geo-redundant backups with point-in-time restore
- AKS: Velero backups to Azure Blob Storage
- Key Vault: Soft delete + purge protection (90 days)

### Failover Procedures

1. Database failover is automatic via SQL auto-failover groups
2. AKS workloads are redeployed via Jenkins — see [Jenkins CI/CD Pipeline](jenkins-pipeline.md#disaster-recovery-deployment)
3. DNS failover managed by Azure Traffic Manager

For the Terraform configuration of DR resources, see [Terraform Modules Guide](terraform-modules.md#disaster-recovery-module).
