#!/usr/bin/env python3
"""
Backfill Lambda execution logs from CloudWatch Logs to DynamoDB.
FIXED: Corrects memory unit conversion (bytes → MB).
"""

import boto3
import time
from datetime import datetime, timedelta
from decimal import Decimal

REGION = 'eu-west-1'
LOG_GROUP = '/aws/lambda/cvdev'
DYNAMODB_TABLE = 'lambda-execution-logs'


def query_cloudwatch_logs(start_time, end_time):
    """Query CloudWatch Logs for Lambda executions using Logs Insights."""
    client = boto3.client('logs', region_name=REGION)

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

    while True:
        result = client.get_query_results(queryId=query_id)
        status = result['status']

        if status == 'Complete':
            break
        elif status in ['Failed', 'Cancelled']:
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
            try:
                path = message.split('path = ')[1].split(',')[0].strip()
                path_map[request_id] = path
            except:
                pass

    print(f"  Found paths for {len(path_map)} requests")
    return path_map


def parse_report_line(fields):
    """Parse REPORT line fields into structured data.

    FIXED: CloudWatch returns memory in BYTES, not MB!
    - @memorySize is in bytes (128 MB = 134,217,728 bytes)
    - @maxMemoryUsed is in bytes
    """
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
            # FIX: Convert bytes to MB
            data['memory_limit_mb'] = float(field_value) / (1024 * 1024)
        elif field_name == '@maxMemoryUsed':
            # FIX: Convert bytes to MB
            data['max_memory_used_mb'] = float(field_value) / (1024 * 1024)

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


def write_to_dynamodb(executions, dry_run=False):
    """Write execution records to DynamoDB."""
    if dry_run:
        print(f"\n[DRY RUN] Would write {len(executions)} records to DynamoDB")
        return 0

    dynamodb = boto3.resource('dynamodb', region_name=REGION)
    table = dynamodb.Table(DYNAMODB_TABLE)

    written = 0
    skipped = 0

    print(f"\nWriting to DynamoDB...")

    # Use batch write for efficiency
    with table.batch_writer() as batch:
        for execution in executions:
            try:
                # Convert to DynamoDB types
                item = {
                    'timestamp': execution['timestamp'],
                    'request_id': execution['request_id'],
                    'function_name': execution['function_name'],
                    'duration_ms': Decimal(str(execution['duration_ms'])),
                    'memory_limit_mb': Decimal(str(execution['memory_limit_mb'])),
                    'path': execution.get('path', ''),
                    'estimated_cost_usd': execution['estimated_cost_usd']
                }

                batch.put_item(Item=item)
                written += 1

                if written % 100 == 0:
                    print(f"  Written {written} records...")

            except Exception as e:
                print(f"  Error writing record {execution['request_id']}: {e}")
                skipped += 1

    print(f"  ✓ Wrote {written} records")
    if skipped > 0:
        print(f"  ⚠ Skipped {skipped} records due to errors")

    return written


def main():
    # Query last 180 days (cvdev retention period)
    end_time = datetime.utcnow()
    start_time = end_time - timedelta(days=180)

    print("="*70)
    print("CloudWatch Logs → DynamoDB Backfill (FIXED)")
    print("="*70)
    print(f"Log Group: {LOG_GROUP}")
    print(f"DynamoDB Table: {DYNAMODB_TABLE}")
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
    executions = []

    for row in report_results:
        data = parse_report_line(row)

        if not data.get('request_id'):
            continue

        # Add path from path_map
        data['path'] = path_map.get(data['request_id'], '')

        # Add function name
        data['function_name'] = 'cvdev'

        # Calculate cost (now with correct memory values!)
        duration_ms = data.get('duration_ms', 0)
        memory_mb = data.get('memory_limit_mb', 128)
        data['estimated_cost_usd'] = calculate_cost(duration_ms, memory_mb)

        executions.append(data)

    print(f"  Processed {len(executions)} executions")

    if not executions:
        print("No executions to write")
        return

    # Show sample of corrected data
    print("\n" + "="*70)
    print("SAMPLE OF CORRECTED DATA (first 3 records)")
    print("="*70)
    for i, exec in enumerate(executions[:3]):
        print(f"\nRecord {i+1}:")
        print(f"  Timestamp: {exec['timestamp']}")
        print(f"  Duration: {exec['duration_ms']:.2f}ms")
        print(f"  Memory Limit: {exec['memory_limit_mb']:.2f}MB (FIXED!)")
        if 'max_memory_used_mb' in exec:
            print(f"  Max Memory Used: {exec['max_memory_used_mb']:.2f}MB (FIXED!)")
        print(f"  Cost: {float(exec['estimated_cost_usd'])*1_000_000:.4f} µ$ (FIXED!)")
        print(f"  Path: {exec.get('path', '(empty)')}")

    # Calculate total cost
    total_cost_microdollars = sum(float(e['estimated_cost_usd']) for e in executions) * 1_000_000
    print(f"\nTotal estimated cost: {total_cost_microdollars:.2f} µ$")

    # Ask for confirmation
    print("\n" + "="*70)
    response = input(f"Write {len(executions)} records to DynamoDB? [y/N]: ")

    if response.lower() != 'y':
        print("Aborted.")
        return

    # Write to DynamoDB
    written = write_to_dynamodb(executions)

    print("\n" + "="*70)
    print("✓ Backfill complete!")
    print("="*70)
    print(f"Total executions backfilled: {written}")
    print(f"Date range: {min(e['timestamp'] for e in executions)}")
    print(f"         to {max(e['timestamp'] for e in executions)}")
    print()
    print("You can now read all data from DynamoDB in Jupyter!")


if __name__ == '__main__':
    main()
