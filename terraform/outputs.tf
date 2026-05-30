output "r2_account_id" {
  value = var.cloudflare_account_id
}

output "r2_bucket" {
  value = var.bucket_name
}

output "r2_endpoint" {
  description = "S3-compatible endpoint for boto3 / r2_upload.py."
  value       = "https://${var.cloudflare_account_id}.r2.cloudflarestorage.com"
}

output "r2_access_key_id" {
  description = "R2 S3 access key id (the API token id)."
  value       = cloudflare_api_token.influxdb_backups_rw.id
  sensitive   = true
}

output "r2_secret_access_key" {
  description = "R2 S3 secret access key — sha256 of the API token value (Cloudflare's R2 convention)."
  value       = sha256(cloudflare_api_token.influxdb_backups_rw.value)
  sensitive   = true
}
