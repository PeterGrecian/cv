# AWS Infrastructure Cleanup Guide

## What's the Problem?

Your AWS account has test shrapnel from experiments:
- **yyy.petergrecian.co.uk** - broken domain with no API mappings
- **zzz.petergrecian.co.uk** - domain pointing to deleted API
- **cv-experimental** - test API Gateway + Lambda
- **example-http-api** - test API Gateway + Lambda
- **my-api-api** - test API Gateway + Lambda

## What's Safe (Production)?

✅ **DO NOT TOUCH THESE:**
- `cvdev` - Your production Lambda
- `w3.petergrecian.co.uk` - Your production domain
- All S3 buckets
- All DynamoDB tables
- `pi-fleet-api` - Active (created today)
- `t3-api` - Possibly active (bus times?)

## How to Clean Up (Foolproof Process)

### Step 1: Import to Terraform
```bash
cd /home/tot/cv/terraform
./import-shrapnel.sh
```

This imports test resources into Terraform state so you can recreate them if needed.

### Step 2: Verify What Will Be Destroyed
```bash
terraform plan
```

**CRITICAL CHECK:** Make sure the output does NOT include:
- ❌ `cvdev`
- ❌ `w3.petergrecian.co.uk`
- ❌ Any S3 buckets
- ❌ Any DynamoDB tables

If you see any of those, **STOP** and ask for help!

### Step 3: Destroy Test Shrapnel
```bash
./destroy-shrapnel.sh
```

This will:
1. Show you a preview of what will be destroyed
2. Ask for confirmation (press Ctrl+C to cancel)
3. Destroy only the test resources

### Step 4: Verify Your Site Still Works
```bash
curl https://w3.petergrecian.co.uk
```

Should return your CV HTML. If it doesn't, **immediately** run:
```bash
terraform apply
```

This will recreate everything (though you probably only want to recreate production stuff).

## If Something Goes Wrong

### Undo Everything
```bash
terraform apply
```

This recreates all destroyed resources with the same configuration (but new AWS IDs).

### Check What's Broken
```bash
aws apigatewayv2 get-apis --region eu-west-1
aws apigatewayv2 get-domain-names --region eu-west-1
```

### Emergency Restore Production
If w3.petergrecian.co.uk breaks (it shouldn't!):
```bash
cd /home/tot/cv/terraform
terraform apply -target=aws_lambda_function.cvdev
terraform apply -target=aws_apigatewayv2_api.cvdev
terraform apply -target=aws_apigatewayv2_domain_name.w3
```

## Files Created

- `cleanup-test-shrapnel.tf` - Terraform config for test resources
- `import-shrapnel.sh` - Script to import resources to Terraform state
- `destroy-shrapnel.sh` - Script to safely destroy test resources
- `CLEANUP-README.md` - This file

## What Gets Deleted?

### API Gateways (3)
- cv-experimental (pd7awxx7p0)
- example-http-api (4y740klap4)
- my-api-api (nxr30ss3wl)

### Custom Domains (2)
- yyy.petergrecian.co.uk
- zzz.petergrecian.co.uk

### Route53 Records (3)
- yyy.petergrecian.co.uk A record
- zzz.petergrecian.co.uk A record
- zzz ACM validation CNAME

### Lambda Functions (3)
- cv-experimental
- example_lambda_function
- my-api-handler

## Cost Savings

These idle resources cost ~$0.50-1.00/month. Not much, but cleanup is good hygiene!

## Questions?

1. **Will this break my website?** No - w3.petergrecian.co.uk uses completely different resources
2. **Can I undo it?** Yes - `terraform apply` recreates everything
3. **What if I'm not sure?** Don't run it - the test resources aren't hurting anything
4. **How do I know it worked?** Run `aws apigatewayv2 get-apis --region eu-west-1` - should only show cvdev, pi-fleet-api, t3-api
