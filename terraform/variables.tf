variable "cloudflare_api_token" {
  description = "Cloudflare API token with permission to manage R2 + create API tokens. Supply via TF_VAR_cloudflare_api_token; never commit it."
  type        = string
  sensitive   = true
}

variable "cloudflare_account_id" {
  description = "Cloudflare account ID (not secret — it's in the public R2 endpoint URL)."
  type        = string
  default     = "d7adee58513c1b2f770ccaac90cf114f"
}

variable "bucket_name" {
  description = "R2 bucket for InfluxDB backups."
  type        = string
  default     = "influxdb-backups"
}

variable "campsite_bucket_name" {
  description = "R2 bucket the campsite collector Worker writes summaries to (read-only ingest)."
  type        = string
  default     = "campsite-raw"
}
