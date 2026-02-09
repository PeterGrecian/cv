# Grafana + Athena Setup for Lambda Logs

## What's Been Set Up

You now have a complete analytics stack for your Lambda execution logs:

1. **DynamoDB → S3 Export** - Your logs are exported to S3 in a partitioned structure
2. **Athena Database** - SQL queries against your logs in S3
3. **Grafana** - Powerful visualization and exploration UI
4. **Automatic Updates** - Scripts to keep data in sync

## Architecture

```
DynamoDB (lambda-execution-logs)
    ↓
S3 (s3://gardencam-berrylands-eu-west-1/lambda-logs-athena/)
    ↓
Athena (lambda_logs.execution_logs)
    ↓
Grafana (http://localhost:3000)
```

## Access Grafana

**URL**: http://localhost:3000
**Username**: admin
**Password**: admin

## Quick Start

### 1. Explore Your Data

1. Open Grafana at http://localhost:3000
2. Click "Explore" (compass icon on left sidebar)
3. Select "Lambda Logs (Athena)" data source
4. Click "Code" to write SQL queries

### 2. Example Queries

**Count all logs:**
```sql
SELECT COUNT(*) as total FROM lambda_logs.execution_logs
```

**Requests by path:**
```sql
SELECT
    path,
    COUNT(*) as count,
    AVG(duration_ms) as avg_duration_ms,
    SUM(estimated_cost_usd) * 1000000 as cost_microdollars
FROM lambda_logs.execution_logs
GROUP BY path
ORDER BY count DESC
```

**Time series of requests (last 7 days):**
```sql
SELECT
    date_parse(timestamp, '%Y-%m-%dT%H:%i:%s.%fZ') as time,
    COUNT(*) as count
FROM lambda_logs.execution_logs
WHERE timestamp >= cast(date_add('day', -7, current_timestamp) as varchar)
GROUP BY timestamp
ORDER BY timestamp
```

**Daily cost breakdown:**
```sql
SELECT
    date as day,
    COUNT(*) as requests,
    SUM(estimated_cost_usd) * 1000000 as cost_microdollars,
    AVG(duration_ms) as avg_duration_ms
FROM lambda_logs.execution_logs
GROUP BY date
ORDER BY date DESC
```

### 3. Create Dashboards

1. Click "Dashboards" → "New Dashboard"
2. Add panels with different visualizations
3. Use the query builder or write SQL directly
4. Save your dashboard

**Panel Types Available:**
- Time series (line/area charts)
- Bar charts
- Pie charts
- Tables
- Stats (single numbers)
- Heatmaps
- And many more!

## Updating Data

### Manual Export

Run this whenever you want to update S3 with latest DynamoDB data:

```bash
cd /home/tot/cv
python3 export_logs_to_s3.py
```

### After Export: Update Partitions

After exporting new data, tell Athena to discover new partitions:

```python
import boto3
client = boto3.client('athena', region_name='eu-west-1')
client.start_query_execution(
    QueryString='MSCK REPAIR TABLE lambda_logs.execution_logs',
    QueryExecutionContext={'Database': 'lambda_logs'},
    ResultConfiguration={
        'OutputLocation': 's3://gardencam-berrylands-eu-west-1/athena-query-results/'
    }
)
```

Or via AWS CLI:
```bash
aws athena start-query-execution \
    --query-string "MSCK REPAIR TABLE lambda_logs.execution_logs" \
    --query-execution-context Database=lambda_logs \
    --result-configuration OutputLocation=s3://gardencam-berrylands-eu-west-1/athena-query-results/ \
    --region eu-west-1
```

### Automated Updates (Future Enhancement)

You could set up:
1. **Lambda function** to automatically export on a schedule
2. **S3 event** to trigger partition updates
3. **DynamoDB Stream** for real-time updates

## Files Created

