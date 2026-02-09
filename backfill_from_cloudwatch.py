#!/usr/bin/env python3
"""
Backfill Lambda execution logs from CloudWatch Logs to S3 for Athena.
This parses CloudWatch Logs to extract execution metrics going back in history.
"""

import boto3
import json
import time
from datetime import datetime, timedelta
from decimal import Decimal
from collections import defaultdict

REGION = 'eu-west-1'
LOG_GROUP = '/aws/lambda/cvdev'
S3_BUCKET = 'gardencam-berrylands-eu-west-1'
S3_PREFIX = 'lambda-logs-athena/'
ATHENA_OUTPUT = f's3://{S3_BUCKET}/athena-query-results/'

# Memory size for cost calculation (128 MB for cvdev)
MEMORY_MB = 128

class DecimalEncoder(json.JSONEncoder):
    """Handle Decimal types."""
    def default(self, obj):
        if isinstance(obj, Decimal):
            return float(obj)
        return super(DecimalEncoder, self).default(obj)


def query_cloudwatch_logs(start_time, end_time):
    """Query CloudWatch Logs for Lambda executions using Logs Insights."""
    client = boto3.client('logs', region_name=REGION)

    # Query to extract REPORT lines which contain execution metrics
    query = """
    fields @timestamp, @requestId, @duration, @billedDuration, @maxMemoryUsed, @memorySize, @message
    | filter @type = "REPORT"
    | sort @timestamp asc
    """

    print(f"Querying CloudWatch Logs from {start_time} to {end_time}...")

    response = client.start_query(
        logGroupName=LOG_GROUP,
        startTime=int(start_time.timestamp()),
        endTime=int(end_time.timestamp()),
        queryString=query,
    )

    query_id = response['queryId']

    # Wait for query to complete
    while True:
        result = client.get_query_results(queryId=query_id)
        status = result['status']

        if status == 'Complete':
            break
        elif status == 'Failed' or status == 'Cancelled':
            print(f"Query {status}")
            return []

        time.sleep(1)

    print(f"  Retrieved {len(result['results'])} REPORT lines")
    return result['results']


def query_path_logs(start_time, end_time):
    """Query for log lines containing 'path =' to extract request paths."""
    client = boto3.client('logs', region_name=REGION)

    query = """
    fields @timestamp, @requestId, @message
    | filter @message like /path = /
    | sort @timestamp asc
    """

    print(f"Querying for path information...")

    response = client.start_query(
        logGroupName=LOG_GROUP,
        startTime=int(start_time.timestamp()),
        endTime=int(end_time.timestamp()),
        queryString=query,
    )

    query_id = response['queryId']

    while True:
        result = client.get_query_results(queryId=query_id)
        status = result['status']

        if status == 'Complete':
            break
        elif status in ['Failed', 'Cancelled']:
            return {}

        time.sleep(1)

    # Build map of requestId -> path
    path_map = {}
    for row in result['results']:
        request_id = None
        message = None

        for field in row:
            if field['field'] == '@requestId':
                request_id = field['value']
            elif field['field'] == '@message':
                message = field['value']

        if request_id and message and 'path = ' in message:
            # Extract path from message like "path = /lambda-stats, stage = ..."
            try:
                path = message.split('path = ')[1].split(',')[0].strip()
                path_map[request_id] = path
            except:
                pass

    print(f"  Found paths for {len(path_map)} requests")
    return path_map


def parse_report_line(fields):
    """Parse REPORT line fields into structured data."""
    data = {}

    for field in fields:
        field_name = field['field']
        field_value = field['value']

        if field_name == '@timestamp':
            data['timestamp'] = field_value
        elif field_name == '@requestId':
            data['request_id'] = field_value
        elif field_name == '@duration':
            data['duration_ms'] = float(field_value)
        elif field_name == '@billedDuration':
            data['billed_duration_ms'] = float(field_value)
        elif field_name == '@memorySize':
            data['memory_limit_mb'] = float(field_value)
        elif field_name == '@maxMemoryUsed':
            data['max_memory_used_mb'] = float(field_value)

    return data


def calculate_cost(duration_ms, memory_mb):
    """Calculate estimated Lambda cost."""
    # Pricing as of 2024
    # Memory: $0.0000166667 per GB-second
    # Requests: $0.20 per 1M requests
    memory_gb = Decimal(str(memory_mb)) / Decimal('1024')
    duration_seconds = Decimal(str(duration_ms)) / Decimal('1000')
    memory_cost = memory_gb * duration_seconds * Decimal('0.0000166667')
    request_cost = Decimal('0.0000002')  # $0.20 per 1M requests
    total_cost = memory_cost + request_cost
    return total_cost


