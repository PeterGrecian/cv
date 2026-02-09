-- Create database if it doesn't exist
CREATE DATABASE IF NOT EXISTS lambda_logs;

-- Create table for Lambda execution logs with date partitioning
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
TBLPROPERTIES ('has_encrypted_data'='false');

-- Add partitions for existing data
-- These need to be run after the table is created
-- You can also use MSCK REPAIR TABLE to auto-discover partitions, but explicit is more reliable

ALTER TABLE lambda_logs.execution_logs ADD IF NOT EXISTS
PARTITION (year='2026', month='01', day='27')
LOCATION 's3://gardencam-berrylands-eu-west-1/lambda-logs-athena/year=2026/month=01/day=27/';

ALTER TABLE lambda_logs.execution_logs ADD IF NOT EXISTS
PARTITION (year='2026', month='02', day='09')
LOCATION 's3://gardencam-berrylands-eu-west-1/lambda-logs-athena/year=2026/month=02/day=09/';
