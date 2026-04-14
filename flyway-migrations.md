# Flyway Database Migrations

This document covers how database schema migrations are managed using Flyway across all environments. For the database infrastructure setup, see [Azure Infrastructure Guide](azure-infrastructure.md#azure-sql-database).

## Overview

Flyway manages schema versioning for our Azure SQL databases. Migrations are:
- Version-controlled in Git alongside application code
- Executed automatically via the Jenkins pipeline — see [Jenkins CI/CD Pipeline](jenkins-pipeline.md#flyway-migration-stage)
- Authenticated using credentials from Vault — see [Vault Secrets Management](vault-secrets.md#azure-sql-credentials)

## Project Structure

```
db/
├── flyway.conf                    # Base configuration
├── conf/
│   ├── flyway-dev.conf           # Dev overrides
│   ├── flyway-staging.conf       # Staging overrides
│   └── flyway-prod.conf         # Prod overrides
├── sql/
│   ├── V1.0__create_users_table.sql
│   ├── V1.1__add_email_index.sql
│   ├── V2.0__create_orders_table.sql
│   ├── V2.1__add_order_status.sql
│   └── V3.0__create_audit_log.sql
└── callbacks/
    ├── beforeMigrate.sql         # Pre-migration checks
    └── afterMigrate.sql          # Post-migration validation
```

## Database Connection

### Static Credentials

For development and testing:

```properties
# flyway-dev.conf
flyway.url=jdbc:sqlserver://sql-platform-dev.database.windows.net:1433;database=app_db;encrypt=true;
flyway.user=${FLYWAY_USER}
flyway.password=${FLYWAY_PASSWORD}
```

Credentials are fetched from Vault at pipeline runtime — see [Vault Secrets Management](vault-secrets.md#azure-sql-credentials).

### Vault Dynamic Credentials

For production, Flyway uses Vault's Database Secrets Engine to obtain short-lived credentials:

```bash
# Fetched by Jenkins at runtime
CREDS=$(vault read -format=json database/creds/flyway-migration)
export FLYWAY_USER=$(echo $CREDS | jq -r '.data.username')
export FLYWAY_PASSWORD=$(echo $CREDS | jq -r '.data.password')
```

The Vault database role is configured in [Vault Secrets Management](vault-secrets.md#database-secrets-engine). The Jenkins pipeline handles this automatically — see [Jenkins CI/CD Pipeline](jenkins-pipeline.md#flyway-migration-stage).

## Migration Naming Conventions

### Versioned Migrations

Format: `V{major}.{minor}__{description}.sql`

- **Major version**: Breaking schema changes
- **Minor version**: Additive changes

Examples:
```
V1.0__create_users_table.sql
V1.1__add_email_column.sql
V2.0__create_orders_table.sql
```

### Repeatable Migrations

Format: `R__{description}.sql`

Used for views, stored procedures, and reference data:
```
R__create_user_summary_view.sql
R__refresh_lookup_data.sql
```

## Running Migrations

### Local Development

```bash
# Using Flyway CLI
flyway -configFiles=conf/flyway-dev.conf migrate

# Using Docker
docker run --rm \
  -v $(pwd)/sql:/flyway/sql \
  -v $(pwd)/conf:/flyway/conf \
  flyway/flyway:10 \
  -configFiles=/flyway/conf/flyway-dev.conf \
  migrate
```

### CI/CD Pipeline

Migrations run as part of the Jenkins pipeline — see [Jenkins CI/CD Pipeline](jenkins-pipeline.md#flyway-migration-stage).

The pipeline stages:
1. `flyway info` — Show migration status
2. `flyway validate` — Validate pending migrations
3. `flyway migrate` — Apply pending migrations
4. `flyway info` — Confirm final state

### Running Migrations in Kubernetes

For environments where direct database access is restricted, migrations run as Kubernetes Jobs:

```yaml
apiVersion: batch/v1
kind: Job
metadata:
  name: flyway-migration
spec:
  template:
    spec:
      containers:
      - name: flyway
        image: acrplatformprod.azurecr.io/flyway-runner:latest
        env:
        - name: FLYWAY_URL
          valueFrom:
            secretKeyRef:
              name: db-connection
              key: jdbc-url
        - name: FLYWAY_USER
          valueFrom:
            secretKeyRef:
              name: db-credentials
              key: username
        - name: FLYWAY_PASSWORD
          valueFrom:
            secretKeyRef:
              name: db-credentials
              key: password
      restartPolicy: Never
```

The Kubernetes secrets referenced above are synced from Vault — see [Vault Secrets Management](vault-secrets.md#vault-to-azure-keyvault-sync). The AKS cluster where this runs is documented in [Azure Infrastructure Guide](azure-infrastructure.md#azure-kubernetes-service-aks).

The Flyway Docker image is built and pushed via Jenkins — see [Jenkins CI/CD Pipeline](jenkins-pipeline.md#docker-build-and-push). The ACR registry is provisioned by Terraform — see [Terraform Modules Guide](terraform-modules.md#acr-module).

## Rollback Strategy

### Undo Migrations

We maintain undo migrations for critical schema changes:

```
U2.0__drop_orders_table.sql   # Undo for V2.0
U3.0__drop_audit_log.sql      # Undo for V3.0
```

### Emergency Rollback Procedure

1. Jenkins triggers rollback job — see [Jenkins CI/CD Pipeline](jenkins-pipeline.md#emergency-rollback)
2. Flyway executes undo migration
3. Application rolled back to previous version
4. Database point-in-time restore available as last resort — see [Azure Infrastructure Guide](azure-infrastructure.md#disaster-recovery)

## Schema Validation

### Pre-Migration Checks

The `beforeMigrate.sql` callback validates:
- Database connectivity
- Current schema version
- Sufficient permissions

### Post-Migration Validation

The `afterMigrate.sql` callback:
- Verifies all expected tables exist
- Checks index integrity
- Validates foreign key constraints
- Reports to monitoring — see [Azure Infrastructure Guide](azure-infrastructure.md#monitoring-and-alerting)

## Environment-Specific Configuration

### Dev Environment

```properties
flyway.url=jdbc:sqlserver://sql-platform-dev.database.windows.net:1433;database=app_db
flyway.cleanDisabled=false
flyway.baselineOnMigrate=true
```

Database provisioned by Terraform — [Terraform Modules Guide](terraform-modules.md#sql-database-module).

### Staging Environment

```properties
flyway.url=jdbc:sqlserver://sql-platform-staging.database.windows.net:1433;database=app_db
flyway.cleanDisabled=true
flyway.outOfOrder=false
```

### Production Environment

```properties
flyway.url=jdbc:sqlserver://sql-platform-prod.database.windows.net:1433;database=app_db
flyway.cleanDisabled=true
flyway.outOfOrder=false
flyway.validateMigrationNaming=true
```

Production credentials are fetched dynamically — [Vault Secrets Management](vault-secrets.md#vault-dynamic-credentials).

## Troubleshooting

### Common Issues

1. **Migration checksum mismatch**: Never modify applied migrations; create a new versioned migration instead
2. **Connection timeout**: Check VNet rules and private endpoints — [Azure Infrastructure Guide](azure-infrastructure.md#azure-sql-database) and [Terraform Modules Guide](terraform-modules.md#networking-module)
3. **Permission denied**: Verify Vault credentials are valid — [Vault Secrets Management](vault-secrets.md#azure-sql-credentials)
4. **Out of order migration**: Disabled in staging/prod; fix in dev first
5. **Pipeline failure**: Check Jenkins logs — [Jenkins CI/CD Pipeline](jenkins-pipeline.md#flyway-migration-stage)
