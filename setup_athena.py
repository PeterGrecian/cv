#!/usr/bin/env python3
"""
Set up Athena database and table for Lambda logs.
"""

import boto3
import time

REGION = 'eu-west-1'
S3_BUCKET = 'gardencam-berrylands-eu-west-1'
ATHENA_OUTPUT_LOCATION = f's3://{S3_BUCKET}/athena-query-results/'

def run_athena_query(query, database=None):
    """Execute an Athena query and wait for completion."""
    client = boto3.client('athena', region_name=REGION)

    params = {
        'QueryString': query,
        'ResultConfiguration': {
            'OutputLocation': ATHENA_OUTPUT_LOCATION,
        }
    }

    if database:
        params['QueryExecutionContext'] = {'Database': database}

    print(f"Executing query: {query[:100]}...")
    response = client.start_query_execution(**params)
    query_execution_id = response['QueryExecutionId']

    # Wait for query to complete
    while True:
        response = client.get_query_execution(QueryExecutionId=query_execution_id)
        status = response['QueryExecution']['Status']['State']

        if status in ['SUCCEEDED', 'FAILED', 'CANCELLED']:
            break

        time.sleep(1)

    if status == 'SUCCEEDED':
        print(f"  ✓ Query succeeded")
        return True
    else:
        reason = response['QueryExecution']['Status'].get('StateChangeReason', 'Unknown')
        print(f"  ✗ Query {status}: {reason}")
        return False


def main():
    print("Setting up Athena database and table...")
    print()

    # Create database
    print("1. Creating database 'lambda_logs'...")
    run_athena_query("CREATE DATABASE IF NOT EXISTS lambda_logs")

    # Create table
    print("\n2. Creating table 'execution_logs'...")
    create_table_query = """
CREATE EXTERNAL TABLE IF NOT EXISTS lambda_logs.execution_logs (
    timestamp STRING,
    request_id STRING,
    function_name STRING,
    duration_ms DOUBLE,
    memory_limit_mb DOUBLE,
    path STRING,
    estimated_cost_usd DOUBLE,
    date STRING
)
PARTITIONED BY (
    year STRING,
    month STRING,
    day STRING
)
ROW FORMAT SERDE 'org.openx.data.jsonserde.JsonSerDe'
LOCATION 's3://gardencam-berrylands-eu-west-1/lambda-logs-athena/'
TBLPROPERTIES ('has_encrypted_data'='false')
    """
    run_athena_query(create_table_query, database='lambda_logs')

    # Discover partitions automatically
    print("\n3. Discovering partitions...")
    run_athena_query("MSCK REPAIR TABLE lambda_logs.execution_logs", database='lambda_logs')

    # Test query
    print("\n4. Testing with a simple query...")
    test_query = "SELECT COUNT(*) as total_logs FROM lambda_logs.execution_logs"
    if run_athena_query(test_query, database='lambda_logs'):
        print("\n✓ Athena setup complete!")
        print(f"\nYou can now query your logs with SQL in Athena:")
        print(f"  Database: lambda_logs")
        print(f"  Table: execution_logs")
        print(f"\nExample queries:")
        print(f"  SELECT * FROM lambda_logs.execution_logs LIMIT 10;")
        print(f"  SELECT path, COUNT(*) as count FROM lambda_logs.execution_logs GROUP BY path;")
        print(f"  SELECT date, SUM(estimated_cost_usd)*1000000 as cost_microdollars FROM lambda_logs.execution_logs GROUP BY date;")


if __name__ == '__main__':
    main()
