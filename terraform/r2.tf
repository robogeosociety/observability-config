# The influxdb-backups bucket already exists (created via dashboard/MCP), so it is
# imported into state rather than recreated. Remove the import block after the
# first successful apply.
resource "cloudflare_r2_bucket" "influxdb_backups" {
  account_id = var.cloudflare_account_id
  name       = var.bucket_name
}

import {
  to = cloudflare_r2_bucket.influxdb_backups
  id = "${var.cloudflare_account_id}/${var.bucket_name}"
}

# Scoped R2 S3-API token: Object Read & Write on ONLY the influxdb-backups bucket.
# Cloudflare derives the S3 credentials from this token:
#   access_key_id     = token id
#   secret_access_key = sha256(token value)   (see outputs.tf)
resource "cloudflare_api_token" "influxdb_backups_rw" {
  name = "influxdb-backups-rw"

  policies = [{
    effect = "allow"
    # Account-level R2 permission groups (Read + Write)...
    permission_groups = [
      { id = "b4992e1108244f5d8bdbd7373ae9a774" }, # Workers R2 Storage Read
      { id = "bf7481a1826f439697cb59a20b22293e" }, # Workers R2 Storage Write
    ]
    # ...scoped to a single bucket (jurisdiction "default"). If your account
    # rejects bucket-level scoping, fall back to account scope:
    #   "com.cloudflare.api.account.${var.cloudflare_account_id}" = "*"
    resources = {
      "com.cloudflare.edge.r2.bucket.${var.cloudflare_account_id}_default_${var.bucket_name}" = "*"
    }
  }]
}