- `export_logs_to_s3.py` - Export DynamoDB to S3
- `setup_athena.py` - Create Athena database and table
- `configure_grafana.py` - Configure Grafana data source
- `athena_create_table.sql` - SQL definitions (for reference)
- `docker-compose-grafana.yml` - Docker compose file (for reference)

## Managing Grafana

**Start Grafana:**
```bash
docker start grafana-lambda-logs
```

**Stop Grafana:**
```bash
docker stop grafana-lambda-logs
```

**View logs:**
```bash
docker logs grafana-lambda-logs
```

**Remove and recreate:**
```bash
docker rm -f grafana-lambda-logs
docker run -d --name grafana-lambda-logs -p 3000:3000 \
  -v ~/.aws:/usr/share/grafana/.aws:ro \
  -e "GF_SECURITY_ADMIN_PASSWORD=admin" \
  -e "GF_INSTALL_PLUGINS=grafana-athena-datasource" \
  grafana/grafana:latest
```

## Data Structure

### S3 Layout
```
s3://gardencam-berrylands-eu-west-1/lambda-logs-athena/
  year=2026/
    month=01/
      day=27/
        data.json
    month=02/
      day=09/
        data.json
```

### Athena Table Schema
- `timestamp` - ISO 8601 timestamp
- `request_id` - AWS request ID
- `function_name` - Lambda function name
- `duration_ms` - Execution duration in milliseconds
- `memory_limit_mb` - Memory allocated to function
- `path` - Request path (e.g., /t3, /lambda-stats)
- `estimated_cost_usd` - Estimated cost in USD
- `date` - Date string (YYYY-MM-DD)

## Cost Considerations

**S3 Storage:**
- ~62 logs = ~15KB
- 100 logs/day = ~24KB/day = ~9MB/year
- Cost: Negligible (~$0.0002/year)

**Athena Queries:**
- $5 per TB scanned
- Your 62 logs = ~15KB = $0.000000075 per full scan
- 1000 queries = ~$0.00008
- Cost: Essentially free

**Grafana:**
- Running locally: Free
- Uses minimal resources (200MB RAM)

**Total cost: ~$0/month** for your volume!

## Grafana Tips

### Time Range Picker
- Top right corner - adjust time range for all panels
- "Last 7 days", "Last 30 days", custom ranges

### Variables
- Create dashboard variables for filtering (e.g., path, function_name)
- Use in queries: `WHERE path = '$path'`

### Annotations
- Mark deployments or incidents on charts
- Helps correlate changes with metrics

### Alerts
- Set up alerts for cost thresholds
- Email/Slack notifications when errors spike

### Sharing
- Export dashboards as JSON
- Share with team or across Grafana instances

## Troubleshooting

**Athena queries returning no data:**
- Run `MSCK REPAIR TABLE lambda_logs.execution_logs` to update partitions
- Check S3 data exists: `aws s3 ls s3://gardencam-berrylands-eu-west-1/lambda-logs-athena/ --recursive`

**Grafana can't connect to Athena:**
- Check AWS credentials: `ls -la ~/.aws/`
- Verify credentials work: `aws athena list-databases --catalog-name AwsDataCatalog --region eu-west-1`
- Check Docker volume mount: `docker inspect grafana-lambda-logs | grep aws`

**Plugin not installed:**
```bash
docker exec -it grafana-lambda-logs grafana-cli plugins install grafana-athena-datasource
docker restart grafana-lambda-logs
```

## Next Steps

1. **Create your first dashboard** with the metrics you care about
2. **Set up automated exports** to keep data fresh
3. **Add more data sources** (CloudWatch metrics, application logs)
4. **Explore Grafana features** (alerting, annotations, variables)

## Resources

- [Grafana Documentation](https://grafana.com/docs/grafana/latest/)
- [Athena SQL Reference](https://docs.aws.amazon.com/athena/latest/ug/ddl-sql-reference.html)
- [Grafana Athena Plugin](https://grafana.com/grafana/plugins/grafana-athena-datasource/)

---

Enjoy your powerful Lambda analytics platform! 🚀
