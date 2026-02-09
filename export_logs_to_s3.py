#!/usr/bin/env python3
"""
Export Lambda execution logs from DynamoDB to S3 for Athena querying.
Formats data with date partitioning for efficient queries.
"""

import boto3
import json
from datetime import datetime
from decimal import Decimal
from collections import defaultdict

REGION = 'eu-west-1'
DYNAMODB_TABLE = 'lambda-execution-logs'
S3_BUCKET = 'gardencam-berrylands-eu-west-1'
S3_PREFIX = 'lambda-logs-athena/'

class DecimalEncoder(json.JSONEncoder):
    """Handle Decimal types from DynamoDB."""
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super(DecimalEncoder, self).default(obj)


def scan_dynamodb_logs():
    """Scan all logs from DynamoDB."""
    dynamodb = boto3.resource('dynamodb', region_name=REGION)
    table = dynamodb.Table(DYNAMODB_TABLE)

    logs = []

    print("Scanning DynamoDB table...")
    response = table.scan()
    logs.extend(response.get('Items', []))

    # Handle pagination
    while 'LastEvaluatedKey' in response:
        print(f"  Retrieved {len(logs)} items so far...")
        response = table.scan(ExclusiveStartKey=response['LastEvaluatedKey'])
        logs.extend(response.get('Items', []))

    print(f"Total items retrieved: {len(logs)}")
    return logs


def partition_logs_by_date(logs):
    """Partition logs by date for efficient Athena queries."""
    partitioned = defaultdict(list)

    for log in logs:
        timestamp = log.get('timestamp', '')
        if not timestamp:
            continue

        # Extract date from ISO timestamp (YYYY-MM-DD)
        date = timestamp.split('T')[0]
        partitioned[date].append(log)

    return partitioned


def upload_to_s3(partitioned_logs):
    """Upload partitioned logs to S3."""
    s3 = boto3.client('s3', region_name=REGION)

    total_uploaded = 0

    for date, logs in sorted(partitioned_logs.items()):
        # Create S3 key with date partitioning
        # Format: lambda-logs-athena/year=2026/month=02/day=09/data.json
        year, month, day = date.split('-')
        s3_key = f"{S3_PREFIX}year={year}/month={month}/day={day}/data.json"

        # Convert logs to newline-delimited JSON (required for Athena)
        # Also add explicit date fields for easier querying
        enriched_logs = []
        for log in logs:
            log['date'] = date
            log['year'] = int(year)
            log['month'] = int(month)
            log['day'] = int(day)
            enriched_logs.append(log)

        json_data = '\n'.join([json.dumps(log, cls=DecimalEncoder) for log in enriched_logs])

        # Upload to S3
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=s3_key,
            Body=json_data.encode('utf-8'),
            ContentType='application/json'
        )

        total_uploaded += len(logs)
        print(f"Uploaded {len(logs)} logs for {date} to s3://{S3_BUCKET}/{s3_key}")

    return total_uploaded


def main():
    print(f"Starting export from DynamoDB to S3...")
    print(f"  Source: DynamoDB table '{DYNAMODB_TABLE}' in {REGION}")
    print(f"  Destination: s3://{S3_BUCKET}/{S3_PREFIX}")
    print()

    # Scan DynamoDB
    logs = scan_dynamodb_logs()

    if not logs:
        print("No logs found in DynamoDB.")
        return

    # Partition by date
    print("\nPartitioning logs by date...")
    partitioned_logs = partition_logs_by_date(logs)
    print(f"Logs span {len(partitioned_logs)} dates")

    # Upload to S3
    print("\nUploading to S3...")
    total = upload_to_s3(partitioned_logs)

    print(f"\n✓ Export complete!")
    print(f"  Total logs exported: {total}")
    print(f"  Date range: {min(partitioned_logs.keys())} to {max(partitioned_logs.keys())}")
    print(f"\nNext step: Create Athena table to query this data")


if __name__ == '__main__':
    main()
