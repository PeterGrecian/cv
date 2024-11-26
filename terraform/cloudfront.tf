
locals {
  s3_origin_id = "myS3Origin"
}

#https://medium.com/@frankpromiseedah/hosting-a-static-website-on-aws-s3-using-terraform-e12addd22d18
# source from the s3 pages if the lambda 'active' pages fail
resource "aws_s3_bucket" "b" {
  bucket = var.origin_bucket

  tags = {
    Name = "website"
  }
}



resource "aws_s3_bucket_website_configuration" "example" {
  bucket = aws_s3_bucket.b.id

  index_document {
    suffix = "cv.html"
  }

}

resource "aws_s3_bucket_acl" "b_acl" {
  bucket = "petergrecian-cvlogs"
  acl    = "public-read"
}

resource "aws_s3_bucket" "cvlogs" {
    bucket = var.log_bucket
    # FIXME
    # ACLs had to be enabled by hand - they are deprecated but terraform v1.9.8-dev
    # provider registry.terraform.io/hashicorp/aws v5.76.0 says
    # InvalidArgument: The S3 bucket that you specified for CloudFront logs does not enable ACL access
}

resource "aws_cloudfront_distribution" "s3_distribution" {
  origin {
    domain_name              = aws_s3_bucket.b.bucket_regional_domain_name
    #origin_access_control_id = aws_cloudfront_origin_access_control.default.id
    origin_id                = local.s3_origin_id
  }

  enabled             = true
  is_ipv6_enabled     = true
  comment             = "CV"
  default_root_object = "cv.html"

  logging_config {
    include_cookies = false
    bucket          = "${var.log_bucket}.s3.amazonaws.com"
    prefix          = "CloudFront"
  }

  aliases = [var.destination]

  default_cache_behavior {
    allowed_methods  = ["DELETE", "GET", "HEAD", "OPTIONS", "PATCH", "POST", "PUT"]
    cached_methods   = ["GET", "HEAD"]
    target_origin_id = local.s3_origin_id

    forwarded_values {
      query_string = false

      cookies {
        forward = "none"
      }
    }

    viewer_protocol_policy = "allow-all"
    min_ttl                = 0
    default_ttl            = 3600
    max_ttl                = 86400
  }

  # Cache behavior with precedence 0
  ordered_cache_behavior {
    path_pattern     = "/content/immutable/*"
    allowed_methods  = ["GET", "HEAD", "OPTIONS"]
    cached_methods   = ["GET", "HEAD", "OPTIONS"]
    target_origin_id = local.s3_origin_id

    forwarded_values {
      query_string = false
      headers      = ["Origin"]

      cookies {
        forward = "none"
      }
    }

    min_ttl                = 0
    default_ttl            = 86400
    max_ttl                = 31536000
    compress               = true
    viewer_protocol_policy = "redirect-to-https"
  }

  # Cache behavior with precedence 1
  ordered_cache_behavior {
    path_pattern     = "/content/*"
    allowed_methods  = ["GET", "HEAD", "OPTIONS"]
    cached_methods   = ["GET", "HEAD"]
    target_origin_id = local.s3_origin_id

    forwarded_values {
      query_string = false

      cookies {
        forward = "none"
      }
    }

    min_ttl                = 0
    default_ttl            = 3600
    max_ttl                = 86400
    compress               = true
    viewer_protocol_policy = "redirect-to-https"
  }

  price_class = "PriceClass_200"

  restrictions {
    geo_restriction {
      restriction_type = "whitelist"
      locations        = ["US", "CA", "GB", "DE"]
    }
  }

  tags = {
    Environment = "production"
  }

  viewer_certificate {
    # must be in us-east-1
    acm_certificate_arn = "arn:aws:acm:us-east-1:700630586062:certificate/571eb679-1684-40fd-9eed-b958204bc30f"
    ssl_support_method = "sni-only"
  }
}
# there should be a way of getting the arn of the cert from us-east-1 (required by clodformation)
# but I've not cracked it
#data "acm_certificate_arn" "wild" {
#    provider = aws/us-east-1
#    domain = "*.petergrecian.co.uk"
#}


data "aws_route53_zone" "pg" {
    name         = "petergrecian.co.uk."
    private_zone = false
}
resource "aws_route53_record" "cname" {
  zone_id = data.aws_route53_zone.pg.zone_id
  name    = "mycv.petergrecian.co.uk"
  type    = "CNAME"
  ttl     = 300
  records = [aws_cloudfront_distribution.s3_distribution.domain_name]
}



