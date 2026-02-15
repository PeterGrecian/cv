# TEST SHRAPNEL - Safe to destroy
# This file contains test infrastructure that can be deleted
# If you need to recreate any of this, just run: terraform apply

# ==============================================================================
# API GATEWAYS - Test instances
# ==============================================================================

resource "aws_apigatewayv2_api" "cv_experimental" {
  name          = "cv-experimental"
  protocol_type = "HTTP"

  tags = {
    Status = "test-shrapnel"
  }
}

resource "aws_apigatewayv2_stage" "cv_experimental" {
  api_id      = aws_apigatewayv2_api.cv_experimental.id
  name        = "lambda_stage"
  auto_deploy = true
}

resource "aws_apigatewayv2_api" "example_http_api" {
  name          = "example-http-api"
  protocol_type = "HTTP"

  tags = {
    Status = "test-shrapnel"
  }
}

resource "aws_apigatewayv2_stage" "example_http_api" {
  api_id      = aws_apigatewayv2_api.example_http_api.id
  name        = "serverless_lambda_stage"
  auto_deploy = true
}

resource "aws_apigatewayv2_api" "my_api" {
  name          = "my-api-api"
  protocol_type = "HTTP"

  tags = {
    Status = "test-shrapnel"
  }
}

resource "aws_apigatewayv2_stage" "my_api" {
  api_id      = aws_apigatewayv2_api.my_api.id
  name        = "default"
  auto_deploy = true
}

# ==============================================================================
# CUSTOM DOMAINS - Broken/unused
# ==============================================================================

resource "aws_apigatewayv2_domain_name" "yyy" {
  domain_name = "yyy.petergrecian.co.uk"

  domain_name_configuration {
    certificate_arn = "arn:aws:acm:eu-west-1:700630586062:certificate/51662f63-ffcf-40f6-a995-4e91b2977605"
    endpoint_type   = "REGIONAL"
    security_policy = "TLS_1_2"
  }

  tags = {
    Status = "test-shrapnel-no-mappings"
  }
}

resource "aws_apigatewayv2_domain_name" "zzz" {
  domain_name = "zzz.petergrecian.co.uk"

  domain_name_configuration {
    certificate_arn = "arn:aws:acm:eu-west-1:700630586062:certificate/51662f63-ffcf-40f6-a995-4e91b2977605"
    endpoint_type   = "REGIONAL"
    security_policy = "TLS_1_2"
  }

  tags = {
    Status = "test-shrapnel-broken-mapping"
  }
}

# Note: zzz has an API mapping to a deleted API (4w46nd3x5l)
# The mapping will be automatically deleted when the domain is destroyed

# ==============================================================================
# ROUTE53 RECORDS - Pointing nowhere
# ==============================================================================

resource "aws_route53_record" "yyy" {
  zone_id = data.aws_route53_zone.pg.zone_id
  name    = "yyy.petergrecian.co.uk"
  type    = "A"
  ttl     = 300
  records = ["127.0.0.1"]  # Placeholder - actual record points nowhere
}

resource "aws_route53_record" "zzz" {
  zone_id = data.aws_route53_zone.pg.zone_id
  name    = "zzz.petergrecian.co.uk"
  type    = "A"
  ttl     = 300
  records = ["127.0.0.1"]  # Placeholder - actual record points nowhere
}

# ACM validation CNAME for zzz (leftover)
resource "aws_route53_record" "zzz_acm_validation" {
  zone_id = data.aws_route53_zone.pg.zone_id
  name    = "_ffc58202af78c7349d8cd1d11f2c6064.zzz.petergrecian.co.uk"
  type    = "CNAME"
  ttl     = 300
  records = ["_4c4071381c4ccd5eb93eb9c0eb5c7212.djqtsrsxkq.acm-validations.aws."]
}

# ==============================================================================
# LAMBDA FUNCTIONS - Test functions
# ==============================================================================

resource "aws_lambda_function" "cv_experimental" {
  function_name = "cv-experimental"
  role          = "arn:aws:iam::700630586062:role/service-role/cv-experimental-role-placeholder"
  handler       = "index.handler"
  runtime       = "python3.11"
  timeout       = 30
  memory_size   = 128

  filename         = "${path.module}/dummy.zip"
  source_code_hash = filebase64sha256("${path.module}/dummy.zip")

  lifecycle {
    ignore_changes = [filename, source_code_hash, role]
  }

  tags = {
    Status = "test-shrapnel"
  }
}

resource "aws_lambda_function" "example_lambda" {
  function_name = "example_lambda_function"
  role          = "arn:aws:iam::700630586062:role/service-role/example-role-placeholder"
  handler       = "index.handler"
  runtime       = "python3.11"
  timeout       = 30
  memory_size   = 128

  filename         = "${path.module}/dummy.zip"
  source_code_hash = filebase64sha256("${path.module}/dummy.zip")

  lifecycle {
    ignore_changes = [filename, source_code_hash, role]
  }

  tags = {
    Status = "test-shrapnel"
  }
}

resource "aws_lambda_function" "my_api_handler" {
  function_name = "my-api-handler"
  role          = "arn:aws:iam::700630586062:role/service-role/my-api-role-placeholder"
  handler       = "index.handler"
  runtime       = "python3.11"
  timeout       = 30
  memory_size   = 128

  filename         = "${path.module}/dummy.zip"
  source_code_hash = filebase64sha256("${path.module}/dummy.zip")

  lifecycle {
    ignore_changes = [filename, source_code_hash, role]
  }

  tags = {
    Status = "test-shrapnel"
  }
}
