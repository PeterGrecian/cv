# Terraform Import Summary

## Successfully Imported Resources ✅

All Lambda and API Gateway resources have been successfully imported into Terraform state:

### Lambda Resources
- ✅ `aws_lambda_function.cvdev` - Lambda function
- ✅ `aws_iam_role.cvdev` - IAM execution role
- ✅ `aws_iam_role_policy_attachment.lambda_basic_execution` - Basic execution policy

### API Gateway Resources
- ✅ `aws_apigatewayv2_api.cvdev` - HTTP API (ID: 41bmi2t2yc)
- ✅ `aws_apigatewayv2_stage.cv` - Stage with throttling settings (100 burst, 50 req/s)
- ✅ `aws_apigatewayv2_integration.lambda` - Lambda integration
- ✅ `aws_apigatewayv2_route.default` - Default route
- ✅ `aws_lambda_permission.api_gateway` - Permission for API Gateway to invoke Lambda

### Custom Domain Resources
- ✅ `aws_apigatewayv2_domain_name.w3` - w3.petergrecian.co.uk
- ✅ `aws_apigatewayv2_api_mapping.w3` - Domain mapping to API
- ✅ `aws_route53_record.w3` - Route53 CNAME record

## Configuration Files Created

1. **`lambda-apigateway.tf`** - Main configuration for Lambda and API Gateway
2. **`terraform.tfvars`** - Variable values
3. **`dummy.zip`** - Dummy deployment package (actual deployments handled by `./update` script)
4. **`import-resources.sh`** - Import script (now complete)

## Important Notes

### Lambda Deployment
The Lambda function code is **NOT** managed by Terraform. The `./update` script continues to handle deployments:
- Terraform configuration uses `lifecycle.ignore_changes` for `filename` and `source_code_hash`
- This prevents Terraform from trying to manage the deployment package
- Continue using `./update` script for code deployments

### Throttling Settings Preserved
The API Gateway throttle settings are now in version control:
```hcl
throttling_burst_limit = 100
throttling_rate_limit  = 50
```

### Inline IAM Policies
The following inline policies on the IAM role are **NOT** yet managed by Terraform:
- cvdev-cloudwatch
- cvdev-dynamodb-s3-secrets
- DynamoDBAccessLogs
- gardencam-timing-access
- gardencam-video-metadata-access
- GardencamCommandsWrite
- GardencamSecretsAccess
- GardencamStatsRead
- hits-db
- lambda-execution-logs-access

These can be imported later if needed, or left as manually managed.

## Verification

Run `terraform plan` to verify no changes are needed for Lambda/API Gateway resources:

```bash
cd /home/tot/cv/terraform
terraform plan
```

Expected output: Only changes for CloudFront/S3 resources (not Lambda/API Gateway).

## Next Steps

1. ✅ Lambda and API Gateway are now under Terraform management
2. ✅ Throttling limits are in version control
3. ⚠️ **CloudFront distribution** (E1T3SJ3IBC8DRC) was detected as deleted - may need cleanup
4. 📝 Consider adding inline IAM policies to Terraform if you want full IaC coverage

## Usage

To apply changes to the infrastructure:

```bash
cd /home/tot/cv/terraform
terraform plan    # Review changes
terraform apply   # Apply changes
```

**Remember:** Lambda code deployments still use `./update` script!
