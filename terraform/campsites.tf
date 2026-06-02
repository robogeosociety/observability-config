# Read-only R2 S3-API token for the campsites ingest (campsites/ingest.py).
#
# Scoped to Object **Read** on ONLY the campsite-raw bucket — no write, no other
# buckets. This is the unattended-daemon credential: scoped, and (unlike a
# wrangler OAuth session) long-lived — Cloudflare API tokens stay valid until
# revoked, with no refresh/browser/shared-state to break a launchd job.
#
# The campsite-raw bucket is owned by the collector Worker project
# (robot-geographical-society), so it is referenced by name for scoping but
# deliberately NOT managed here (no cloudflare_r2_bucket resource / import).
#
# S3 credentials (Cloudflare's R2 convention, see outputs.tf):
#   access_key_id     = token id
#   secret_access_key = sha256(token value)
resource "cloudflare_api_token" "campsite_raw_ro" {
  name = "campsite-raw-ro"

  policies = [{
    effect = "allow"
    permission_groups = [
      { id = "b4992e1108244f5d8bdbd7373ae9a774" }, # Workers R2 Storage Read
    ]
    # Single bucket, jurisdiction "default". If your account rejects bucket-level
    # scoping, fall back to account scope:
    #   "com.cloudflare.api.account.${var.cloudflare_account_id}" = "*"
    resources = {
      "com.cloudflare.edge.r2.bucket.${var.cloudflare_account_id}_default_${var.campsite_bucket_name}" = "*"
    }
  }]
}
