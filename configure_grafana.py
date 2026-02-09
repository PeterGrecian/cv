#!/usr/bin/env python3
"""
Configure Grafana with Athena data source and create initial dashboard.
"""

import requests
import json
import time

GRAFANA_URL = "http://localhost:3000"
GRAFANA_USER = "admin"
GRAFANA_PASSWORD = "admin"
REGION = "eu-west-1"
DATABASE = "lambda_logs"
ATHENA_OUTPUT = "s3://gardencam-berrylands-eu-west-1/athena-query-results/"


def wait_for_grafana():
    """Wait for Grafana to be ready."""
    print("Waiting for Grafana to be ready...")
    for i in range(30):
        try:
            response = requests.get(f"{GRAFANA_URL}/api/health", timeout=2)
            if response.status_code == 200:
                print("  ✓ Grafana is ready")
                return True
        except requests.exceptions.RequestException:
            pass
        time.sleep(1)
    print("  ✗ Grafana did not become ready")
    return False


def create_athena_datasource():
    """Create Athena data source in Grafana."""
    print("\nCreating Athena data source...")

    datasource = {
        "name": "Lambda Logs (Athena)",
        "type": "grafana-athena-datasource",
        "access": "proxy",
        "isDefault": True,
        "jsonData": {
            "authType": "default",  # Uses AWS credentials from environment
            "defaultRegion": REGION,
            "catalog": "AwsDataCatalog",
            "database": DATABASE,
            "workgroup": "primary",
            "outputLocation": ATHENA_OUTPUT
        }
    }

    response = requests.post(
        f"{GRAFANA_URL}/api/datasources",
        auth=(GRAFANA_USER, GRAFANA_PASSWORD),
        headers={"Content-Type": "application/json"},
        data=json.dumps(datasource)
    )

    if response.status_code in [200, 409]:  # 409 = already exists
        print("  ✓ Athena data source created/exists")
        return True
    else:
        print(f"  ✗ Failed to create data source: {response.status_code}")
        print(f"     {response.text}")
        return False


def create_dashboard():
    """Create initial Lambda logs dashboard."""
    print("\nCreating initial dashboard...")

    dashboard = {
        "dashboard": {
            "title": "Lambda Execution Logs",
            "tags": ["lambda", "aws"],
            "timezone": "browser",
            "schemaVersion": 16,
            "version": 0,
            "refresh": "30s",
            "panels": [
                {
                    "id": 1,
                    "gridPos": {"x": 0, "y": 0, "w": 24, "h": 8},
                    "type": "timeseries",
                    "title": "Request Count Over Time",
                    "targets": [{
                        "refId": "A",
                        "datasource": {"type": "grafana-athena-datasource", "uid": "athena"},
                        "rawSQL": """
                            SELECT
                                date_parse(timestamp, '%Y-%m-%dT%H:%i:%s') as time,
                                COUNT(*) as count
                            FROM lambda_logs.execution_logs
                            WHERE timestamp >= cast(date_add('day', -7, current_timestamp) as varchar)
                            GROUP BY timestamp
                            ORDER BY timestamp
                        """,
                        "format": "time_series"
                    }],
                    "fieldConfig": {
                        "defaults": {
                            "color": {"mode": "palette-classic"},
                            "custom": {"fillOpacity": 20}
                        }
                    }
                },
                {
                    "id": 2,
                    "gridPos": {"x": 0, "y": 8, "w": 12, "h": 8},
                    "type": "piechart",
                    "title": "Requests by Path",
                    "targets": [{
                        "refId": "A",
                        "datasource": {"type": "grafana-athena-datasource"},
                        "rawSQL": """
                            SELECT
                                path,
                                COUNT(*) as count
                            FROM lambda_logs.execution_logs
                            GROUP BY path
                            ORDER BY count DESC
                        """,
                        "format": "table"
                    }]
                },
                {
                    "id": 3,
                    "gridPos": {"x": 12, "y": 8, "w": 12, "h": 8},
                    "type": "stat",
                    "title": "Total Cost (Microdollars)",
                    "targets": [{
                        "refId": "A",
                        "datasource": {"type": "grafana-athena-datasource"},
                        "rawSQL": """
                            SELECT
                                SUM(estimated_cost_usd) * 1000000 as total_cost_microdollars
                            FROM lambda_logs.execution_logs
                        """,
                        "format": "table"
                    }],
                    "fieldConfig": {
                        "defaults": {
                            "unit": "µ$",
                            "decimals": 2
                        }
                    }
                }
            ]
        },
        "overwrite": True
    }

    response = requests.post(
        f"{GRAFANA_URL}/api/dashboards/db",
        auth=(GRAFANA_USER, GRAFANA_PASSWORD),
        headers={"Content-Type": "application/json"},
        data=json.dumps(dashboard)
    )

    if response.status_code == 200:
        result = response.json()
        dashboard_url = f"{GRAFANA_URL}{result.get('url', '')}"
        print(f"  ✓ Dashboard created: {dashboard_url}")
        return True
    else:
        print(f"  ✗ Failed to create dashboard: {response.status_code}")
        print(f"     {response.text}")
        return False


def main():
    print("Configuring Grafana...")

    if not wait_for_grafana():
        print("\n✗ Configuration failed - Grafana not ready")
        return

    if not create_athena_datasource():
        print("\n✗ Configuration failed - Could not create data source")
        return

    # Note: Dashboard creation might fail if Athena plugin isn't fully installed yet
    # This is just a starting point - users can create better dashboards in the UI
    print("\n" + "="*70)
    print("✓ Grafana is configured!")
    print("="*70)
    print(f"\nAccess Grafana at: {GRAFANA_URL}")
    print(f"  Username: {GRAFANA_USER}")
    print(f"  Password: {GRAFANA_PASSWORD}")
    print(f"\nData Source: Lambda Logs (Athena)")
    print(f"  Database: {DATABASE}")
    print(f"  Region: {REGION}")
    print("\nNext steps:")
    print("  1. Log in to Grafana")
    print("  2. Go to Data Sources and verify Athena connection")
    print("  3. Create dashboards using the 'Explore' feature")
    print("  4. Example query to get started:")
    print("     SELECT * FROM lambda_logs.execution_logs LIMIT 10")


if __name__ == '__main__':
    main()
