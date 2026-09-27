output "bucket_name" {
  description = "Private staging bucket name to set as KATCHA_REMOTION_STAGING_BUCKET."
  value       = aws_s3_bucket.render_staging.bucket
}

output "bucket_arn" {
  description = "ARN of the staging bucket."
  value       = aws_s3_bucket.render_staging.arn
}

output "staging_prefix" {
  description = "Staging prefix to set as KATCHA_REMOTION_STAGING_PREFIX."
  value       = trim(var.staging_prefix, "/")
}

output "renderer_iam_policy_json" {
  description = "Least-privilege S3 policy document to attach to the renderer's AWS principal."
  value       = data.aws_iam_policy_document.renderer_access.json
}

output "renderer_role_arn" {
  description = "Existing durable renderer role receiving staging access."
  value       = data.aws_iam_role.renderer.arn
}

output "renderer_staging_policy_name" {
  description = "Inline IAM policy name attached to the durable renderer role."
  value       = aws_iam_role_policy.renderer_staging_access.name
}
