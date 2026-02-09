#!/bin/bash
#
# Quick script to update Grafana with latest Lambda logs
#

set -e

echo "=================================="
echo "Updating Lambda Logs for Grafana"
echo "=================================="
echo

# Step 1: Export from DynamoDB to S3
echo "1. Exporting DynamoDB logs to S3..."
python3 /home/tot/cv/export_logs_to_s3.py
echo

# Step 2: Update Athena partitions
echo "2. Updating Athena partitions..."
aws athena start-query-execution \
    --query-string "MSCK REPAIR TABLE lambda_logs.execution_logs" \
    --query-execution-context Database=lambda_logs \
    --result-configuration OutputLocation=s3://gardencam-berrylands-eu-west-1/athena-query-results/ \
    --region eu-west-1 \
    --output json > /dev/null

echo "   ✓ Partition update initiated"
echo

echo "=================================="
echo "✓ Update complete!"
echo "=================================="
echo
echo "Grafana will now have access to the latest logs."
echo "Visit: http://localhost:3000"
echo