def process_logs(report_results, path_map):
    """Process CloudWatch logs into structured execution records."""
    executions = []

    for row in report_results:
        data = parse_report_line(row)

        if not data.get('request_id'):
            continue

        # Add path from path_map
        data['path'] = path_map.get(data['request_id'], '')

        # Add function name
        data['function_name'] = 'cvdev'

        # Calculate cost
        duration_ms = data.get('duration_ms', 0)
        memory_mb = data.get('memory_limit_mb', MEMORY_MB)
        data['estimated_cost_usd'] = calculate_cost(duration_ms, memory_mb)

        # Extract date from timestamp
        try:
            ts = datetime.fromisoformat(data['timestamp'].replace('Z', '+00:00'))
            data['date'] = ts.strftime('%Y-%m-%d')
            data['year'] = ts.year
            data['month'] = ts.month
            data['day'] = ts.day
        except:
            continue

        executions.append(data)

    return executions


def partition_logs_by_date(logs):
    """Partition logs by date."""
    partitioned = defaultdict(list)
    for log in logs:
        date = log.get('date', '')
        if date:
            partitioned[date].append(log)
    return partitioned


def upload_to_s3(partitioned_logs):
    """Upload partitioned logs to S3."""
    s3 = boto3.client('s3', region_name=REGION)
    total_uploaded = 0

    for date, logs in sorted(partitioned_logs.items()):
        year, month, day = date.split('-')
        s3_key = f"{S3_PREFIX}year={year}/month={month}/day={day}/data.json"

        # Convert to newline-delimited JSON
        json_data = '\n'.join([json.dumps(log, cls=DecimalEncoder) for log in logs])

        # Upload to S3 (will overwrite existing data for this date)
        s3.put_object(
            Bucket=S3_BUCKET,
            Key=s3_key,
            Body=json_data.encode('utf-8'),
            ContentType='application/json'
        )

        total_uploaded += len(logs)
        print(f"Uploaded {len(logs)} logs for {date} to s3://{S3_BUCKET}/{s3_key}")

    return total_uploaded


def update_athena_partitions():
    """Update Athena partitions to include new data."""
    client = boto3.client('athena', region_name=REGION)

    print("\nUpdating Athena partitions...")
    response = client.start_query_execution(
        QueryString='MSCK REPAIR TABLE lambda_logs.execution_logs',
        QueryExecutionContext={'Database': 'lambda_logs'},
        ResultConfiguration={'OutputLocation': ATHENA_OUTPUT}
    )

    query_id = response['QueryExecutionId']

    # Wait for completion
    while True:
        result = client.get_query_execution(QueryExecutionId=query_id)
        status = result['QueryExecution']['Status']['State']

        if status == 'SUCCEEDED':
            print("  ✓ Partitions updated")
            break
        elif status in ['FAILED', 'CANCELLED']:
            print(f"  ✗ Partition update {status}")
            break

        time.sleep(1)


def main():
    # Query last 180 days (cvdev retention period)
    end_time = datetime.utcnow()
    start_time = end_time - timedelta(days=180)

    print("="*70)
    print("CloudWatch Logs Backfill to S3/Athena")
    print("="*70)
    print(f"Log Group: {LOG_GROUP}")
    print(f"Date Range: {start_time.date()} to {end_time.date()}")
    print()

    # Query CloudWatch Logs
    report_results = query_cloudwatch_logs(start_time, end_time)

    if not report_results:
        print("No logs found in CloudWatch")
        return

    # Get path information
    path_map = query_path_logs(start_time, end_time)

    # Process logs
    print("\nProcessing logs...")
    executions = process_logs(report_results, path_map)
    print(f"  Processed {len(executions)} executions")

    if not executions:
        print("No executions to upload")
        return

    # Partition by date
    print("\nPartitioning by date...")
    partitioned = partition_logs_by_date(executions)
    print(f"  Logs span {len(partitioned)} dates")

    # Upload to S3
    print("\nUploading to S3...")
    total = upload_to_s3(partitioned)

    # Update Athena partitions
    update_athena_partitions()

    print("\n" + "="*70)
    print("✓ Backfill complete!")
    print("="*70)
    print(f"Total executions backfilled: {total}")
    print(f"Date range: {min(partitioned.keys())} to {max(partitioned.keys())}")
    print()
    print("You can now query all historical data in Grafana/Athena!")


if __name__ == '__main__':
    main()
