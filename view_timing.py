#!/usr/bin/env python3
"""
View page load timing data from DynamoDB
"""
import boto3
from datetime import datetime
from decimal import Decimal

def view_timing_logs(limit=20):
    """Fetch and display recent page load timing logs."""
    dynamodb = boto3.resource('dynamodb', region_name='eu-west-1')
    table = dynamodb.Table('gardencam-page-timing')

    try:
        response = table.scan(Limit=limit)
        items = response.get('Items', [])

        # Sort by timestamp (newest first)
        items.sort(key=lambda x: x.get('timestamp', ''), reverse=True)

        if not items:
            print("No timing data yet. Load the gardencam page to generate logs.")
            return

        print(f"\n{'='*100}")
        print(f"Page Load Timing Logs (Latest {len(items)} entries)")
        print(f"{'='*100}\n")

        for idx, item in enumerate(items, 1):
            timestamp = item.get('timestamp', 'unknown')
            page_load = float(item.get('page_load_ms', 0))
            dom_ready = float(item.get('dom_ready_ms', 0))
            server_response = float(item.get('server_response_ms', 0))
            ip = item.get('ip', 'unknown')
            user_agent = item.get('user_agent', '')[:80]

            # Parse timestamp
            try:
                dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                time_str = dt.strftime('%Y-%m-%d %H:%M:%S')
            except:
                time_str = timestamp

            print(f"{idx}. {time_str}")
            print(f"   Total Page Load:    {page_load:>7,.0f} ms ({page_load/1000:.2f}s)")
            print(f"   DOM Ready:          {dom_ready:>7,.0f} ms ({dom_ready/1000:.2f}s)")
            print(f"   Server Response:    {server_response:>7,.0f} ms ({server_response/1000:.2f}s)")
            print(f"   Image Load Time:    {(page_load - dom_ready):>7,.0f} ms ({(page_load - dom_ready)/1000:.2f}s)")
            print(f"   IP: {ip}")
            print(f"   User Agent: {user_agent}")
            print()

        # Calculate averages
        avg_load = sum(float(i.get('page_load_ms', 0)) for i in items) / len(items)
        avg_dom = sum(float(i.get('dom_ready_ms', 0)) for i in items) / len(items)
        avg_server = sum(float(i.get('server_response_ms', 0)) for i in items) / len(items)

        print(f"{'='*100}")
        print(f"AVERAGES (from {len(items)} loads):")
        print(f"  Page Load:    {avg_load:>7,.0f} ms ({avg_load/1000:.2f}s)")
        print(f"  DOM Ready:    {avg_dom:>7,.0f} ms ({avg_dom/1000:.2f}s)")
        print(f"  Server:       {avg_server:>7,.0f} ms ({avg_server/1000:.2f}s)")
        print(f"  Images:       {(avg_load - avg_dom):>7,.0f} ms ({(avg_load - avg_dom)/1000:.2f}s)")
        print(f"{'='*100}\n")

    except Exception as e:
        print(f"Error fetching timing logs: {e}")

if __name__ == "__main__":
    view_timing_logs()
