#!/bin/bash
# Import existing AWS resources into Terraform state

set -e

echo "Importing existing AWS resources into Terraform..."

# Import Lambda Function
echo "Importing Lambda function..."
terraform import aws_lambda_function.cvdev cvdev

# Import IAM Role
echo "Importing IAM role..."
terraform import aws_iam_role.cvdev cvdev

# Import IAM Role Policy Attachment
echo "Importing IAM role policy attachment..."
terraform import aws_iam_role_policy_attachment.lambda_basic_execution cvdev/arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole

# Import API Gateway API
echo "Importing API Gateway HTTP API..."
terraform import aws_apigatewayv2_api.cvdev 41bmi2t2yc

# Import API Gateway Stage
echo "Importing API Gateway stage..."
terraform import aws_apigatewayv2_stage.cv 41bmi2t2yc/cv

# Import API Gateway Integration
echo "Importing API Gateway integration..."
terraform import aws_apigatewayv2_integration.lambda 41bmi2t2yc/3c89kcp

# Import API Gateway Route
echo "Importing API Gateway route..."
terraform import aws_apigatewayv2_route.default 41bmi2t2yc/vb0todg

# Import Lambda Permission
echo "Importing Lambda permission..."
terraform import aws_lambda_permission.api_gateway cvdev/AllowExecutionFromAPIGateway

# Import Custom Domain Name
echo "Importing custom domain name..."
terraform import aws_apigatewayv2_domain_name.w3 w3.petergrecian.co.uk

# Import API Mapping
echo "Importing API mapping..."
terraform import aws_apigatewayv2_api_mapping.w3 aun38c

# Import Route53 Record
echo "Importing Route53 record..."
ZONE_ID=$(aws route53 list-hosted-zones --query "HostedZones[?Name=='petergrecian.co.uk.'].Id" --output text | cut -d'/' -f3)
terraform import aws_route53_record.w3 ${ZONE_ID}_w3.petergrecian.co.uk_CNAME

echo ""
echo "✅ All resources imported successfully!"
echo ""
echo "Next steps:"
echo "1. Run 'terraform plan' to verify the configuration matches the existing resources"
echo "2. If there are any differences, update the Terraform configuration"
echo "3. Once plan shows no changes, the import is complete"
