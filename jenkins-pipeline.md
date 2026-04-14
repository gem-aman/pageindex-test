# Jenkins CI/CD Pipeline

This document describes the Jenkins CI/CD pipeline configuration for building, testing, and deploying our platform. Jenkins orchestrates all automation including infrastructure provisioning, database migrations, and application deployment.

## Pipeline Architecture

### Jenkins Server

- Runs on Azure VM (provisioned via Terraform — see [Terraform Modules Guide](terraform-modules.md))
- Controller-Agent architecture with ephemeral agents on AKS — see [Azure Infrastructure Guide](azure-infrastructure.md#azure-kubernetes-service-aks)
- Plugins: Pipeline, Vault, Kubernetes, Docker, Slack

### Vault Authentication

Jenkins authenticates to Vault using the AppRole method:

```groovy
def vaultConfig = [
    vaultUrl: 'https://vault.internal.example.com',
    engineVersion: 2
]

def secrets = [
    [path: 'secret/data/platform/${env}/azure-sql', secretValues: [
        [envVar: 'SQL_USER', vaultKey: 'username'],
        [envVar: 'SQL_PASSWORD', vaultKey: 'password']
    ]],
    [path: 'secret/data/terraform/service-principal', secretValues: [
        [envVar: 'ARM_CLIENT_ID', vaultKey: 'client_id'],
        [envVar: 'ARM_CLIENT_SECRET', vaultKey: 'client_secret'],
        [envVar: 'ARM_TENANT_ID', vaultKey: 'tenant_id'],
        [envVar: 'ARM_SUBSCRIPTION_ID', vaultKey: 'subscription_id']
    ]]
]
```

The Vault policies governing Jenkins access are defined in [Vault Secrets Management](vault-secrets.md#jenkins-policy). The AppRole credentials are documented in [Vault Secrets Management](vault-secrets.md#access-methods).

## Main Pipeline (Jenkinsfile)

```groovy
pipeline {
    agent { kubernetes { yaml agentPod() } }
    
    parameters {
        choice(name: 'ENVIRONMENT', choices: ['dev', 'staging', 'prod'])
        booleanParam(name: 'SKIP_TESTS', defaultValue: false)
        booleanParam(name: 'TERRAFORM_APPLY', defaultValue: false)
    }
    
    stages {
        stage('Checkout')           { steps { checkout scm } }
        stage('Terraform Plan')     { steps { terraformPlan() } }
        stage('Terraform Apply')    { when { expression { params.TERRAFORM_APPLY } } steps { terraformApply() } }
        stage('Build & Test')       { steps { buildAndTest() } }
        stage('Docker Build')       { steps { dockerBuild() } }
        stage('Docker Push')        { steps { dockerPush() } }
        stage('Flyway Migrate')     { steps { flywayMigrate() } }
        stage('Deploy to K8s')      { steps { kubernetesDeploy() } }
        stage('Smoke Tests')        { steps { smokeTests() } }
    }
    
    post {
        success { slackSend(channel: '#deployments', message: "✅ Deploy ${env} succeeded") }
        failure { slackSend(channel: '#deployments', message: "❌ Deploy ${env} failed") }
    }
}
```

## Terraform Plan and Apply

### Plan Stage

Runs on every pull request to show infrastructure changes:

```groovy
def terraformPlan() {
    withVaultSecrets(secrets) {
        sh '''
            cd terraform/
            terraform init \
                -backend-config="access_key=${TF_STATE_ACCESS_KEY}"
            terraform plan \
                -var="environment=${ENVIRONMENT}" \
                -out=tfplan
        '''
        archiveArtifacts artifacts: 'terraform/tfplan'
    }
}
```

The Terraform modules being planned are documented in [Terraform Modules Guide](terraform-modules.md). Backend configuration secrets come from [Vault Secrets Management](vault-secrets.md#terraform-state-backend).

### Apply Stage

Runs only on merge to main with explicit approval:

```groovy
def terraformApply() {
    timeout(time: 30, unit: 'MINUTES') {
        input message: "Apply Terraform changes to ${ENVIRONMENT}?"
    }
    withVaultSecrets(secrets) {
        sh '''
            cd terraform/
            terraform apply tfplan
        '''
    }
}
```

After Terraform apply, the AKS kubeconfig is stored in Vault — see [Vault Secrets Management](vault-secrets.md#kubeconfig-storage).

## Docker Build and Push

Builds Docker images and pushes to ACR — see [Azure Infrastructure Guide](azure-infrastructure.md#azure-container-registry-acr).

```groovy
def dockerBuild() {
    sh '''
        docker build -t ${ACR_LOGIN_SERVER}/app:${BUILD_NUMBER} .
        docker build -t ${ACR_LOGIN_SERVER}/flyway-runner:${BUILD_NUMBER} -f Dockerfile.flyway .
    '''
}

def dockerPush() {
    withVaultSecrets([
        [path: 'secret/data/platform/${ENVIRONMENT}/acr', secretValues: [
            [envVar: 'ACR_TOKEN', vaultKey: 'token'],
            [envVar: 'ACR_LOGIN_SERVER', vaultKey: 'login_server']
        ]]
    ]) {
        sh '''
            echo ${ACR_TOKEN} | docker login ${ACR_LOGIN_SERVER} --username 00000000-0000-0000-0000-000000000000 --password-stdin
            docker push ${ACR_LOGIN_SERVER}/app:${BUILD_NUMBER}
            docker push ${ACR_LOGIN_SERVER}/flyway-runner:${BUILD_NUMBER}
        '''
    }
}
```

ACR credentials are managed in [Vault Secrets Management](vault-secrets.md#acr-credentials). The ACR itself is provisioned by Terraform — [Terraform Modules Guide](terraform-modules.md#acr-module).

## Flyway Migration Stage

Runs database migrations before application deployment. See [Flyway Database Migrations](flyway-migrations.md) for full details.

```groovy
def flywayMigrate() {
    withVaultSecrets([
        [path: "secret/data/platform/${ENVIRONMENT}/azure-sql", secretValues: [
            [envVar: 'FLYWAY_USER', vaultKey: 'username'],
            [envVar: 'FLYWAY_PASSWORD', vaultKey: 'password']
        ]]
    ]) {
        sh '''
            flyway -configFiles=db/conf/flyway-${ENVIRONMENT}.conf info
            flyway -configFiles=db/conf/flyway-${ENVIRONMENT}.conf validate
            flyway -configFiles=db/conf/flyway-${ENVIRONMENT}.conf migrate
            flyway -configFiles=db/conf/flyway-${ENVIRONMENT}.conf info
        '''
    }
}
```

For production, dynamic Vault credentials are used instead — see [Flyway Database Migrations](flyway-migrations.md#vault-dynamic-credentials) and [Vault Secrets Management](vault-secrets.md#database-secrets-engine).

## Environment Variable Injection

Application environment variables are injected during deployment:

```groovy
def injectEnvVars() {
    withVaultSecrets([
        [path: "secret/data/platform/${ENVIRONMENT}/app-secrets/db-connection", secretValues: [
            [envVar: 'DB_CONNECTION_STRING', vaultKey: 'connection_string']
        ]],
        [path: "secret/data/platform/${ENVIRONMENT}/app-secrets/api-keys", secretValues: [
            [envVar: 'EXTERNAL_API_KEY', vaultKey: 'key']
        ]]
    ]) {
        // Variables available to deployment steps
    }
}
```

Connection strings follow the format defined in [Azure Infrastructure Guide](azure-infrastructure.md#connection-strings). Secrets come from [Vault Secrets Management](vault-secrets.md#specific-secret-paths).

## Kubernetes Deployment

Deploys application to AKS using kubectl:

```groovy
def kubernetesDeploy() {
    withVaultSecrets([
        [path: "secret/data/platform/${ENVIRONMENT}/kubeconfig", secretValues: [
            [envVar: 'KUBECONFIG_DATA', vaultKey: 'kubeconfig']
        ]]
    ]) {
        sh '''
            echo "${KUBECONFIG_DATA}" | base64 -d > /tmp/kubeconfig
            export KUBECONFIG=/tmp/kubeconfig
            
            kubectl set image deployment/app \
                app=${ACR_LOGIN_SERVER}/app:${BUILD_NUMBER} \
                -n platform-${ENVIRONMENT}
            
            kubectl rollout status deployment/app \
                -n platform-${ENVIRONMENT} \
                --timeout=300s
        '''
    }
}
```

The AKS cluster is provisioned by Terraform — [Terraform Modules Guide](terraform-modules.md#aks-cluster-module). Kubeconfig is stored in Vault — [Vault Secrets Management](vault-secrets.md#kubeconfig-storage).

## Vault Sync Job

A scheduled Jenkins job that synchronizes secrets from HashiCorp Vault to Azure Key Vault:

```groovy
pipeline {
    triggers { cron('H/15 * * * *') }  // Every 15 minutes
    
    stages {
        stage('Sync Secrets') {
            steps {
                withVaultSecrets(allSecrets) {
                    sh '''
                        python3 scripts/vault_to_azure_kv_sync.py \
                            --environment ${ENVIRONMENT} \
                            --verify-checksums
                    '''
                }
            }
        }
    }
}
```

The sync process is detailed in [Vault Secrets Management](vault-secrets.md#vault-to-azure-keyvault-sync). The Azure Key Vault target is provisioned by Terraform — [Terraform Modules Guide](terraform-modules.md#key-vault-module).

## Emergency Rollback

### Application Rollback

```groovy
def rollbackApplication() {
    withVaultSecrets(kubeconfigSecrets) {
        sh '''
            kubectl rollout undo deployment/app \
                -n platform-${ENVIRONMENT}
            kubectl rollout status deployment/app \
                -n platform-${ENVIRONMENT} \
                --timeout=300s
        '''
    }
}
```

### Database Rollback

```groovy
def rollbackDatabase() {
    withVaultSecrets(sqlSecrets) {
        sh '''
            flyway -configFiles=db/conf/flyway-${ENVIRONMENT}.conf undo
        '''
    }
}
```

See [Flyway Database Migrations](flyway-migrations.md#rollback-strategy) for the undo migration approach.

## Disaster Recovery Deployment

For DR scenarios, a dedicated pipeline deploys to the secondary region:

```groovy
def disasterRecoveryDeploy() {
    // Failover SQL
    sh 'az sql failover-group set-primary --resource-group rg-platform-dr ...'
    
    // Deploy to DR AKS cluster
    withVaultSecrets(drKubeconfigSecrets) {
        kubernetesDeploy()
    }
    
    // Update Traffic Manager
    sh 'az network traffic-manager endpoint update ...'
}
```

DR infrastructure is managed by Terraform — see [Terraform Modules Guide](terraform-modules.md#disaster-recovery-module). Azure DR setup details in [Azure Infrastructure Guide](azure-infrastructure.md#disaster-recovery).

## Pipeline Maintenance

### Adding New Secrets

When a new secret is needed:

1. Add secret to Vault — see [Vault Secrets Management](vault-secrets.md)
2. Update Vault policy if needed — see [Vault Secrets Management](vault-secrets.md#vault-policies)
3. Add to Jenkins pipeline `withVaultSecrets` block
4. If needed for AKS, add to sync job and Key Vault — see [Vault Secrets Management](vault-secrets.md#vault-to-azure-keyvault-sync)
5. Update Terraform Key Vault access policies — see [Terraform Modules Guide](terraform-modules.md#key-vault-module)

### Troubleshooting

1. **Vault authentication failed**: Check AppRole credentials and policy — [Vault Secrets Management](vault-secrets.md#vault-authentication)
2. **Terraform state lock**: Another pipeline may be running; check Azure Storage — [Terraform Modules Guide](terraform-modules.md#backend-configuration)
3. **Docker push failed**: Verify ACR credentials in Vault — [Vault Secrets Management](vault-secrets.md#acr-credentials)
4. **Flyway migration failed**: Check migration SQL and database connectivity — [Flyway Database Migrations](flyway-migrations.md#troubleshooting)
5. **K8s deployment timeout**: Check AKS cluster health — [Azure Infrastructure Guide](azure-infrastructure.md#azure-kubernetes-service-aks)
