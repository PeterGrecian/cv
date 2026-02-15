#!/bin/bash
# Import test shrapnel into Terraform state
# This makes it safe to destroy (and recreate if needed)

set -e  # Exit on error

cd "$(dirname "$0")"

echo "================================================"
echo "IMPORTING TEST SHRAPNEL TO TERRAFORM STATE"
echo "================================================"
echo ""
echo "This imports test resources so we can safely destroy them."
echo "If anything breaks, we can recreate with: terraform apply"
echo ""

# API Gateways
echo "📡 Importing API Gateways..."
terraform import aws_apigatewayv2_api.cv_experimental pd7awxx7p0 || echo "Already imported or doesn't exist"
terraform import aws_apigatewayv2_stage.cv_experimental pd7awxx7p0/lambda_stage || echo "Already imported or doesn't exist"

terraform import aws_apigatewayv2_api.example_http_api 4y740klap4 || echo "Already imported or doesn't exist"
terraform import aws_apigatewayv2_stage.example_http_api 4y740klap4/serverless_lambda_stage || echo "Already imported or doesn't exist"

terraform import aws_apigatewayv2_api.my_api nxr30ss3wl || echo "Already imported or doesn't exist"
terraform import aws_apigatewayv2_stage.my_api nxr30ss3wl/default || echo "Already imported or doesn't exist"

# Custom Domains
echo "🌐 Importing Custom Domains..."
terraform import aws_apigatewayv2_domain_name.yyy yyy.petergrecian.co.uk || echo "Already imported or doesn't exist"
terraform import aws_apigatewayv2_domain_name.zzz zzz.petergrecian.co.uk || echo "Already imported or doesn't exist"

# Route53 Records
echo "📝 Importing Route53 Records..."
ZONE_ID=$(aws route53 list-hosted-zones --query "HostedZones[?Name=='petergrecian.co.uk.'].Id" --output text | cut -d'/' -f3)
echo "Zone ID: $ZONE_ID"

terraform import aws_route53_record.yyy "${ZONE_ID}_yyy.petergrecian.co.uk_A" || echo "Already imported or doesn't exist"
terraform import aws_route53_record.zzz "${ZONE_ID}_zzz.petergrecian.co.uk_A" || echo "Already imported or doesn't exist"
terraform import aws_route53_record.zzz_acm_validation "${ZONE_ID}__ffc58202af78c7349d8cd1d11f2c6064.zzz.petergrecian.co.uk._CNAME" || echo "Already imported or doesn't exist"

# Lambda Functions
echo "🔧 Importing Lambda Functions..."
terraform import aws_lambda_function.cv_experimental cv-experimental || echo "Already imported or doesn't exist"
terraform import aws_lambda_function.example_lambda example_lambda_function || echo "Already imported or doesn't exist"
terraform import aws_lambda_function.my_api_handler my-api-handler || echo "Already imported or doesn't exist"

echo ""
echo "✅ Import complete!"
echo ""
echo "================================================"
echo "NEXT STEPS:"
echo "================================================"
echo "1. Run: terraform plan"
echo "   (Review what Terraform thinks needs changing)"
echo ""
echo "2. Run: terraform plan -destroy -target=module... OR use destroy-shrapnel.sh"
echo "   (Preview what will be deleted)"
echo ""
echo "3. If it looks safe, run: ./destroy-shrapnel.sh"
echo "   (Actually delete the test resources)"
echo ""
echo "⚠️  IMPORTANT: Check that 'cvdev' and 'w3.petergrecian.co.uk' are NOT in the destroy list!"
echo ""
