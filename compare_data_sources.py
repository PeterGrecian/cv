#!/usr/bin/env python3
"""
Compare data collected from CloudWatch vs DynamoDB logging.
"""

import boto3
import json
import pandas as pd
from datetime import datetime

REGION = 'eu-west-1'
DYNAMODB_TABLE = 'lambda-execution-logs'
S3_BUCKET = 'gardencam-berrylands-eu-west-1'
S3_PREFIX = 'lambda-logs-athena/'


def load_dynamodb_logs():
    """Load logs from DynamoDB."""
    dynamodb = boto3.resource('dynamodb', region_name=REGION)
    table = dynamodb.Table(DYNAMODB_TABLE)

    print("Loading from DynamoDB...")
    response = table.scan()
    logs = response.get('Items', [])

    while 'LastEvaluatedKey' in response:
        response = table.scan(ExclusiveStartKey=response['LastEvaluatedKey'])
        logs.extend(response.get('Items', []))

    print(f"  ✓ Loaded {len(logs)} records from DynamoDB")
    return pd.DataFrame(logs)


def load_s3_logs():
    """Load logs from S3 (CloudWatch-sourced)."""
    s3 = boto3.client('s3', region_name=REGION)

    print("Loading from S3 (CloudWatch backfill)...")
    paginator = s3.get_paginator('list_objects_v2')
    pages = paginator.paginate(Bucket=S3_BUCKET, Prefix=S3_PREFIX)

    all_logs = []
    for page in pages:
        if 'Contents' not in page:
            continue

        for obj in page['Contents']:
            key = obj['Key']
            if not key.endswith('.json'):
                continue

            response = s3.get_object(Bucket=S3_BUCKET, Key=key)
            content = response['Body'].read().decode('utf-8')

            for line in content.strip().split('\n'):
                if line:
                    all_logs.append(json.loads(line))

    print(f"  ✓ Loaded {len(all_logs)} records from S3")
    return pd.DataFrame(all_logs)


def compare_schemas(df_dynamo, df_s3):
    """Compare available fields in each dataset."""
    print("\n" + "="*70)
    print("SCHEMA COMPARISON")
    print("="*70)

    dynamo_cols = set(df_dynamo.columns)
    s3_cols = set(df_s3.columns)

    print("\n📊 DynamoDB columns:")
    for col in sorted(dynamo_cols):
        print(f"  - {col}")

    print("\n📊 CloudWatch/S3 columns:")
    for col in sorted(s3_cols):
        print(f"  - {col}")

    print("\n🔍 Columns ONLY in DynamoDB:")
    only_dynamo = dynamo_cols - s3_cols
    for col in sorted(only_dynamo):
        print(f"  - {col}")

    print("\n🔍 Columns ONLY in CloudWatch/S3:")
    only_s3 = s3_cols - dynamo_cols
    for col in sorted(only_s3):
        print(f"  - {col}")

    print("\n✓ Common columns:")
    common = dynamo_cols & s3_cols
    for col in sorted(common):
        print(f"  - {col}")


def compare_data_quality(df_dynamo, df_s3):
    """Compare data quality and completeness."""
    print("\n" + "="*70)
    print("DATA QUALITY COMPARISON")
    print("="*70)

    # Path field completeness
    print("\n📍 Path field completeness:")

    dynamo_paths = df_dynamo['path'].notna().sum() if 'path' in df_dynamo.columns else 0
    dynamo_total = len(df_dynamo)
    print(f"  DynamoDB: {dynamo_paths}/{dynamo_total} ({dynamo_paths/dynamo_total*100:.1f}%) have path")

    s3_paths = df_s3['path'].notna().sum() if 'path' in df_s3.columns else 0
    s3_total = len(df_s3)
    # Count non-empty paths
    s3_non_empty = (df_s3['path'] != '').sum() if 'path' in df_s3.columns else 0
    print(f"  CloudWatch: {s3_non_empty}/{s3_total} ({s3_non_empty/s3_total*100:.1f}%) have path")

    # Duration statistics
    print("\n⏱️  Duration comparison:")
    if 'duration_ms' in df_dynamo.columns:
        print(f"  DynamoDB - Mean: {df_dynamo['duration_ms'].mean():.2f}ms, "
              f"Median: {df_dynamo['duration_ms'].median():.2f}ms, "
              f"Max: {df_dynamo['duration_ms'].max():.2f}ms")

    if 'duration_ms' in df_s3.columns:
        print(f"  CloudWatch - Mean: {df_s3['duration_ms'].mean():.2f}ms, "
              f"Median: {df_s3['duration_ms'].median():.2f}ms, "
              f"Max: {df_s3['duration_ms'].max():.2f}ms")

    # Cost comparison
    print("\n💰 Cost comparison:")
    if 'estimated_cost_usd' in df_dynamo.columns:
        total_cost_dynamo = float(df_dynamo['estimated_cost_usd'].sum()) * 1_000_000
        print(f"  DynamoDB - Total: {total_cost_dynamo:.2f} µ$")

    if 'estimated_cost_usd' in df_s3.columns:
        total_cost_s3 = float(df_s3['estimated_cost_usd'].sum()) * 1_000_000
        print(f"  CloudWatch - Total: {total_cost_s3:.2f} µ$")


