# terraform — R2 backup credentials

Manages the Cloudflare R2 pieces for the InfluxDB offsite backup so the token is
**reproducible** instead of hand-pasted:

- `cloudflare_r2_bucket.influxdb_backups` — the `influxdb-backups` bucket
  (already exists, so it's **imported** on first apply).
- `cloudflare_api_token.influxdb_backups_rw` — an R2 S3-API token scoped to
  Object Read & Write on **only** that bucket.

Outputs the S3 credentials (sensitive) to drop into `influxdb/.env`.

> Validated with `terraform validate` against cloudflare provider v5.19.1, but
> **not applied** here (no Cloudflare API token on the machine). Run `plan`
> first. State is local and contains the token value — it's gitignored.

## Prerequisites

A Cloudflare **API token** (different from an R2 S3 key) with permissions to
manage R2 and create API tokens — e.g. *Account · Workers R2 Storage · Edit* and
*User · API Tokens · Edit*. Create it at
https://dash.cloudflare.com/profile/api-tokens.

## Apply

```sh
cd terraform
export TF_VAR_cloudflare_api_token=<that API token>
terraform init
terraform plan      # review: import the bucket + create 1 api token
terraform apply
```

If `apply` rejects the bucket-scoped resource string, switch `resources` in
`r2.tf` to account scope (`com.cloudflare.api.account.<id>` = "*") and re-apply.
After the first successful apply you can delete the `import` block in `r2.tf`.

## Wire the creds into influxdb/.env

```sh
cd terraform
ACCT=$(terraform output -raw r2_account_id)
BUCKET=$(terraform output -raw r2_bucket)
AK=$(terraform output -raw r2_access_key_id)
SK=$(terraform output -raw r2_secret_access_key)
cat >> ../influxdb/.env <<EOF
R2_ACCOUNT_ID=$ACCT
R2_ACCESS_KEY_ID=$AK
R2_SECRET_ACCESS_KEY=$SK
R2_BUCKET=$BUCKET
EOF
```

Verify, then the next backup uploads offsite:

```sh
cd ../influxdb && uv run --with boto3 python r2_upload.py check   # write probe
./backup.sh                                                       # full run
```

The **Backups** Grafana dashboard's R2 panels turn from "NOT CONFIGURED" to live.
