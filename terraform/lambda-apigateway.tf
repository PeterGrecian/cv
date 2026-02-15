# Lambda Function
resource "aws_lambda_function" "cvdev" {
  function_name = "cvdev"
  role          = aws_iam_role.cvdev.arn
  handler       = "cv.lambda_handler"
  runtime       = "python3.11"
  timeout       = 30
  memory_size   = 128

  # Dummy filename required by Terraform - actual deployment handled by ./update script
  filename         = "dummy.zip"
  source_code_hash = filebase64sha256("dummy.zip")

  # The deployment is handled by the ./update script
  # This prevents Terraform from trying to manage the code
  lifecycle {
    ignore_changes = [
      filename,
      source_code_hash,
    ]
  }

  tags = {
    hashicorp-learn = "lambda-api-gateway"
  }
}

# IAM Role for Lambda
resource "aws_iam_role" "cvdev" {
  name = "cvdev"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = ""
        Effect = "Allow"
        Principal = {
          Service = "lambda.amazonaws.com"
        }
        Action = "sts:AssumeRole"
      }
    ]
  })

  tags = {
    hashicorp-learn = "lambda-api-gateway"
  }
}

# Attach AWS managed policy for basic Lambda execution
resource "aws_iam_role_policy_attachment" "lambda_basic_execution" {
  role       = aws_iam_role.cvdev.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaBasicExecutionRole"
}

# Note: Inline policies need to be added separately or imported
# Current inline policies on the role:
# - cvdev-cloudwatch
# - cvdev-dynamodb-s3-secrets
# - DynamoDBAccessLogs
# - gardencam-timing-access
# - gardencam-video-metadata-access
# - GardencamCommandsWrite
# - GardencamSecretsAccess
# - GardencamStatsRead
# - hits-db
# - lambda-execution-logs-access

# API Gateway HTTP API
resource "aws_apigatewayv2_api" "cvdev" {
  name          = "cvdev"
  protocol_type = "HTTP"

  tags = {
    hashicorp-learn = "lambda-api-gateway"
  }
}

# API Gateway Stage
resource "aws_apigatewayv2_stage" "cv" {
  api_id      = aws_apigatewayv2_api.cvdev.id
  name        = "cv"
  auto_deploy = true

  access_log_settings {
    destination_arn = "arn:aws:logs:eu-west-1:700630586062:log-group:/aws/api_gw/cvdev"
    format = jsonencode({
      httpMethod               = "$context.httpMethod"
      integrationErrorMessage  = "$context.integrationErrorMessage"
      protocol                 = "$context.protocol"
      requestId                = "$context.requestId"
      requestTime              = "$context.requestTime"
      resourcePath             = "$context.resourcePath"
      responseLength           = "$context.responseLength"
      routeKey                 = "$context.routeKey"
      sourceIp                 = "$context.identity.sourceIp"
      status                   = "$context.status"
    })
  }

  default_route_settings {
    detailed_metrics_enabled = false
    throttling_burst_limit   = 100
    throttling_rate_limit    = 50
  }

  tags = {
    hashicorp-learn = "lambda-api-gateway"
  }
}

# API Gateway Integration with Lambda
resource "aws_apigatewayv2_integration" "lambda" {
  api_id           = aws_apigatewayv2_api.cvdev.id
  integration_type = "AWS_PROXY"
  integration_uri  = aws_lambda_function.cvdev.invoke_arn

  integration_method      = "POST"
  payload_format_version  = "1.0"
  timeout_milliseconds    = 30000
}

# API Gateway Default Route
resource "aws_apigatewayv2_route" "default" {
  api_id    = aws_apigatewayv2_api.cvdev.id
  route_key = "$default"
  target    = "integrations/${aws_apigatewayv2_integration.lambda.id}"
}

# Lambda Permission for API Gateway
resource "aws_lambda_permission" "api_gateway" {
  statement_id  = "AllowExecutionFromAPIGateway"
  action        = "lambda:InvokeFunction"
  function_name = aws_lambda_function.cvdev.function_name
  principal     = "apigateway.amazonaws.com"
  source_arn    = "${aws_apigatewayv2_api.cvdev.execution_arn}/*/*"
}

# Custom Domain Name
resource "aws_apigatewayv2_domain_name" "w3" {
  domain_name = "w3.petergrecian.co.uk"

  domain_name_configuration {
    certificate_arn = "arn:aws:acm:eu-west-1:700630586062:certificate/51662f63-ffcf-40f6-a995-4e91b2977605"
    endpoint_type   = "REGIONAL"
    security_policy = "TLS_1_2"
  }
}

# API Mapping to Custom Domain
resource "aws_apigatewayv2_api_mapping" "w3" {
  api_id      = aws_apigatewayv2_api.cvdev.id
  domain_name = aws_apigatewayv2_domain_name.w3.id
  stage       = aws_apigatewayv2_stage.cv.id
}

# Route53 Record for Custom Domain
resource "aws_route53_record" "w3" {
  zone_id = data.aws_route53_zone.pg.zone_id
  name    = "w3.petergrecian.co.uk"
  type    = "CNAME"
  ttl     = 300
  records = [aws_apigatewayv2_domain_name.w3.domain_name_configuration[0].target_domain_name]
}