def compare_samples(df_dynamo, df_s3):
    """Show sample records from each source."""
    print("\n" + "="*70)
    print("SAMPLE RECORDS")
    print("="*70)

    print("\n📝 DynamoDB sample (first 3 records):")
    if len(df_dynamo) > 0:
        df_dynamo['timestamp'] = pd.to_datetime(df_dynamo['timestamp'])
        sample = df_dynamo.sort_values('timestamp').head(3)
        for idx, row in sample.iterrows():
            print(f"\n  Record {idx + 1}:")
            for col in sorted(row.index):
                val = row[col]
                if pd.notna(val):
                    print(f"    {col}: {val}")

    print("\n\n📝 CloudWatch/S3 sample (first 3 records):")
    if len(df_s3) > 0:
        df_s3['timestamp'] = pd.to_datetime(df_s3['timestamp'])
        sample = df_s3.sort_values('timestamp').head(3)
        for idx, row in sample.iterrows():
            print(f"\n  Record {idx + 1}:")
            for col in sorted(row.index):
                val = row[col]
                if pd.notna(val):
                    print(f"    {col}: {val}")


def compare_date_ranges(df_dynamo, df_s3):
    """Compare date ranges covered by each source."""
    print("\n" + "="*70)
    print("DATE RANGE COMPARISON")
    print("="*70)

    df_dynamo['timestamp'] = pd.to_datetime(df_dynamo['timestamp'])
    df_s3['timestamp'] = pd.to_datetime(df_s3['timestamp'])

    print(f"\n📅 DynamoDB:")
    print(f"  Earliest: {df_dynamo['timestamp'].min()}")
    print(f"  Latest: {df_dynamo['timestamp'].max()}")
    print(f"  Span: {(df_dynamo['timestamp'].max() - df_dynamo['timestamp'].min()).days} days")

    print(f"\n📅 CloudWatch/S3:")
    print(f"  Earliest: {df_s3['timestamp'].min()}")
    print(f"  Latest: {df_s3['timestamp'].max()}")
    print(f"  Span: {(df_s3['timestamp'].max() - df_s3['timestamp'].min()).days} days")

    # Find overlap
    dynamo_min = df_dynamo['timestamp'].min()
    dynamo_max = df_dynamo['timestamp'].max()
    s3_min = df_s3['timestamp'].min()
    s3_max = df_s3['timestamp'].max()

    overlap_start = max(dynamo_min, s3_min)
    overlap_end = min(dynamo_max, s3_max)

    if overlap_start < overlap_end:
        print(f"\n🔗 Overlap period:")
        print(f"  {overlap_start} to {overlap_end}")
        print(f"  Duration: {(overlap_end - overlap_start).days} days")

        # Count records in overlap
        dynamo_overlap = df_dynamo[
            (df_dynamo['timestamp'] >= overlap_start) &
            (df_dynamo['timestamp'] <= overlap_end)
        ]
        s3_overlap = df_s3[
            (df_s3['timestamp'] >= overlap_start) &
            (df_s3['timestamp'] <= overlap_end)
        ]

        print(f"\n  DynamoDB records in overlap: {len(dynamo_overlap)}")
        print(f"  CloudWatch records in overlap: {len(s3_overlap)}")
    else:
        print("\n⚠️  No overlap between datasets!")


def main():
    print("="*70)
    print("CLOUDWATCH vs DYNAMODB DATA COMPARISON")
    print("="*70)

    # Load data
    df_dynamo = load_dynamodb_logs()
    df_s3 = load_s3_logs()

    # Run comparisons
    compare_schemas(df_dynamo, df_s3)
    compare_date_ranges(df_dynamo, df_s3)
    compare_data_quality(df_dynamo, df_s3)
    compare_samples(df_dynamo, df_s3)

    print("\n" + "="*70)
    print("SUMMARY")
    print("="*70)
    print(f"\n✓ DynamoDB: {len(df_dynamo)} records")
    print(f"✓ CloudWatch: {len(df_s3)} records")
    print(f"\nCloudWatch has {len(df_s3) - len(df_dynamo)} more records")
    print(f"That's {len(df_s3) / len(df_dynamo):.1f}x more data!\n")


if __name__ == '__main__':
    main()
