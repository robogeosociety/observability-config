# Terraform for the observability stack's Cloudflare R2 backup credentials.
#
# Creates a scoped R2 S3-API token (Object Read & Write on ONLY the
# influxdb-backups bucket) so the InfluxDB backup is reproducible instead of a
# hand-pasted token. The token's S3 credentials are exposed as (sensitive)
# outputs to drop into influxdb/.env.
#
# Apply (the API token is supplied at apply time, never committed):
#   export TF_VAR_cloudflare_api_token=<CF API token with R2 admin perms>
#   terraform init && terraform plan && terraform apply
#   # then: terraform output -raw r2_access_key_id / r2_secret_access_key
terraform {
  required_version = ">= 1.5"
  required_providers {
    cloudflare = {
      source  = "cloudflare/cloudflare"
      version = "~> 5.0"
    }
  }
}

provider "cloudflare" {
  api_token = var.cloudflare_api_token
}
