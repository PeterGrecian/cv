#!/bin/bash
# Destroy test shrapnel safely
# Only destroys resources in cleanup-test-shrapnel.tf

set -e  # Exit on error

cd "$(dirname "$0")"

echo "================================================"
echo "⚠️  DESTROYING TEST SHRAPNEL"
echo "================================================"
echo ""
echo "This will DELETE the following:"
echo "  - cv-experimental API Gateway + Lambda"
echo "  - example-http-api API Gateway + Lambda"
echo "  - my-api-api API Gateway + Lambda"
echo "  - yyy.petergrecian.co.uk domain + Route53 record"
echo "  - zzz.petergrecian.co.uk domain + Route53 record"
echo ""
echo "✅ SAFE: This will NOT touch:"
echo "  - cvdev (your production Lambda)"
echo "  - w3.petergrecian.co.uk (your production domain)"
echo "  - Any S3 buckets or DynamoDB tables"
echo ""
echo "Press Ctrl+C to cancel, or Enter to preview what will be destroyed..."
read

echo ""
echo "📋 PREVIEW: Here's what will be destroyed..."
echo ""

# Preview destruction
terraform plan -destroy \
  -target=aws_apigatewayv2_api.cv_experimental \
  -target=aws_apigatewayv2_stage.cv_experimental \
  -target=aws_apigatewayv2_api.example_http_api \
  -target=aws_apigatewayv2_stage.example_http_api \
  -target=aws_apigatewayv2_api.my_api \
  -target=aws_apigatewayv2_stage.my_api \
  -target=aws_apigatewayv2_domain_name.yyy \
  -target=aws_apigatewayv2_domain_name.zzz \
  -target=aws_route53_record.yyy \
  -target=aws_route53_record.zzz \
  -target=aws_route53_record.zzz_acm_validation \
  -target=aws_lambda_function.cv_experimental \
  -target=aws_lambda_function.example_lambda \
  -target=aws_lambda_function.my_api_handler

echo ""
echo "================================================"
echo "⚠️  FINAL CONFIRMATION"
echo "================================================"
echo ""
echo "Review the plan above carefully."
echo "Make sure it does NOT include:"
echo "  ❌ cvdev"
echo "  ❌ w3.petergrecian.co.uk"
echo "  ❌ Any S3 buckets"
echo "  ❌ Any DynamoDB tables"
echo ""
echo "If anything looks wrong, press Ctrl+C NOW!"
echo ""
echo "Press Enter to DESTROY the test shrapnel..."
read

echo ""
echo "🔥 Destroying test resources..."
echo ""

# Actually destroy
terraform destroy -auto-approve \
  -target=aws_apigatewayv2_api.cv_experimental \
  -target=aws_apigatewayv2_stage.cv_experimental \
  -target=aws_apigatewayv2_api.example_http_api \
  -target=aws_apigatewayv2_stage.example_http_api \
  -target=aws_apigatewayv2_api.my_api \
  -target=aws_apigatewayv2_stage.my_api \
  -target=aws_apigatewayv2_domain_name.yyy \
  -target=aws_apigatewayv2_domain_name.zzz \
  -target=aws_route53_record.yyy \
  -target=aws_route53_record.zzz \
  -target=aws_route53_record.zzz_acm_validation \
  -target=aws_lambda_function.cv_experimental \
  -target=aws_lambda_function.example_lambda \
  -target=aws_lambda_function.my_api_handler

echo ""
echo "================================================"
echo "✅ CLEANUP COMPLETE!"
echo "================================================"
echo ""
echo "Test shrapnel has been destroyed."
echo ""
echo "Your production site (w3.petergrecian.co.uk) is still running."
echo ""
echo "If you need to undo this, run:"
echo "  terraform apply"
echo "(This will recreate the destroyed resources)"
echo ""
