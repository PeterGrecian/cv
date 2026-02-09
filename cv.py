from pprint import pformat
import os
import base64
import urllib.request
from io import BytesIO
from datetime import datetime, timezone

try:
    import boto3
    import json
    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False

GARDENCAM_BUCKET = "gardencam-berrylands-eu-west-1"
GARDENCAM_REGION = "eu-west-1"
GARDENCAM_SECRET_NAME = "gardencam/password"
GARDENCAM_PASSWORD = None
TFL_SECRET_NAME = "tfl/api-key"
TFL_API_KEY = None
DYNAMODB_TABLE = "cv-access-logs"

# Memspeed configuration - uses same bucket with memspeed/ prefix
MEMSPEED_PREFIX = "memspeed/"
MEMSPEED_RESULTS_PREFIX = "memspeed/results/"
MEMSPEED_DOWNLOADS_PREFIX = "memspeed/downloads/"


def get_secret(secret_name, key='password'):
    """Retrieve a secret from AWS Secrets Manager."""
    if not BOTO3_AVAILABLE:
        print(f"WARNING: boto3 not available. Cannot retrieve secret: {secret_name}")
        return None

    try:
        client = boto3.client('secretsmanager', region_name=GARDENCAM_REGION)
        response = client.get_secret_value(SecretId=secret_name)

        if 'SecretString' in response:
            secret = json.loads(response['SecretString'])
            return secret.get(key)

        return None
    except Exception as e:
        print(f"ERROR: Failed to retrieve secret {secret_name}: {str(e)}")
        return None


# Initialize secrets from Secrets Manager on cold start
GARDENCAM_PASSWORD = get_secret(GARDENCAM_SECRET_NAME, 'password')
if not GARDENCAM_PASSWORD:
    print(f"WARNING: Could not retrieve password from Secrets Manager ({GARDENCAM_SECRET_NAME}). Gardencam will be inaccessible.")

TFL_API_KEY = get_secret(TFL_SECRET_NAME, 'api_key')
if TFL_API_KEY:
    print("TfL API key loaded from Secrets Manager")
else:
    print("WARNING: No TfL API key found. Using unauthenticated access (rate limited).")


def check_basic_auth(event, required_password):
    """Check HTTP Basic Authentication. Returns True if authorized, False otherwise."""
    headers = event.get('headers', {})

    # API Gateway may lowercase headers, so check both cases
    auth_header = headers.get('Authorization') or headers.get('authorization', '')

    if not auth_header.startswith('Basic '):
        return False

    try:
        # Decode base64 credentials
        encoded_credentials = auth_header[6:]  # Remove 'Basic ' prefix
        decoded = base64.b64decode(encoded_credentials).decode('utf-8')
        username, password = decoded.split(':', 1)

        # Check password (username can be anything)
        return password == required_password
    except (ValueError, UnicodeDecodeError):
        return False


def log_connection(event, context):
    """Log connection details to DynamoDB."""
    if not BOTO3_AVAILABLE:
        return

    try:
        dynamodb = boto3.resource('dynamodb', region_name=GARDENCAM_REGION)
        table = dynamodb.Table(DYNAMODB_TABLE)

        headers = event.get('headers', {})
        timestamp = datetime.utcnow().isoformat()

        # Get user agent (check both cases due to API Gateway)
        user_agent = headers.get('User-Agent') or headers.get('user-agent', 'Unknown')

        item = {
            'timestamp': timestamp,
            'request_id': context.request_id,
            'path': event.get('path', ''),
            'ip': headers.get('X-Forwarded-For', 'Unknown'),
            'user_agent': user_agent,
            'referer': headers.get('referer') or headers.get('Referer', ''),
            'stage': event.get('requestContext', {}).get('stage', ''),
            'host': headers.get('Host', '')
        }

        table.put_item(Item=item)
    except Exception as e:
        print(f"Failed to log connection: {str(e)}")


def log_execution_metrics(context, duration_ms, path=''):
    """Log Lambda execution metrics to DynamoDB."""
    if not BOTO3_AVAILABLE:
        return

    try:
        from decimal import Decimal
        dynamodb = boto3.resource('dynamodb', region_name=GARDENCAM_REGION)
        table = dynamodb.Table('lambda-execution-logs')

        timestamp = datetime.utcnow().isoformat()
        function_name = context.function_name

        # Calculate estimated cost (pricing as of 2024)
        # Memory: $0.0000166667 per GB-second
        # Requests: $0.20 per 1M requests
        memory_gb = Decimal(str(context.memory_limit_in_mb)) / Decimal('1024')
        duration_seconds = Decimal(str(duration_ms)) / Decimal('1000')
        memory_cost = memory_gb * duration_seconds * Decimal('0.0000166667')
        request_cost = Decimal('0.0000002')  # $0.20 per 1M requests
        total_cost = memory_cost + request_cost

        item = {
            'function_name': function_name,
            'timestamp': timestamp,
            'request_id': context.request_id,
            'duration_ms': Decimal(str(duration_ms)),
            'memory_limit_mb': Decimal(str(context.memory_limit_in_mb)),
            'path': path,
            'estimated_cost_usd': total_cost
        }

        table.put_item(Item=item)
    except Exception as e:
        print(f"Failed to log execution metrics: {str(e)}")


def parse_timestamp_from_key(key):
    """Extract timestamp from filename: garden_YYYYMMDD_HHMMSS.jpg"""
    try:
        filename_parts = key.replace('.jpg', '').split('_')
        if len(filename_parts) >= 3:
            date_str = filename_parts[1]
            time_str = filename_parts[2]
            return f"{date_str[:4]}-{date_str[4:6]}-{date_str[6:8]} {time_str[:2]}:{time_str[2:4]}:{time_str[4:6]}"
    except:
        pass
    return None


def get_presigned_url(key, expires_in=3600):
    """Generate presigned URL for a specific S3 key."""
    if not BOTO3_AVAILABLE:
        return None
    s3 = boto3.client("s3", region_name=GARDENCAM_REGION)
    return s3.generate_presigned_url(
        'get_object',
        Params={'Bucket': GARDENCAM_BUCKET, 'Key': key},
        ExpiresIn=expires_in
    )


def get_all_gardencam_images():
    """Get all gardencam images from S3."""
    if not BOTO3_AVAILABLE:
        return []
    s3 = boto3.client("s3", region_name=GARDENCAM_REGION)

    # List objects with garden_ prefix (excludes thumbnails and averaged images)
    # Use paginator to handle >1000 objects
    paginator = s3.get_paginator('list_objects_v2')
    pages = paginator.paginate(Bucket=GARDENCAM_BUCKET, Prefix='garden_')

    all_objects = []
    for page in pages:
        if "Contents" in page:
            all_objects.extend(page["Contents"])

    if not all_objects:
        return []

    # Sort by Key (filename contains timestamp), newest first
    objects = sorted(all_objects, key=lambda x: x["Key"], reverse=True)
    images = []

    for obj in objects:
        key = obj["Key"]
        # Only include .jpg files
        if not key.endswith('.jpg'):
            continue

        timestamp = parse_timestamp_from_key(key) or obj["LastModified"].strftime("%Y-%m-%d %H:%M:%S")

        images.append({
            'key': key,
            'timestamp': timestamp,
            'last_modified': obj["LastModified"]
        })

    return images


def get_image_dimensions(s3_client, key):
    """Get image dimensions from S3 object."""
    try:
        from PIL import Image
        from io import BytesIO

        response = s3_client.get_object(Bucket=GARDENCAM_BUCKET, Key=key)
        img = Image.open(BytesIO(response['Body'].read()))
        return img.size  # Returns (width, height)
    except:
        return None


def get_latest_gardencam_images(count=3):
    """Get presigned URLs for the latest N images from S3."""
    if not BOTO3_AVAILABLE:
        return []

    all_images = get_all_gardencam_images()
    images = []

    s3 = boto3.client("s3", region_name=GARDENCAM_REGION)

    for img in all_images[:count]:
        # Check if thumbnail exists, fall back to full-res if not
        thumb_key = f"thumb_{img['key']}"
        try:
            s3.head_object(Bucket=GARDENCAM_BUCKET, Key=thumb_key)
            display_url = get_presigned_url(thumb_key)
        except:
            # Thumbnail doesn't exist, use full-res
            display_url = get_presigned_url(img['key'])

        # Full-res URL for click-through
        full_url = get_presigned_url(img['key'])

        # Get image dimensions
        dimensions = get_image_dimensions(s3, img['key'])
        resolution = f"{dimensions[0]}×{dimensions[1]}" if dimensions else ""

        images.append({
            'url': display_url,
            'full_url': full_url,
            'timestamp': img['timestamp'],
            'key': img['key'],
            'resolution': resolution
        })

    return images


def group_images_by_4hour_periods(images):
    """Group images into 4-hour time periods."""
    from collections import defaultdict

    periods = defaultdict(list)

    for img in images:
        # Parse the timestamp to get the hour
        try:
            ts = img['timestamp']
            # Format: YYYY-MM-DD HH:MM:SS
            date_part = ts.split()[0]
            hour = int(ts.split()[1].split(':')[0])

            # Calculate 4-hour period (0-3, 4-7, 8-11, 12-15, 16-19, 20-23)
            period_start = (hour // 4) * 4
            period_key = f"{date_part} {period_start:02d}:00-{(period_start+3):02d}:59"

            periods[period_key].append(img)
        except:
            periods['Unknown'].append(img)

    # Sort periods in reverse chronological order
    sorted_periods = sorted(periods.items(), reverse=True)
    return sorted_periods


def get_gardencam_stats(limit=500):
    """Get image statistics from DynamoDB."""
    if not BOTO3_AVAILABLE:
        return []

    try:
        dynamodb = boto3.resource('dynamodb', region_name=GARDENCAM_REGION)
        table = dynamodb.Table('gardencam-stats')

        response = table.scan(Limit=limit)
        items = response.get('Items', [])

        # Sort by timestamp
        items.sort(key=lambda x: x.get('timestamp', ''), reverse=True)

        return items
    except Exception as e:
        print(f"Error fetching stats from DynamoDB: {e}")
        return []


def get_lambda_execution_stats(limit=1000):
    """Get Lambda execution statistics from DynamoDB."""
    if not BOTO3_AVAILABLE:
        return []

    try:
        dynamodb = boto3.resource('dynamodb', region_name=GARDENCAM_REGION)
        table = dynamodb.Table('lambda-execution-logs')

        response = table.scan(Limit=limit)
        items = response.get('Items', [])

        # Sort by timestamp
        items.sort(key=lambda x: x.get('timestamp', ''), reverse=True)

        return items
    except Exception as e:
        print(f"Error fetching Lambda execution stats from DynamoDB: {e}")
        return []


# ============================================================================
# t3 - Terse Transport Times (Parklands stop, K2 bus)
# ============================================================================

TFL_API_BASE = "https://api.tfl.gov.uk"
T3_BUSES_PER_DIRECTION = 2

# Stop configurations for K2 bus
T3_STOPS = {
    'parklands': {
        'name': 'Parklands',
        'inbound': '490010781S',   # Parklands southbound (towards Kingston)
        'outbound': '490010781N',  # Parklands northbound (towards Hook)
        'inbound_dest': 'Kingston',
        'outbound_dest': 'Hook'
    },
    'surbiton': {
        'name': 'Surbiton Station',
        'outbound': '490015165B',  # Surbiton Station towards Hook
        'outbound_dest': 'Hook'
        # No inbound - only show outbound at Surbiton
    }
}

# Legacy constants for backwards compatibility
T3_STOP_INBOUND = T3_STOPS['parklands']['inbound']
T3_STOP_OUTBOUND = T3_STOPS['parklands']['outbound']


def t3_fetch_stop(stop_id, api_key=None):
    """Fetch arrivals for a specific stop. Returns seconds."""
    url = f"{TFL_API_BASE}/StopPoint/{stop_id}/Arrivals"
    if api_key:
        url += f"?app_key={api_key}"

    try:
        req = urllib.request.Request(url)
        req.add_header('User-Agent', 't3-terse-transport-times/1.0')
        with urllib.request.urlopen(req, timeout=10) as response:
            data = json.loads(response.read().decode())
        return [a.get('timeToStation', 0) for a in data], None  # Return seconds
    except Exception as e:
        return [], str(e)


def t3_seconds_to_quarter_minutes(seconds):
    """Convert seconds to quarter-minute string (e.g., 5, 5¼, 5½, 5¾)."""
    minutes = seconds // 60
    remainder = seconds % 60
    if remainder < 15:
        return str(minutes)
    elif remainder < 30:
        return f"{minutes}¼"
    elif remainder < 45:
        return f"{minutes}½"
    else:
        return f"{minutes}¾"


def t3_fetch_arrivals(api_key=None, stop='parklands'):
    """Fetch bus arrivals for specified stop from TfL API."""
    stop_config = T3_STOPS.get(stop, T3_STOPS['parklands'])

    result = {}
    errors = []

    # Fetch inbound if available for this stop
    if 'inbound' in stop_config:
        inbound, err = t3_fetch_stop(stop_config['inbound'], api_key)
        if err:
            errors.append(err)
        else:
            inbound.sort()
            result['inbound'] = inbound[:T3_BUSES_PER_DIRECTION]

    # Fetch outbound if available for this stop
    if 'outbound' in stop_config:
        outbound, err = t3_fetch_stop(stop_config['outbound'], api_key)
        if err:
            errors.append(err)
        else:
            outbound.sort()
            result['outbound'] = outbound[:T3_BUSES_PER_DIRECTION]

    if not result and errors:
        return {}, '; '.join(errors)

    return result, None


def t3_format_html(arrivals):
    """Format Parklands arrivals as HTML."""
    inbound = arrivals.get('inbound', [])
    outbound = arrivals.get('outbound', [])

    def format_times(times):
        if not times:
            return '<span class="time-box" style="color:#666">--</span>'
        boxes = []
        for i, secs in enumerate(times):
            cls = 'time-box next' if i == 0 else 'time-box'
            display = t3_seconds_to_quarter_minutes(secs)
            boxes.append(f'<span class="{cls}">{display}</span>')
        return ' '.join(boxes)

    return f"""
<title>K2 Parklands</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body {{ font-family: -apple-system, sans-serif; background: #1a1a1a; color: #fff; padding: 1rem; margin: 0; text-align: center; }}
.nav {{ position: absolute; top: 1rem; left: 1rem; }}
.nav a {{ color: #4a9eff; text-decoration: none; font-size: 0.9rem; }}
h1 {{ font-size: 1.2rem; margin-top: 1rem; margin-bottom: 6rem; }}
.direction {{ margin: 3rem 0; }}
.times {{ font-family: monospace; }}
.time-box {{ display: inline-block; font-size: 6rem; color: #4a9eff; border: 2px solid #4a9eff; border-radius: 12px; padding: 0.3rem 0.8rem; margin: 0 0.5rem; }}
.time-box.next {{ color: #fff; font-weight: bold; border-color: #fff; }}
.dest {{ font-size: 0.75rem; color: #666; margin-top: 1rem; }}
.refresh {{ font-size: 0.8rem; color: #444; margin-top: 3rem; }}
</style>
<div class="nav"><a href="contents">Home</a></div>
<h1>K2 @ Parklands</h1>
<div class="direction">
  <div class="times">{format_times(inbound)}</div>
  <div class="dest">towards Kingston</div>
</div>
<div class="direction">
  <div class="times">{format_times(outbound)}</div>
  <div class="dest">towards Hook</div>
</div>
<div class="refresh">refresh in <span id="countdown">60</span>s</div>
<script>
let t = 60;
const el = document.getElementById('countdown');
setInterval(() => {{
  t--;
  if (t <= 0) location.reload();
  el.textContent = t;
}}, 1000);
</script>
"""


def t3_format_json(arrivals, stop='parklands'):
    """Format arrivals as JSON for API consumers."""
    stop_config = T3_STOPS.get(stop, T3_STOPS['parklands'])

    result = {
        "stop": stop_config['name'],
        "route": "K2",
        "timestamp": datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
    }

    # Add inbound if available for this stop
    if 'inbound' in stop_config:
        result["inbound"] = {
            "destination": stop_config['inbound_dest'],
            "seconds": arrivals.get('inbound', [])
        }

    # Add outbound if available for this stop
    if 'outbound' in stop_config:
        result["outbound"] = {
            "destination": stop_config['outbound_dest'],
            "seconds": arrivals.get('outbound', [])
        }

    return json.dumps(result)


# ============================================================================
# Memspeed - Memory bandwidth benchmark visualization
# ============================================================================

def get_memspeed_results():
    """Get all memspeed benchmark results from S3."""
    if not BOTO3_AVAILABLE:
        return []

    s3 = boto3.client("s3", region_name=GARDENCAM_REGION)
    results = []

    try:
        paginator = s3.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=GARDENCAM_BUCKET, Prefix=MEMSPEED_RESULTS_PREFIX):
            if "Contents" not in page:
                continue
            for obj in page["Contents"]:
                key = obj["Key"]
                if not key.endswith('.json'):
                    continue
                try:
                    response = s3.get_object(Bucket=GARDENCAM_BUCKET, Key=key)
                    data = json.loads(response['Body'].read().decode('utf-8'))
                    data['_key'] = key
                    results.append(data)
                except Exception as e:
                    print(f"Error reading {key}: {e}")
    except Exception as e:
        print(f"Error listing memspeed results: {e}")

    return results


def get_memspeed_downloads():
    """Get list of available memspeed downloads from S3."""
    if not BOTO3_AVAILABLE:
        return []

    s3 = boto3.client("s3", region_name=GARDENCAM_REGION)
    downloads = []

    try:
        paginator = s3.get_paginator('list_objects_v2')
        for page in paginator.paginate(Bucket=GARDENCAM_BUCKET, Prefix=MEMSPEED_DOWNLOADS_PREFIX):
            if "Contents" not in page:
                continue
            for obj in page["Contents"]:
                key = obj["Key"]
                filename = key.replace(MEMSPEED_DOWNLOADS_PREFIX, '')
                if not filename:
                    continue
                downloads.append({
                    'key': key,
                    'filename': filename,
                    'size': obj['Size'],
                    'last_modified': obj['LastModified'].isoformat()
                })
    except Exception as e:
        print(f"Error listing memspeed downloads: {e}")

    return downloads


def get_memspeed_download_url(key, expires_in=3600):
    """Generate presigned URL for a memspeed download."""
    if not BOTO3_AVAILABLE:
        return None
    s3 = boto3.client("s3", region_name=GARDENCAM_REGION)
    return s3.generate_presigned_url(
        'get_object',
        Params={'Bucket': GARDENCAM_BUCKET, 'Key': key},
        ExpiresIn=expires_in
    )


def save_memspeed_result(data):
    """Save a memspeed benchmark result to S3."""
    if not BOTO3_AVAILABLE:
        return False, "S3 not available"

    # Validate required fields
    required = ['machine', 'data']
    for field in required:
        if field not in data:
            return False, f"Missing required field: {field}"

    # Add timestamp if not present
    if 'timestamp' not in data:
        data['timestamp'] = datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')

    # Generate key from machine name and timestamp
    machine = data['machine'].replace(' ', '_').replace('/', '_')
    ts = data['timestamp'].replace(':', '-').replace('T', '_').split('.')[0]
    key = f"{MEMSPEED_RESULTS_PREFIX}{machine}_{ts}.json"

    try:
        s3 = boto3.client("s3", region_name=GARDENCAM_REGION)
        s3.put_object(
            Bucket=GARDENCAM_BUCKET,
            Key=key,
            Body=json.dumps(data, indent=2),
            ContentType='application/json'
        )
        return True, key
    except Exception as e:
        return False, str(e)


def render_memspeed_page(results, downloads):
    """Render the memspeed visualization page."""
    # Assign colors to machines
    colors = [
        '#4a9eff', '#ef4444', '#10b981', '#f59e0b', '#8b5cf6',
        '#ec4899', '#06b6d4', '#84cc16', '#f97316', '#6366f1'
    ]

    # Prepare datasets for Chart.js
    datasets_js = []
    for i, result in enumerate(results):
        machine = result.get('machine', 'Unknown')
        color = colors[i % len(colors)]
        data_points = result.get('data', [])

        # Format data for scatter plot
        points = [{'x': p['size'], 'y': p['speed']} for p in data_points]

        cpu = result.get('cpu', '')
        ram = result.get('ram', '')
        label = machine
        if cpu:
            label += f" ({cpu})"

        datasets_js.append({
            'label': label,
            'data': points,
            'borderColor': color,
            'backgroundColor': color,
            'showLine': True,
            'tension': 0.1,
            'pointRadius': 2,
            'borderWidth': 2
        })

    # Build downloads HTML
    downloads_html = ''
    if downloads:
        downloads_html = '<h2>Downloads</h2><div class="downloads-grid">'
        for dl in downloads:
            size_kb = dl['size'] / 1024
            if size_kb > 1024:
                size_str = f"{size_kb/1024:.1f} MB"
            else:
                size_str = f"{size_kb:.1f} KB"
            downloads_html += f'''
            <a href="memspeed/download?file={dl['filename']}" class="download-item">
                <span class="filename">{dl['filename']}</span>
                <span class="filesize">{size_str}</span>
            </a>
            '''
        downloads_html += '</div>'

    # Build results table
    results_table = ''
    if results:
        results_table = '''
        <h2>Benchmark Results</h2>
        <table class="results-table">
            <thead>
                <tr>
                    <th>Machine</th>
                    <th>CPU</th>
                    <th>Cache (L1 / L2 / L3)</th>
                    <th>RAM</th>
                    <th>OS</th>
                    <th>Timestamp</th>
                </tr>
            </thead>
            <tbody>
        '''
        for result in results:
            cache = result.get('cache', {})
            if cache:
                cache_str = f"{cache.get('L1', '-')} / {cache.get('L2', '-')} / {cache.get('L3', '-')}"
            else:
                cache_str = '-'
            results_table += f'''
                <tr>
                    <td>{result.get('machine', 'Unknown')}</td>
                    <td>{result.get('cpu', '-')}</td>
                    <td>{cache_str}</td>
                    <td>{result.get('ram', '-')}</td>
                    <td>{result.get('os', '-')}</td>
                    <td>{result.get('timestamp', '-')}</td>
                </tr>
            '''
        results_table += '</tbody></table>'

    return f'''
    <title>Memory Bandwidth Benchmark</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
    <style>
        body {{ font-family: Arial, sans-serif; margin: 0; padding: 1rem; background: #1a1a1a; color: #fff; }}
        .nav {{ text-align: center; margin-bottom: 1.5rem; }}
        .nav a {{ color: #4a9eff; text-decoration: none; margin: 0 1rem; padding: 0.5rem 1rem; background: #2a2a2a; border-radius: 6px; display: inline-block; }}
        .nav a:hover {{ background: #3a3a3a; }}
        h1 {{ text-align: center; margin-bottom: 2rem; }}
        h2 {{ color: #aaa; margin-top: 2rem; }}
        .chart-container {{ max-width: 1400px; margin: 0 auto 2rem auto; background: #2a2a2a; padding: 1.5rem; border-radius: 8px; }}
        .chart-title {{ font-size: 1.2rem; margin-bottom: 1rem; color: #aaa; text-align: center; }}
        canvas {{ max-height: 500px; }}
        .upload-section {{ max-width: 600px; margin: 2rem auto; padding: 1.5rem; background: #2a2a2a; border-radius: 8px; }}
        .upload-section h2 {{ margin-top: 0; }}
        .upload-form {{ display: flex; flex-direction: column; gap: 1rem; }}
        .upload-form input[type="file"] {{ padding: 0.5rem; background: #1a1a1a; border: 1px solid #444; border-radius: 4px; color: #fff; }}
        .upload-form button {{ padding: 0.75rem 1.5rem; background: #4a9eff; color: #fff; border: none; border-radius: 6px; cursor: pointer; font-size: 1rem; }}
        .upload-form button:hover {{ background: #3a8eef; }}
        .upload-form button:disabled {{ background: #666; cursor: not-allowed; }}
        #uploadStatus {{ margin-top: 0.5rem; font-size: 0.9rem; }}
        .downloads-grid {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(250px, 1fr)); gap: 1rem; margin-top: 1rem; }}
        .download-item {{ display: flex; justify-content: space-between; align-items: center; padding: 1rem; background: #1a1a1a; border-radius: 6px; text-decoration: none; color: #4a9eff; transition: background 0.3s; }}
        .download-item:hover {{ background: #333; }}
        .filename {{ font-family: monospace; }}
        .filesize {{ color: #888; font-size: 0.9rem; }}
        .results-table {{ width: 100%; border-collapse: collapse; margin-top: 1rem; }}
        .results-table th, .results-table td {{ padding: 0.75rem; text-align: left; border-bottom: 1px solid #3a3a3a; }}
        .results-table th {{ color: #aaa; background: #1a1a1a; }}
        .results-table td {{ font-family: monospace; font-size: 0.9rem; }}
        .no-data {{ text-align: center; color: #888; padding: 2rem; }}
        .about-section {{ max-width: 900px; margin: 0 auto 2rem auto; padding: 1.5rem; background: #2a2a2a; border-radius: 8px; line-height: 1.6; }}
        .about-section h2 {{ margin-top: 0; color: #aaa; }}
        .about-section p {{ color: #ccc; margin: 1rem 0; }}
        .about-section code {{ background: #1a1a1a; padding: 0.2rem 0.5rem; border-radius: 4px; font-family: monospace; }}
        .about-section pre {{ background: #1a1a1a; padding: 1rem; border-radius: 6px; overflow-x: auto; font-size: 0.9rem; }}
        .about-section ul {{ color: #ccc; margin: 1rem 0; padding-left: 1.5rem; }}
        .about-section li {{ margin: 0.5rem 0; }}
    </style>
    <div class="nav">
        <a href="contents">Home</a>
        <a href="memspeed/data">JSON API</a>
    </div>
    <h1>Memory Bandwidth Benchmark</h1>

    <div class="about-section">
        <h2>About</h2>
        <p>
            This tool measures memory bandwidth by writing to buffers of increasing size.
            The resulting curve reveals CPU cache hierarchy: L1 cache (fastest), L2, L3, and main RAM (slowest).
            Sharp drops in speed indicate transitions between cache levels.
        </p>
        <p>
            The chart uses a log-log scale to clearly show performance across buffer sizes from 1KB to 1GB.
            Compare results across different machines to see how CPU architecture and RAM speed affect performance.
        </p>

        <h2>How to Run</h2>
        <p>Download the source or pre-built binary, run the benchmark, and upload your results:</p>
        <pre># Option 1: Download and compile from source
tar -xzf memspeed-src.tar.gz
gcc ms.c -o ms

# Option 2: Use pre-built binary (Linux x86_64)
chmod +x ms-linux-x86_64
./ms-linux-x86_64

# Run benchmark and generate results
./ms > all.out
grep -v Reps all.out > data.csv
./export_json.py > result.json</pre>
        <p>Then upload <code>result.json</code> using the form below, or via curl:</p>
        <pre>curl -u ":PASSWORD" -X POST -H "Content-Type: application/json" \\
  -d @result.json https://cv.petergrecian.co.uk/memspeed/upload</pre>
    </div>

    <div class="chart-container">
        <div class="chart-title">Memory Read Speed vs Buffer Size (Log-Log Scale)</div>
        <canvas id="memspeedChart"></canvas>
    </div>

    {downloads_html}

    {results_table if results else '<p class="no-data">No benchmark results yet. Upload your results below.</p>'}

    <div class="upload-section">
        <h2>Upload Results</h2>
        <form class="upload-form" id="uploadForm">
            <input type="file" id="jsonFile" accept=".json" required>
            <button type="submit" id="uploadBtn">Upload Benchmark</button>
            <div id="uploadStatus"></div>
        </form>
        <p style="color: #888; font-size: 0.85rem; margin-top: 1rem;">
            Generate JSON with: <code style="background: #1a1a1a; padding: 0.2rem 0.4rem; border-radius: 3px;">./export_json.py &gt; result.json</code>
        </p>
    </div>

    <script>
    const datasets = {json.dumps(datasets_js)};

    if (datasets.length > 0) {{
        new Chart(document.getElementById('memspeedChart'), {{
            type: 'scatter',
            data: {{ datasets: datasets }},
            options: {{
                responsive: true,
                maintainAspectRatio: true,
                scales: {{
                    x: {{
                        type: 'logarithmic',
                        title: {{ display: true, text: 'Buffer Size (bytes)', color: '#aaa' }},
                        ticks: {{
                            color: '#888',
                            callback: function(value) {{
                                if (value >= 1e9) return (value/1e9) + ' GB';
                                if (value >= 1e6) return (value/1e6) + ' MB';
                                if (value >= 1e3) return (value/1e3) + ' KB';
                                return value + ' B';
                            }}
                        }},
                        grid: {{ color: '#333' }}
                    }},
                    y: {{
                        type: 'logarithmic',
                        title: {{ display: true, text: 'Speed (bytes/sec)', color: '#aaa' }},
                        ticks: {{
                            color: '#888',
                            callback: function(value) {{
                                if (value >= 1e9) return (value/1e9) + ' GB/s';
                                if (value >= 1e6) return (value/1e6) + ' MB/s';
                                if (value >= 1e3) return (value/1e3) + ' KB/s';
                                return value + ' B/s';
                            }}
                        }},
                        grid: {{ color: '#333' }}
                    }}
                }},
                plugins: {{
                    legend: {{
                        labels: {{ color: '#aaa' }},
                        position: 'top'
                    }},
                    tooltip: {{
                        callbacks: {{
                            label: function(context) {{
                                const size = context.parsed.x;
                                const speed = context.parsed.y;
                                let sizeStr = size >= 1e6 ? (size/1e6).toFixed(1) + ' MB' : (size/1e3).toFixed(1) + ' KB';
                                let speedStr = (speed/1e9).toFixed(2) + ' GB/s';
                                return context.dataset.label + ': ' + sizeStr + ' @ ' + speedStr;
                            }}
                        }}
                    }}
                }}
            }}
        }});
    }}

    document.getElementById('uploadForm').addEventListener('submit', async function(e) {{
        e.preventDefault();
        const fileInput = document.getElementById('jsonFile');
        const status = document.getElementById('uploadStatus');
        const btn = document.getElementById('uploadBtn');

        if (!fileInput.files[0]) {{
            status.textContent = 'Please select a file';
            status.style.color = '#ef4444';
            return;
        }}

        btn.disabled = true;
        btn.textContent = 'Uploading...';
        status.textContent = '';

        try {{
            const text = await fileInput.files[0].text();
            const data = JSON.parse(text);

            const response = await fetch('memspeed/upload', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify(data)
            }});

            const result = await response.json();

            if (response.ok) {{
                status.textContent = result.message || 'Upload successful!';
                status.style.color = '#10b981';
                setTimeout(() => location.reload(), 1500);
            }} else {{
                status.textContent = result.error || 'Upload failed';
                status.style.color = '#ef4444';
            }}
        }} catch (err) {{
            status.textContent = 'Error: ' + err.message;
            status.style.color = '#ef4444';
        }}

        btn.disabled = false;
        btn.textContent = 'Upload Benchmark';
    }});
    </script>
    '''


def lambda_handler(event, context):
    import time
    start_time = time.time()

    # Log connection details to DynamoDB
    log_connection(event, context)

    html = ""
    try:
        favicon = open("favicon.png64", "r").read()
        fav = f'<link rel="icon" type="image/png" href="data:image/png;base64,{favicon}">'
    except FileNotFoundError:
        fav = ""
    #fav += '\n<head><link rel="stylesheet" href="styles.css"></head>'
    path = event['path']
    stage = event['requestContext']['stage']
    host = event['headers']['Host']
    root=f'https://{host}/{stage}'
    print(f'path = {path}, stage = {stage}, root = {root}')
    try:
        ref = event['headers']['referer']
        print(f'referer = {ref}')
    except KeyError:
        pass
    ip = event['headers']['X-Forwarded-For']
    print(f'X-Forwarded-For = {ip}')

    if path == f'/{stage}/event' or path == '/event':   # debugging info
        html += '<div style="text-align: center; margin: 1rem;"><a href="contents" style="color: #4a9eff; text-decoration: none;">Home</a></div>'
        html += 'log_group = ' + context.log_group_name + '<br>'
        html += 'log_stream = ' + context.log_stream_name + '<br>' 
        html += 'path = ' + path + '<br>'
        html += 'stage = ' + stage + '<br>'
        html += 'root = ' + root + '<br>'
        html += 'pwd = ' + os.getcwd() + '<br>'
        for ff in os.listdir(os.getcwd()):
            html += ff + ', '
        html += '<br>'
        for key in event.keys():
            html += "_______________________" + key + "_________________________<br>"
            html += pformat(event[key]).replace(',', ',<br>') + "<br><br>"
    elif path == f'/{stage}/gitinfo' or path == '/gitinfo':
        html = open("gitinfo.html", "r").read()
    elif path == f'/{stage}/contents' or path == '/contents':
        html += open('contents.html', 'r').read()
    elif path.startswith(f'/{stage}/gardencam/capture') or path.startswith('/gardencam/capture'):
        # Capture command endpoint
        if not check_basic_auth(event, GARDENCAM_PASSWORD):
            return {
                'statusCode': 401,
                'body': json.dumps({'error': 'Unauthorized'}),
                'headers': {
                    'Content-Type': 'application/json',
                    'WWW-Authenticate': 'Basic realm="Garden Camera"'
                }
            }

        # Write command to DynamoDB
        try:
            dynamodb = boto3.resource('dynamodb', region_name=GARDENCAM_REGION)
            table = dynamodb.Table('gardencam-commands')

            command_id = f"capture_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
            item = {
                'command_id': command_id,
                'command': 'take_picture',
                'status': 'pending',
                'created_at': datetime.utcnow().isoformat(),
                'requested_by': event['headers'].get('X-Forwarded-For', 'unknown')
            }

            table.put_item(Item=item)

            return {
                'statusCode': 200,
                'body': json.dumps({'message': 'Capture command sent! Image will appear shortly.', 'command_id': command_id}),
                'headers': {'Content-Type': 'application/json'}
            }
        except Exception as e:
            print(f"Error writing capture command: {e}")
            return {
                'statusCode': 500,
                'body': json.dumps({'error': 'Failed to send capture command'}),
                'headers': {'Content-Type': 'application/json'}
            }

    elif path.startswith(f'/{stage}/gardencam/stats') or path.startswith('/gardencam/stats'):
        # Stats visualization page
        if not check_basic_auth(event, GARDENCAM_PASSWORD):
            return {
                'statusCode': 401,
                'body': '<html><body><h1>401 Unauthorized</h1><p>Access denied.</p></body></html>',
                'headers': {
                    'Content-Type': 'text/html',
                    'WWW-Authenticate': 'Basic realm="Garden Camera"'
                }
            }

        stats = get_gardencam_stats(limit=500)

        # Prepare data for Chart.js
        # Note on noise floor and autocontrast:
        # - SD (noise floor) is NOT affected by shifts but IS affected by scaling
        # - Autocontrast scales by 255/(max-min), so post_ac_sd = pre_ac_sd * 255/dynamic_range
        # - We use noise_floor_post_ac for the chart so all values are comparable
        #   (including manually measured averaged images which were measured after autocontrast)
        timestamps = []
        avg_brightness_data = []
        peak_brightness_data = []
        noise_floor_data = []  # Post-autocontrast SD for comparability
        noise_floor_pre_ac = []  # Pre-autocontrast SD (raw)
        modes = []

        for item in reversed(stats):  # Reverse to show oldest first in chart
            ts = item.get('timestamp', '')
            if ts:
                # Format timestamp for display (just date and time, no milliseconds)
                try:
                    dt_str = ts.split('.')[0].replace('T', ' ')
                    timestamps.append(dt_str)
                except:
                    timestamps.append(ts)

                avg_brightness_data.append(float(item.get('avg_brightness', 0)))
                peak_brightness_data.append(float(item.get('peak_brightness', 0)))
                # Use post-autocontrast noise floor if available, fall back to pre-AC
                post_ac = item.get('noise_floor_post_ac')
                pre_ac = float(item.get('noise_floor', 0))
                if post_ac is not None:
                    noise_floor_data.append(float(post_ac))
                else:
                    # Old data without post_ac: estimate it from pre_ac and dynamic range
                    min_b = float(item.get('min_brightness', 0))
                    max_b = float(item.get('peak_brightness', 255))
                    dynamic_range = max_b - min_b
                    if dynamic_range > 0:
                        noise_floor_data.append(pre_ac * 255.0 / dynamic_range)
                    else:
                        noise_floor_data.append(pre_ac)
                noise_floor_pre_ac.append(pre_ac)
                modes.append(item.get('mode', 'unknown'))

        # Get averaged images data for noise floor comparison
        averaged_timestamps = []
        averaged_noise_floor = []

        # Known averaged images (measured after autocontrast, comparable to noise_floor_post_ac)
        known_averaged = [
            ('2026-01-20 06:00:00', 41.5),  # averaged_20260120_04-07.jpg (24 images)
        ]

        for ts, noise in known_averaged:
            averaged_timestamps.append(ts)
            averaged_noise_floor.append(noise)

        print(f"Showing {len(averaged_timestamps)} averaged images on noise floor chart")

        html += f'''
        <title>Garden Camera Statistics</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 0; padding: 1rem; background: #1a1a1a; color: #fff; }}
            .nav {{ text-align: center; margin-bottom: 1.5rem; }}
            .nav a {{ color: #4a9eff; text-decoration: none; margin: 0 1rem; padding: 0.5rem 1rem; background: #2a2a2a; border-radius: 6px; display: inline-block; }}
            .nav a:hover {{ background: #3a3a3a; }}
            h1 {{ text-align: center; margin-bottom: 2rem; }}
            .chart-container {{ max-width: 1400px; margin: 0 auto 3rem auto; background: #2a2a2a; padding: 1.5rem; border-radius: 8px; }}
            .chart-title {{ font-size: 1.2rem; margin-bottom: 1rem; color: #aaa; text-align: center; }}
            canvas {{ max-height: 400px; }}
            .stats-summary {{ max-width: 1400px; margin: 0 auto 2rem auto; padding: 1rem; background: #2a2a2a; border-radius: 8px; }}
            .stats-summary h2 {{ margin-top: 0; color: #aaa; }}
            .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; }}
            .stat-box {{ background: #1a1a1a; padding: 1rem; border-radius: 6px; text-align: center; }}
            .stat-value {{ font-size: 2rem; font-weight: bold; color: #4a9eff; }}
            .stat-label {{ color: #888; margin-top: 0.5rem; }}
        </style>
        <div class="nav">
            <a href="../../contents">Home</a>
            <a href="../gardencam">Latest</a>
            <a href="gallery">Gallery</a>
            <a href="videos">Videos</a>
            <a href="s3-stats">Storage</a>
        </div>
        <h1>Garden Camera Statistics</h1>

        <div class="stats-summary">
            <h2>Summary (Last {len(stats)} images)</h2>
            <div class="stats-grid">
                <div class="stat-box">
                    <div class="stat-value">{len(stats)}</div>
                    <div class="stat-label">Total Images</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">{sum(1 for m in modes if m == 'day')}</div>
                    <div class="stat-label">Day Mode</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">{sum(1 for m in modes if m == 'night')}</div>
                    <div class="stat-label">Night Mode</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">{sum(avg_brightness_data)/len(avg_brightness_data):.1f}</div>
                    <div class="stat-label">Avg Brightness</div>
                </div>
            </div>
        </div>

        <div class="chart-container">
            <div class="chart-title">Average Brightness Over Time</div>
            <canvas id="brightnessChart"></canvas>
        </div>

        <div class="chart-container">
            <div class="chart-title">Peak Brightness Over Time</div>
            <canvas id="peakChart"></canvas>
        </div>

        <div class="chart-container">
            <div class="chart-title">Noise Floor Over Time (Post-Autocontrast SD)</div>
            <canvas id="noiseChart"></canvas>
        </div>

        <script>
        const timestamps = {json.dumps(timestamps)};
        const avgBrightness = {json.dumps(avg_brightness_data)};
        const peakBrightness = {json.dumps(peak_brightness_data)};
        const noiseFloor = {json.dumps(noise_floor_data)};
        const averagedTimestamps = {json.dumps(averaged_timestamps)};
        const averagedNoiseFloor = {json.dumps(averaged_noise_floor)};

        const chartOptions = {{
            responsive: true,
            maintainAspectRatio: true,
            scales: {{
                x: {{
                    ticks: {{ color: '#888', maxTicksLimit: 10 }},
                    grid: {{ color: '#333' }}
                }},
                y: {{
                    ticks: {{ color: '#888' }},
                    grid: {{ color: '#333' }}
                }}
            }},
            plugins: {{
                legend: {{ labels: {{ color: '#aaa' }} }}
            }}
        }};

        new Chart(document.getElementById('brightnessChart'), {{
            type: 'line',
            data: {{
                labels: timestamps,
                datasets: [{{
                    label: 'Average Brightness',
                    data: avgBrightness,
                    borderColor: '#4a9eff',
                    backgroundColor: 'rgba(74, 158, 255, 0.1)',
                    tension: 0.3
                }}]
            }},
            options: chartOptions
        }});

        new Chart(document.getElementById('peakChart'), {{
            type: 'line',
            data: {{
                labels: timestamps,
                datasets: [{{
                    label: 'Peak Brightness',
                    data: peakBrightness,
                    borderColor: '#f59e0b',
                    backgroundColor: 'rgba(245, 158, 11, 0.1)',
                    tension: 0.3
                }}]
            }},
            options: chartOptions
        }});

        // Prepare averaged image data points for scatter plot
        const averagedDataPoints = averagedTimestamps.map((ts, i) => ({{
            x: ts,
            y: averagedNoiseFloor[i]
        }}));

        new Chart(document.getElementById('noiseChart'), {{
            type: 'line',
            data: {{
                labels: timestamps,
                datasets: [
                    {{
                        label: 'Noise Floor (Post-AC SD)',
                        data: noiseFloor,
                        borderColor: '#10b981',
                        backgroundColor: 'rgba(16, 185, 129, 0.1)',
                        tension: 0.3,
                        type: 'line',
                        order: 2
                    }},
                    {{
                        label: 'Averaged Images (Expected)',
                        data: averagedDataPoints,
                        borderColor: '#ef4444',
                        backgroundColor: '#ef4444',
                        pointRadius: 12,
                        pointHoverRadius: 15,
                        pointStyle: 'circle',
                        showLine: false,
                        type: 'scatter',
                        order: 1
                    }}
                ]
            }},
            options: chartOptions
        }});
        </script>
        '''
    elif path.startswith(f'/{stage}/gardencam/fullres') or path.startswith('/gardencam/fullres'):
        # Full resolution image view
        if not check_basic_auth(event, GARDENCAM_PASSWORD):
            return {
                'statusCode': 401,
                'body': '<html><body><h1>401 Unauthorized</h1><p>Access denied.</p></body></html>',
                'headers': {
                    'Content-Type': 'text/html',
                    'WWW-Authenticate': 'Basic realm="Garden Camera"'
                }
            }

        # Get image key from query string
        query_params = event.get('queryStringParameters', {}) or {}
        image_key = query_params.get('key', '')

        if image_key:
            timestamp = parse_timestamp_from_key(image_key) or 'Unknown'
            image_url = get_presigned_url(image_key)

            html += f'''
            <title>Full Resolution - {timestamp}</title>
            <style>
                body {{ font-family: Arial, sans-serif; text-align: center; margin: 0; padding: 1rem; background: #1a1a1a; color: #fff; }}
                .nav {{ margin-bottom: 1rem; }}
                .nav a {{ color: #4a9eff; text-decoration: none; margin: 0 1rem; }}
                .nav a:hover {{ text-decoration: underline; }}
                h2 {{ margin-bottom: 1rem; color: #aaa; }}
                img {{ max-width: 100%; height: auto; border-radius: 8px; box-shadow: 0 4px 8px rgba(0,0,0,0.5); }}
            </style>
            <div class="nav">
                <a href="../../contents">Home</a> | <a href="../gardencam">Latest</a> | <a href="gallery">Gallery</a>
            </div>
            <h2>{timestamp} UTC</h2>
            <img src="{image_url}" alt="Full resolution image">
            '''
        else:
            html += '<h1>Error: No image specified</h1>'
    elif path.startswith(f'/{stage}/gardencam/display') or path.startswith('/gardencam/display'):
        # Display-width image view
        if not check_basic_auth(event, GARDENCAM_PASSWORD):
            return {
                'statusCode': 401,
                'body': '<html><body><h1>401 Unauthorized</h1><p>Access denied.</p></body></html>',
                'headers': {
                    'Content-Type': 'text/html',
                    'WWW-Authenticate': 'Basic realm="Garden Camera"'
                }
            }

        # Get image key from query string
        query_params = event.get('queryStringParameters', {}) or {}
        image_key = query_params.get('key', '')

        if image_key:
            timestamp = parse_timestamp_from_key(image_key) or 'Unknown'
            image_url = get_presigned_url(image_key)

            html += f'''
            <title>Display Width - {timestamp}</title>
            <style>
                body {{ font-family: Arial, sans-serif; text-align: center; margin: 0; padding: 1rem; background: #1a1a1a; color: #fff; }}
                .nav {{ margin-bottom: 1rem; }}
                .nav a {{ color: #4a9eff; text-decoration: none; margin: 0 1rem; }}
                .nav a:hover {{ text-decoration: underline; }}
                h2 {{ margin-bottom: 1rem; color: #aaa; }}
                .image-container {{ max-width: 1920px; margin: 0 auto; }}
                img {{ width: 100%; height: auto; border-radius: 8px; box-shadow: 0 4px 8px rgba(0,0,0,0.5); }}
            </style>
            <div class="nav">
                <a href="../../contents">Home</a> | <a href="../gardencam">Latest</a> | <a href="gallery">Gallery</a> | <a href="fullres?key={image_key}">Full Res</a>
            </div>
            <h2>{timestamp} UTC</h2>
            <div class="image-container">
                <a href="fullres?key={image_key}">
                    <img src="{image_url}" alt="Display width image">
                </a>
            </div>
            '''
        else:
            html += '<h1>Error: No image specified</h1>'
    elif path.startswith(f'/{stage}/gardencam/gallery') or path.startswith('/gardencam/gallery'):
        # Gallery page with thumbnails organized by 4-hour periods
        if not check_basic_auth(event, GARDENCAM_PASSWORD):
            return {
                'statusCode': 401,
                'body': '<html><body><h1>401 Unauthorized</h1><p>Access denied.</p></body></html>',
                'headers': {
                    'Content-Type': 'text/html',
                    'WWW-Authenticate': 'Basic realm="Garden Camera"'
                }
            }

        all_images = get_all_gardencam_images()
        periods = group_images_by_4hour_periods(all_images)

        # Get query parameters
        query_params = event.get('queryStringParameters', {}) or {}
        period_param = query_params.get('period', '')

        # If no period specified, show index of all periods
        if not period_param:
            html += '''
            <title>Garden Camera Gallery Index</title>
            <style>
                body { font-family: Arial, sans-serif; margin: 0; padding: 1rem; background: #1a1a1a; color: #fff; }
                .nav { text-align: center; margin-bottom: 2rem; }
                .nav a { color: #4a9eff; text-decoration: none; margin: 0 1rem; }
                .nav a:hover { text-decoration: underline; }
                h1 { text-align: center; margin-bottom: 2rem; }
                .period-list { max-width: 800px; margin: 0 auto; }
                .period-link { display: block; padding: 1rem 1.5rem; margin-bottom: 0.75rem; background: #2a2a2a; border-radius: 8px; text-decoration: none; color: #4a9eff; font-size: 1.1rem; transition: background 0.3s; }
                .period-link:hover { background: #3a3a3a; }
                .period-count { float: right; color: #888; font-size: 0.9rem; }
            </style>
            <div class="nav">
                <a href="../../contents">Home</a>
                <a href="../gardencam">Latest</a>
                <a href="videos">Videos</a>
                <a href="stats">Statistics</a>
            </div>
            <h1>Garden Camera Gallery Index</h1>
            <div class="period-list">
            '''

            for period_name, period_images in periods:
                image_count = len(period_images)
                html += f'''
                <a href="gallery?period={period_name}" class="period-link">
                    {period_name} UTC
                    <span class="period-count">{image_count} image{"s" if image_count != 1 else ""}</span>
                </a>
                '''

            html += '</div>'

        else:
            # Show specific period
            # Find the requested period
            period_index = None
            current_period_images = []
            for idx, (period_name, period_images) in enumerate(periods):
                if period_name == period_param:
                    period_index = idx
                    current_period_images = period_images
                    break

            if period_index is None:
                html += '<h1>Period not found</h1><p><a href="gallery">Back to Gallery Index</a></p>'
            else:
                # Build navigation links
                prev_link = ''
                next_link = ''
                if period_index > 0:
                    prev_period = periods[period_index - 1][0]
                    prev_link = f'<a href="gallery?period={prev_period}">← Previous</a>'
                if period_index < len(periods) - 1:
                    next_period = periods[period_index + 1][0]
                    next_link = f'<a href="gallery?period={next_period}">Next →</a>'

                html += f'''
                <title>{period_param} - Gallery</title>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 0; padding: 1rem; background: #1a1a1a; color: #fff; }}
                    .nav {{ text-align: center; margin-bottom: 1.5rem; }}
                    .nav a {{ color: #4a9eff; text-decoration: none; margin: 0 1rem; padding: 0.5rem 1rem; background: #2a2a2a; border-radius: 6px; display: inline-block; }}
                    .nav a:hover {{ background: #3a3a3a; }}
                    h1 {{ text-align: center; margin-bottom: 2rem; }}
                    .thumbnails {{ display: grid; grid-template-columns: repeat(auto-fill, minmax(200px, 1fr)); gap: 1rem; max-width: 1400px; margin: 0 auto; }}
                    .thumb-container {{ position: relative; }}
                    .thumb-container a {{ display: block; }}
                    .thumb-container img {{ width: 100%; height: 150px; object-fit: cover; border-radius: 6px; transition: transform 0.3s; box-shadow: 0 2px 4px rgba(0,0,0,0.5); }}
                    .thumb-container img:hover {{ transform: scale(1.05); }}
                    .thumb-time {{ text-align: center; font-size: 0.85rem; color: #888; margin-top: 0.3rem; }}

                    @media (max-width: 768px) {{
                        .thumbnails {{ grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 0.75rem; }}
                        .thumb-container img {{ height: 120px; }}
                    }}
                </style>
                <div class="nav">
                    <a href="../../contents">Home</a>
                    {prev_link}
                    <a href="gallery">Index</a>
                    <a href="../gardencam">Latest</a>
                    {next_link}
                </div>
                <h1>{period_param} UTC</h1>
                '''

                # Check for averaged image for this period
                try:
                    date_part = period_param.split()[0].replace('-', '')
                    time_range = period_param.split()[1]
                    start_hour = int(time_range.split(':')[0])
                    # Period is "04:00-07:59", so end hour should be 7 (last hour in range)
                    end_hour = int(time_range.split('-')[1].split(':')[0])

                    # Check if averaged image exists
                    averaged_key = f"averaged_medium_{date_part}_{start_hour:02d}-{end_hour:02d}.jpg"
                    averaged_full_key = f"averaged_{date_part}_{start_hour:02d}-{end_hour:02d}.jpg"

                    s3 = boto3.client("s3", region_name=GARDENCAM_REGION)
                    try:
                        s3.head_object(Bucket=GARDENCAM_BUCKET, Key=averaged_key)
                        has_averaged = True
                    except:
                        has_averaged = False
                except:
                    has_averaged = False

                if has_averaged:
                    # Show averaged image at the top
                    averaged_url = get_presigned_url(averaged_key)
                    averaged_full_url = get_presigned_url(averaged_full_key)
                    html += f'''
                    <div style="text-align: center; margin-bottom: 2rem; padding: 1rem; background: #2a2a2a; border-radius: 8px;">
                        <div style="color: #4a9eff; margin-bottom: 0.5rem; font-size: 1.1rem;">⭐ Averaged from {len(current_period_images)} images</div>
                        <a href="display?key={averaged_full_key}" style="display: inline-block; max-width: 800px;">
                            <img src="{averaged_url}" alt="Averaged {period_param}" style="width: 100%; border-radius: 8px; box-shadow: 0 4px 8px rgba(0,0,0,0.5);">
                        </a>
                    </div>
                    '''

                html += '<div class="thumbnails">'

                for img in current_period_images:
                    thumb_url = get_presigned_url(img['key'])
                    time_only = img['timestamp'].split()[1] if ' ' in img['timestamp'] else img['timestamp']

                    html += f'''
                    <div class="thumb-container">
                        <a href="display?key={img['key']}">
                            <img src="{thumb_url}" alt="{img['timestamp']}">
                        </a>
                        <div class="thumb-time">{time_only}</div>
                    </div>
                    '''

                html += '</div>'

    elif path.startswith(f'/{stage}/gardencam/s3-stats') or path.startswith('/gardencam/s3-stats'):
        # S3 storage statistics page - reads from cached JSON
        if not check_basic_auth(event, GARDENCAM_PASSWORD):
            return {
                'statusCode': 401,
                'body': '<html><body><h1>401 Unauthorized</h1><p>Access denied.</p></body></html>',
                'headers': {
                    'Content-Type': 'text/html',
                    'WWW-Authenticate': 'Basic realm="Garden Camera"'
                }
            }

        # Read cached summary from S3 (updated hourly by gardencam-storage-summary Lambda)
        s3 = boto3.client("s3", region_name=GARDENCAM_REGION)
        cache_key = "stats/s3-storage-summary.json"
        cache_error = None

        try:
            response = s3.get_object(Bucket=GARDENCAM_BUCKET, Key=cache_key)
            summary = json.loads(response['Body'].read().decode('utf-8'))
        except Exception as e:
            cache_error = str(e)
            summary = None

        if summary:
            # Extract data from cached summary
            total_files = summary.get('total_count', 0)
            total_size_gb = summary.get('total_size_gb', 0)
            costs = summary.get('costs', {})
            storage_cost = costs.get('monthly_storage_cost_usd', 0)
            put_cost = costs.get('monthly_put_cost_usd', 0)
            get_cost = costs.get('monthly_get_cost_usd', 0)
            total_monthly = costs.get('total_monthly_cost_usd', storage_cost)
            yearly_total = costs.get('yearly_total_cost_usd', total_monthly * 12)
            weekly_stats = summary.get('weekly_stats', {})
            generated_at = summary.get('generated_at', 'Unknown')

            # Sort weeks
            sorted_weeks = sorted(weekly_stats.items(), reverse=True)

            # Prepare chart data (last 12 weeks, oldest first)
            chart_weeks = []
            chart_counts = []
            chart_sizes = []

            for week, data in reversed(sorted_weeks[:12]):
                chart_weeks.append(week)
                chart_counts.append(data['count'])
                chart_sizes.append(data.get('size_gb', 0))

            html += f'''
        <title>S3 Storage Statistics</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 0; padding: 1rem; background: #1a1a1a; color: #fff; }}
            .nav {{ text-align: center; margin-bottom: 1.5rem; }}
            .nav a {{ color: #4a9eff; text-decoration: none; margin: 0 1rem; padding: 0.5rem 1rem; background: #2a2a2a; border-radius: 6px; display: inline-block; }}
            .nav a:hover {{ background: #3a3a3a; }}
            h1 {{ text-align: center; margin-bottom: 2rem; }}
            .chart-container {{ max-width: 1400px; margin: 0 auto 3rem auto; background: #2a2a2a; padding: 1.5rem; border-radius: 8px; }}
            .chart-title {{ font-size: 1.2rem; margin-bottom: 1rem; color: #aaa; text-align: center; }}
            canvas {{ max-height: 400px; }}
            .stats-summary {{ max-width: 1400px; margin: 0 auto 2rem auto; padding: 1rem; background: #2a2a2a; border-radius: 8px; }}
            .stats-summary h2 {{ margin-top: 0; color: #aaa; }}
            .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; }}
            .stat-box {{ background: #1a1a1a; padding: 1rem; border-radius: 6px; text-align: center; }}
            .stat-value {{ font-size: 2rem; font-weight: bold; color: #4a9eff; }}
            .stat-label {{ color: #888; margin-top: 0.5rem; }}
            .weekly-table {{ width: 100%; border-collapse: collapse; margin-top: 1rem; }}
            .weekly-table th, .weekly-table td {{ padding: 0.5rem; text-align: left; border-bottom: 1px solid #3a3a3a; }}
            .weekly-table th {{ color: #aaa; background: #1a1a1a; }}
            .weekly-table td {{ font-family: monospace; }}
        </style>
        <div class="nav">
            <a href="../../contents">Home</a>
            <a href="../gardencam">Latest</a>
            <a href="gallery">Gallery</a>
            <a href="videos">Videos</a>
            <a href="stats">Capture Stats</a>
        </div>
        <h1>S3 Storage Statistics</h1>

        <div class="stats-summary">
            <h2>Total Storage</h2>
            <div class="stats-grid">
                <div class="stat-box">
                    <div class="stat-value">{total_files:,}</div>
                    <div class="stat-label">Total Files</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">{total_size_gb:.2f} GB</div>
                    <div class="stat-label">Total Size</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">${total_monthly:.3f}</div>
                    <div class="stat-label">Monthly Total</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">${yearly_total:.2f}</div>
                    <div class="stat-label">Yearly Total</div>
                </div>
            </div>
            <div style="margin-top: 1rem; color: #666; font-size: 0.85rem;">
                Breakdown: Storage ${storage_cost:.4f} + PUT requests ${put_cost:.4f} + GET requests ${get_cost:.4f}
            </div>
        </div>

        <div class="chart-container">
            <div class="chart-title">Files per Week</div>
            <canvas id="countChart"></canvas>
        </div>

        <div class="chart-container">
            <div class="chart-title">Storage Size per Week (GB)</div>
            <canvas id="sizeChart"></canvas>
        </div>

        <div class="stats-summary">
            <h2>Weekly Breakdown</h2>
            <table class="weekly-table">
                <thead>
                    <tr>
                        <th>Week</th>
                        <th>Files</th>
                        <th>Size</th>
                        <th>Weekly Cost</th>
                    </tr>
                </thead>
                <tbody>
        '''

            for week, data in sorted_weeks:
                size_gb = data.get('size_gb', 0)
                size_bytes = data.get('size', 0)
                size_mb = size_bytes / 1048576 if size_bytes else size_gb * 1024
                weekly_cost = data.get('weekly_cost_usd', 0)
                size_display = f"{size_mb:.1f} MB" if size_gb < 1 else f"{size_gb:.2f} GB"
                html += f'''
                    <tr>
                        <td>{week}</td>
                        <td>{data['count']:,}</td>
                        <td>{size_display}</td>
                        <td>${weekly_cost:.4f}</td>
                    </tr>
                '''

            html += f'''
                </tbody>
            </table>
            <p style="color: #666; font-size: 0.85rem; margin-top: 1rem;">Last updated: {generated_at}</p>
        </div>

        <script>
        const chartWeeks = {json.dumps(chart_weeks)};
        const chartCounts = {json.dumps(chart_counts)};
        const chartSizes = {json.dumps(chart_sizes)};

        const chartOptions = {{
            responsive: true,
            maintainAspectRatio: true,
            scales: {{
                x: {{ ticks: {{ color: '#888' }}, grid: {{ color: '#333' }} }},
                y: {{ ticks: {{ color: '#888' }}, grid: {{ color: '#333' }} }}
            }},
            plugins: {{ legend: {{ labels: {{ color: '#aaa' }} }} }}
        }};

        new Chart(document.getElementById('countChart'), {{
            type: 'bar',
            data: {{
                labels: chartWeeks,
                datasets: [{{
                    label: 'Files',
                    data: chartCounts,
                    backgroundColor: '#4a9eff'
                }}]
            }},
            options: chartOptions
        }});

        new Chart(document.getElementById('sizeChart'), {{
            type: 'bar',
            data: {{
                labels: chartWeeks,
                datasets: [{{
                    label: 'Size (GB)',
                    data: chartSizes,
                    backgroundColor: '#10b981'
                }}]
            }},
            options: chartOptions
        }});
        </script>
            '''
        else:
            # Cache not available - show error
            html += f'''
            <title>S3 Storage Statistics</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 0; padding: 1rem; background: #1a1a1a; color: #fff; }}
                .nav {{ text-align: center; margin-bottom: 1.5rem; }}
                .nav a {{ color: #4a9eff; text-decoration: none; margin: 0 1rem; padding: 0.5rem 1rem; background: #2a2a2a; border-radius: 6px; display: inline-block; }}
                .error {{ max-width: 800px; margin: 2rem auto; padding: 2rem; background: #2a2a2a; border-radius: 8px; text-align: center; }}
                .error h1 {{ color: #ef4444; }}
            </style>
            <div class="nav">
                <a href="../../contents">Home</a>
                <a href="../gardencam">Latest</a>
            </div>
            <div class="error">
                <h1>Cache Not Available</h1>
                <p>The storage summary cache has not been generated yet.</p>
                <p style="color: #888;">Error: {cache_error}</p>
                <p style="color: #666; font-size: 0.9rem;">The cache is updated hourly by a scheduled Lambda function.</p>
            </div>
            '''

    elif path.startswith(f'/{stage}/gardencam/videos') or path.startswith('/gardencam/videos'):
        # Timelapse video gallery page
        if not check_basic_auth(event, GARDENCAM_PASSWORD):
            return {
                'statusCode': 401,
                'body': '<html><body><h1>401 Unauthorized</h1><p>Access denied.</p></body></html>',
                'headers': {
                    'Content-Type': 'text/html',
                    'WWW-Authenticate': 'Basic realm="Garden Camera"'
                }
            }

        # Get timelapse videos from S3
        s3 = boto3.client("s3", region_name=GARDENCAM_REGION)
        videos = []

        try:
            # List videos from S3
            response = s3.list_objects_v2(
                Bucket=GARDENCAM_BUCKET,
                Prefix='videos/timelapse_'
            )

            if 'Contents' in response:
                for obj in response['Contents']:
                    key = obj['Key']
                    if key.endswith('.mp4'):
                        # Extract video ID from filename: videos/timelapse_YYYYMMDD-YYYYMMDD.mp4
                        video_id = key.replace('videos/', '').replace('.mp4', '')

                        # Parse date range from filename
                        try:
                            date_part = video_id.replace('timelapse_', '')
                            start_date, end_date = date_part.split('-')
                            start_formatted = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
                            end_formatted = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
                        except:
                            start_formatted = "Unknown"
                            end_formatted = "Unknown"

                        # Get metadata from DynamoDB if available
                        try:
                            dynamodb = boto3.resource('dynamodb', region_name=GARDENCAM_REGION)
                            metadata_table = dynamodb.Table('gardencam-video-metadata')
                            metadata_response = metadata_table.get_item(Key={'video_id': video_id})

                            if 'Item' in metadata_response:
                                item = metadata_response['Item']
                                frame_count = int(item.get('frame_count', 0))
                                duration = int(item.get('duration_seconds', 5))
                            else:
                                frame_count = 0
                                duration = 5
                        except:
                            frame_count = 0
                            duration = 5

                        # Generate presigned URL (valid for 1 hour)
                        presigned_url = s3.generate_presigned_url(
                            'get_object',
                            Params={'Bucket': GARDENCAM_BUCKET, 'Key': key},
                            ExpiresIn=3600
                        )

                        videos.append({
                            'id': video_id,
                            'key': key,
                            'url': presigned_url,
                            'size_mb': obj['Size'] / 1048576,
                            'last_modified': obj['LastModified'].isoformat(),
                            'start_date': start_formatted,
                            'end_date': end_formatted,
                            'frame_count': frame_count,
                            'duration': duration
                        })

            # Sort by video ID (date) descending
            videos.sort(key=lambda v: v['id'], reverse=True)

        except Exception as e:
            print(f"Error listing videos: {e}")
            videos = []

        # Render HTML
        html += f'''
        <title>Timelapse Videos - Garden Camera</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 0; padding: 1rem; background: #1a1a1a; color: #fff; }}
            .nav {{ text-align: center; margin-bottom: 1.5rem; }}
            .nav a {{ color: #4a9eff; text-decoration: none; margin: 0 1rem; padding: 0.5rem 1rem; background: #2a2a2a; border-radius: 6px; display: inline-block; }}
            .nav a:hover {{ background: #3a3a3a; }}
            h1 {{ text-align: center; margin-bottom: 2rem; }}
            .video-grid {{ max-width: 1400px; margin: 0 auto; display: grid; grid-template-columns: repeat(auto-fill, minmax(600px, 1fr)); gap: 2rem; }}
            .video-card {{ background: #2a2a2a; border-radius: 8px; overflow: hidden; padding: 1rem; }}
            .video-card video {{ width: 100%; border-radius: 6px; background: #000; }}
            .video-metadata {{ margin-top: 1rem; }}
            .video-metadata h3 {{ margin: 0 0 0.5rem 0; color: #4a9eff; }}
            .video-metadata p {{ margin: 0.25rem 0; color: #aaa; font-size: 0.9rem; }}
            .download-btn {{ display: inline-block; margin-top: 0.5rem; padding: 0.5rem 1rem; background: #4a9eff; color: #fff; text-decoration: none; border-radius: 4px; font-size: 0.9rem; }}
            .download-btn:hover {{ background: #3a8eef; }}
            .no-videos {{ max-width: 800px; margin: 2rem auto; padding: 2rem; background: #2a2a2a; border-radius: 8px; text-align: center; }}
        </style>
        <div class="nav">
            <a href="../../contents">Home</a>
            <a href="../gardencam">Latest</a>
            <a href="gallery">Gallery</a>
            <a href="stats">Capture Stats</a>
            <a href="s3-stats">Storage Stats</a>
        </div>
        <h1>Timelapse Videos</h1>
        '''

        if videos:
            html += '<div class="video-grid">'
            for video in videos:
                html += f'''
                <div class="video-card">
                    <video controls loop preload="metadata">
                        <source src="{video['url']}" type="video/mp4">
                        Your browser does not support the video tag.
                    </video>
                    <div class="video-metadata">
                        <h3>Week of {video['start_date']} to {video['end_date']}</h3>
                        <p>{video['frame_count']} frames | {video['duration']}s duration | {video['size_mb']:.1f} MB</p>
                        <a href="{video['url']}" download class="download-btn">Download MP4</a>
                    </div>
                </div>
                '''
            html += '</div>'
        else:
            html += '''
            <div class="no-videos">
                <h2>No Videos Yet</h2>
                <p>Timelapse videos are generated weekly on Sundays at 2 AM UTC.</p>
                <p style="color: #666; margin-top: 1rem;">Videos will appear here once the first weekly generation completes.</p>
            </div>
            '''

    elif path == f'/{stage}/gardencam' or path == '/gardencam':
        # Check authentication
        if not check_basic_auth(event, GARDENCAM_PASSWORD):
            return {
                'statusCode': 401,
                'body': '<html><body><h1>401 Unauthorized</h1><p>Access denied.</p></body></html>',
                'headers': {
                    'Content-Type': 'text/html',
                    'WWW-Authenticate': 'Basic realm="Garden Camera"'
                }
            }

        images = get_latest_gardencam_images(3)
        if images:
            html += '''
            <title>Garden Camera</title>
            <style>
                body { font-family: Arial, sans-serif; text-align: center; margin: 1rem; background: #1a1a1a; color: #fff; }
                h1 { margin-bottom: 1rem; font-size: 2rem; }
                .gallery-link { display: inline-block; margin-bottom: 1.5rem; padding: 0.5rem 1.5rem; background: #4a5568; color: #fff; text-decoration: none; border-radius: 6px; transition: background 0.3s; }
                .gallery-link:hover { background: #5a6578; }
                .gallery { display: flex; gap: 1rem; justify-content: center; flex-wrap: wrap; max-width: 1024px; margin: 0 auto; }
                .image-container { flex: 1; min-width: 280px; max-width: 340px; }
                .image-container a { display: block; cursor: pointer; }
                .image-container img { width: 100%; height: auto; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); transition: transform 0.3s; }
                .image-container img:hover { transform: scale(1.05); }
                .timestamp { color: #888; margin-top: 0.5rem; font-size: 0.9rem; }
                .label { color: #aaa; font-weight: bold; margin-bottom: 0.5rem; font-size: 1rem; }

                /* Mobile/Tablet - stack vertically */
                @media (max-width: 1024px) {
                    body { margin: 0.5rem; }
                    h1 { font-size: 1.5rem; margin-bottom: 0.75rem; }
                    .gallery { flex-direction: column; gap: 1rem; }
                    .image-container { min-width: 100%; max-width: 100%; }
                    .label { font-size: 1rem; }
                    .timestamp { font-size: 0.85rem; }
                }
            </style>
            <div style="text-align: center; margin-bottom: 1rem;">
                <a href="contents" style="color: #4a9eff; text-decoration: none;">Home</a>
            </div>
            <h1>Garden Camera</h1>
            <a href="gardencam/gallery" class="gallery-link">View Full Gallery</a>
            <a href="gardencam/videos" class="gallery-link" style="margin-left: 0.5rem;">🎬 Timelapse Videos</a>
            <a href="gardencam/stats" class="gallery-link" style="margin-left: 0.5rem;">Capture Stats</a>
            <a href="gardencam/s3-stats" class="gallery-link" style="margin-left: 0.5rem;">Storage Stats</a>
            <button id="captureBtn" class="gallery-link" style="margin-left: 0.5rem; cursor: pointer;">📷 Capture Now</button>
            <div id="captureStatus" style="margin-top: 0.5rem; font-size: 0.9rem;"></div>
            <script>
            document.getElementById('captureBtn').addEventListener('click', function() {{
                const btn = this;
                const status = document.getElementById('captureStatus');
                btn.disabled = true;
                btn.textContent = '📷 Capturing...';
                status.textContent = 'Sending capture command...';
                status.style.color = '#4a9eff';

                fetch('gardencam/capture', {{ method: 'POST' }})
                    .then(response => response.json())
                    .then(data => {{
                        status.textContent = data.message || 'Capture command sent! Image will appear in ~30 seconds.';
                        status.style.color = '#10b981';
                        setTimeout(() => {{
                            btn.disabled = false;
                            btn.textContent = '📷 Capture Now';
                        }}, 3000);
                    }})
                    .catch(error => {{
                        status.textContent = 'Error: ' + error.message;
                        status.style.color = '#ef4444';
                        btn.disabled = false;
                        btn.textContent = '📷 Capture Now';
                    }});
            }});
            </script>
            <div class="gallery">
            '''
            labels = ['Latest', 'Previous', 'Earlier']
            for idx, img in enumerate(images):
                label = labels[idx] if idx < len(labels) else f'Image {idx+1}'
                resolution_display = f" • {img['resolution']}" if img.get('resolution') else ""
                html += f'''
                <div class="image-container">
                    <div class="label">{label}</div>
                    <a href="gardencam/display?key={img['key']}">
                        <img src="{img['url']}" alt="{label} capture">
                    </a>
                    <p class="timestamp">{img['timestamp']}{resolution_display}</p>
                </div>
                '''
            html += '</div>'
        else:
            html += '<title>Garden Camera</title><h1>Garden Camera</h1><p>No images available yet.</p>'

    elif path == f'/{stage}/lambda-stats' or path == '/lambda-stats':
        # Lambda execution statistics page
        stats = get_lambda_execution_stats(limit=5000)

        # Aggregate data by day
        from collections import defaultdict
        from decimal import Decimal
        from datetime import datetime, timedelta

        daily_stats = defaultdict(lambda: {
            'count': 0,
            'total_duration_ms': Decimal('0'),
            'total_cost_usd': Decimal('0'),
            'paths': defaultdict(int),
            'gb_seconds': Decimal('0')
        })

        # Track all unique paths for the stacked chart
        all_paths = set()

        for item in stats:
            ts = item.get('timestamp', '')
            if ts:
                date = ts.split('T')[0]  # Get just the date part
                daily_stats[date]['count'] += 1
                daily_stats[date]['total_duration_ms'] += Decimal(str(item.get('duration_ms', 0)))
                daily_stats[date]['total_cost_usd'] += Decimal(str(item.get('estimated_cost_usd', 0)))
                path_item = item.get('path', 'unknown')
                daily_stats[date]['paths'][path_item] += 1
                all_paths.add(path_item)

                # Calculate GB-seconds for free tier tracking
                memory_gb = Decimal(str(item.get('memory_limit_mb', 512))) / Decimal('1024')
                duration_seconds = Decimal(str(item.get('duration_ms', 0))) / Decimal('1000')
                daily_stats[date]['gb_seconds'] += memory_gb * duration_seconds

        # Sort by date
        sorted_dates = sorted(daily_stats.keys(), reverse=True)

        # Prepare chart data
        chart_dates = []
        chart_counts = []
        chart_durations = []
        chart_costs = []
        chart_path_data = defaultdict(list)

        for date in reversed(sorted_dates[-30:]):  # Last 30 days
            chart_dates.append(date)
            chart_counts.append(daily_stats[date]['count'])
            chart_durations.append(float(daily_stats[date]['total_duration_ms']))
            chart_costs.append(float(daily_stats[date]['total_cost_usd']))

            # Collect path counts for stacked chart
            for path_name in all_paths:
                chart_path_data[path_name].append(daily_stats[date]['paths'].get(path_name, 0))

        total_executions = sum(d['count'] for d in daily_stats.values())
        total_cost = sum(d['total_cost_usd'] for d in daily_stats.values())
        total_duration = sum(d['total_duration_ms'] for d in daily_stats.values())
        total_gb_seconds = sum(d['gb_seconds'] for d in daily_stats.values())

        # Calculate current month stats for free tier tracking
        today = datetime.utcnow()
        month_start = today.replace(day=1).strftime('%Y-%m-%d')
        current_month_stats = {
            'requests': 0,
            'gb_seconds': Decimal('0'),
            'cost': Decimal('0')
        }

        for date_str, data in daily_stats.items():
            if date_str >= month_start:
                current_month_stats['requests'] += data['count']
                current_month_stats['gb_seconds'] += data['gb_seconds']
                current_month_stats['cost'] += data['total_cost_usd']

        # AWS Lambda Free Tier limits (monthly)
        FREE_TIER_REQUESTS = 1_000_000
        FREE_TIER_GB_SECONDS = 400_000

        # Calculate usage percentages
        request_usage_pct = (current_month_stats['requests'] / FREE_TIER_REQUESTS) * 100
        gb_seconds_usage_pct = (float(current_month_stats['gb_seconds']) / FREE_TIER_GB_SECONDS) * 100

        # Calculate costs if free tier was exceeded
        excess_requests = max(0, current_month_stats['requests'] - FREE_TIER_REQUESTS)
        excess_gb_seconds = max(0, float(current_month_stats['gb_seconds']) - FREE_TIER_GB_SECONDS)

        excess_request_cost = (excess_requests / 1_000_000) * 0.20
        excess_compute_cost = excess_gb_seconds * 0.0000166667
        total_excess_cost = excess_request_cost + excess_compute_cost

        # Monthly projection (days elapsed in month)
        days_in_month = (today.replace(month=today.month % 12 + 1, day=1) if today.month < 12
                         else today.replace(year=today.year + 1, month=1, day=1)) - today.replace(day=1)
        days_elapsed = today.day
        projection_multiplier = days_in_month.days / days_elapsed if days_elapsed > 0 else 1

        projected_requests = int(current_month_stats['requests'] * projection_multiplier)
        projected_gb_seconds = float(current_month_stats['gb_seconds']) * projection_multiplier
        projected_cost = float(current_month_stats['cost']) * projection_multiplier

        # Calculate top paths by total count
        path_totals = defaultdict(int)
        for data in daily_stats.values():
            for path_name, count in data['paths'].items():
                path_totals[path_name] += count

        top_paths = sorted(path_totals.items(), key=lambda x: x[1], reverse=True)[:10]

        # Determine free tier status color
        free_tier_status = 'good' if request_usage_pct < 50 else ('warning' if request_usage_pct < 80 else 'danger')
        status_colors = {'good': '#32cd32', 'warning': '#ffa500', 'danger': '#ff4444'}

        html += f'''
        <title>Lambda Execution Statistics</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 0; padding: 1rem; background: #1a1a1a; color: #fff; }}
            .nav {{ text-align: center; margin-bottom: 1.5rem; }}
            .nav a {{ color: #4a9eff; text-decoration: none; margin: 0 1rem; padding: 0.5rem 1rem; background: #2a2a2a; border-radius: 6px; display: inline-block; }}
            .nav a:hover {{ background: #3a3a3a; }}
            h1 {{ text-align: center; margin-bottom: 2rem; }}
            .chart-container {{ max-width: 1400px; margin: 0 auto 3rem auto; background: #2a2a2a; padding: 1.5rem; border-radius: 8px; }}
            .chart-title {{ font-size: 1.2rem; margin-bottom: 1rem; color: #aaa; text-align: center; }}
            canvas {{ max-height: 400px; }}
            .stats-summary {{ max-width: 1400px; margin: 0 auto 2rem auto; padding: 1rem; background: #2a2a2a; border-radius: 8px; }}
            .stats-summary h2 {{ margin-top: 0; color: #aaa; }}
            .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; }}
            .stat-box {{ background: #1a1a1a; padding: 1rem; border-radius: 6px; text-align: center; }}
            .stat-value {{ font-size: 2rem; font-weight: bold; color: #4a9eff; }}
            .stat-label {{ color: #888; margin-top: 0.5rem; }}
            .daily-table {{ width: 100%; border-collapse: collapse; margin-top: 1rem; }}
            .daily-table th, .daily-table td {{ padding: 0.5rem; text-align: left; border-bottom: 1px solid #3a3a3a; }}
            .daily-table th {{ color: #aaa; background: #1a1a1a; }}
            .free-tier-box {{ background: #1a1a1a; padding: 1.5rem; border-radius: 6px; border-left: 4px solid {status_colors[free_tier_status]}; }}
            .progress-bar {{ background: #3a3a3a; height: 20px; border-radius: 10px; overflow: hidden; margin: 0.5rem 0; }}
            .progress-fill {{ height: 100%; transition: width 0.3s; }}
            .metric-row {{ display: flex; justify-content: space-between; align-items: center; margin: 1rem 0; }}
            .metric-name {{ color: #aaa; }}
            .metric-value {{ color: #fff; font-weight: bold; }}
            .projection {{ background: #252525; padding: 1rem; border-radius: 6px; margin-top: 1rem; }}
            .projection-label {{ color: #888; font-size: 0.9rem; }}
            .projection-value {{ color: #ffa500; font-size: 1.3rem; font-weight: bold; }}
        </style>
        <div class="nav">
            <a href="../contents">Home</a>
        </div>
        <h1>Lambda Execution Statistics</h1>

        <div class="stats-summary">
            <h2>AWS Free Tier Usage (Current Month)</h2>
            <div class="free-tier-box">
                <div class="metric-row">
                    <span class="metric-name">Requests</span>
                    <span class="metric-value">{current_month_stats['requests']:,} / {FREE_TIER_REQUESTS:,} ({request_usage_pct:.1f}%)</span>
                </div>
                <div class="progress-bar">
                    <div class="progress-fill" style="width: {min(request_usage_pct, 100):.1f}%; background: {status_colors[free_tier_status]};"></div>
                </div>

                <div class="metric-row" style="margin-top: 1.5rem;">
                    <span class="metric-name">Compute (GB-seconds)</span>
                    <span class="metric-value">{float(current_month_stats['gb_seconds']):,.1f} / {FREE_TIER_GB_SECONDS:,} ({gb_seconds_usage_pct:.1f}%)</span>
                </div>
                <div class="progress-bar">
                    <div class="progress-fill" style="width: {min(gb_seconds_usage_pct, 100):.1f}%; background: {status_colors[free_tier_status]};"></div>
                </div>

                <div class="projection">
                    <div class="projection-label">Monthly Projection (based on {days_elapsed} days):</div>
                    <div style="display: grid; grid-template-columns: repeat(3, 1fr); gap: 1rem; margin-top: 0.5rem;">
                        <div>
                            <div class="projection-label">Requests</div>
                            <div class="projection-value">{projected_requests:,}</div>
                        </div>
                        <div>
                            <div class="projection-label">GB-seconds</div>
                            <div class="projection-value">{projected_gb_seconds:,.0f}</div>
                        </div>
                        <div>
                            <div class="projection-label">Projected Cost</div>
                            <div class="projection-value">${projected_cost:.4f}</div>
                        </div>
                    </div>
                </div>

                <div class="projection" style="background: #2a1a1a;">
                    <div class="projection-label">Cost if Free Tier Exceeded (This Month):</div>
                    <div class="projection-value" style="color: #ff6b6b;">${total_excess_cost:.4f}</div>
                    <div style="font-size: 0.85rem; color: #888; margin-top: 0.5rem;">
                        ({excess_requests:,} excess requests @ $0.20/M + {excess_gb_seconds:,.0f} excess GB-seconds)
                    </div>
                </div>
            </div>
        </div>

        <div class="stats-summary">
            <h2>Summary (All Time - Last {len(stats)} executions)</h2>
            <div class="stats-grid">
                <div class="stat-box">
                    <div class="stat-value">{total_executions:,}</div>
                    <div class="stat-label">Total Executions</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">${float(total_cost):.4f}</div>
                    <div class="stat-label">Total Cost (USD)</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">{float(total_duration)/1000:.1f}s</div>
                    <div class="stat-label">Total Runtime</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">{float(total_duration)/total_executions if total_executions > 0 else 0:.0f}ms</div>
                    <div class="stat-label">Avg Duration</div>
                </div>
            </div>
        </div>

        <div class="chart-container">
            <div class="chart-title">Executions by Endpoint (Last 30 Days)</div>
            <canvas id="pathChart"></canvas>
        </div>

        <div class="chart-container">
            <div class="chart-title">Daily Execution Count</div>
            <canvas id="countChart"></canvas>
        </div>

        <div class="chart-container">
            <div class="chart-title">Daily Total Duration (seconds)</div>
            <canvas id="durationChart"></canvas>
        </div>

        <div class="chart-container">
            <div class="chart-title">Daily Cost (USD)</div>
            <canvas id="costChart"></canvas>
        </div>

        <div class="stats-summary">
            <h2>Top Endpoints by Request Count</h2>
            <table class="daily-table">
                <thead>
                    <tr>
                        <th>Endpoint</th>
                        <th>Total Requests</th>
                        <th>Percentage</th>
                    </tr>
                </thead>
                <tbody>
        '''

        for path_name, count in top_paths:
            percentage = (count / total_executions * 100) if total_executions > 0 else 0
            html += f'''
                    <tr>
                        <td>{path_name if path_name else '(root)'}</td>
                        <td>{count:,}</td>
                        <td>{percentage:.1f}%</td>
                    </tr>
            '''

        html += '''
                </tbody>
            </table>
        </div>

        <div class="stats-summary">
            <h2>Daily Breakdown</h2>
            <table class="daily-table">
                <thead>
                    <tr>
                        <th>Date</th>
                        <th>Executions</th>
                        <th>Total Duration</th>
                        <th>Avg Duration</th>
                        <th>Cost (USD)</th>
                    </tr>
                </thead>
                <tbody>
        '''

        for date in sorted_dates[:30]:  # Show last 30 days
            day_data = daily_stats[date]
            avg_duration = float(day_data['total_duration_ms']) / day_data['count'] if day_data['count'] > 0 else 0
            html += f'''
                    <tr>
                        <td>{date}</td>
                        <td>{day_data['count']:,}</td>
                        <td>{float(day_data['total_duration_ms'])/1000:.1f}s</td>
                        <td>{avg_duration:.0f}ms</td>
                        <td>${float(day_data['total_cost_usd']):.4f}</td>
                    </tr>
            '''

        html += '''
                </tbody>
            </table>
        </div>

        <script>
        const chartDates = ''' + str(chart_dates) + ''';
        const chartCounts = ''' + str(chart_counts) + ''';
        const chartDurations = ''' + str([d/1000 for d in chart_durations]) + ''';
        const chartCosts = ''' + str(chart_costs) + ''';

        // Prepare path breakdown data
        const pathNames = ''' + str(list(all_paths)[:10]) + ''';  // Top 10 paths
        const pathData = ''' + str({k: v[:len(chart_dates)] if len(v) >= len(chart_dates) else v + [0]*(len(chart_dates)-len(v))
                                     for k, v in list(chart_path_data.items())[:10]}) + ''';

        // Generate colors for paths
        const pathColors = [
            '#4a9eff', '#32cd32', '#ffa500', '#ff69b4', '#9370db',
            '#00ced1', '#ff6347', '#98fb98', '#dda0dd', '#f0e68c'
        ];

        // Path Breakdown Stacked Chart
        const pathDatasets = pathNames.map((pathName, idx) => ({
            label: pathName || '(root)',
            data: pathData[pathName] || [],
            backgroundColor: pathColors[idx % pathColors.length],
            borderColor: pathColors[idx % pathColors.length],
            borderWidth: 1
        }));

        new Chart(document.getElementById('pathChart'), {
            type: 'bar',
            data: {
                labels: chartDates,
                datasets: pathDatasets
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        labels: { color: '#fff' },
                        position: 'bottom'
                    }
                },
                scales: {
                    x: {
                        stacked: true,
                        ticks: { color: '#aaa' },
                        grid: { color: '#3a3a3a' }
                    },
                    y: {
                        stacked: true,
                        ticks: { color: '#aaa' },
                        grid: { color: '#3a3a3a' }
                    }
                }
            }
        });

        // Count Chart
        new Chart(document.getElementById('countChart'), {
            type: 'line',
            data: {
                labels: chartDates,
                datasets: [{
                    label: 'Executions',
                    data: chartCounts,
                    borderColor: '#4a9eff',
                    backgroundColor: 'rgba(74, 158, 255, 0.1)',
                    tension: 0.3
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { labels: { color: '#fff' } } },
                scales: {
                    x: { ticks: { color: '#aaa' }, grid: { color: '#3a3a3a' } },
                    y: { ticks: { color: '#aaa' }, grid: { color: '#3a3a3a' } }
                }
            }
        });

        // Duration Chart
        new Chart(document.getElementById('durationChart'), {
            type: 'bar',
            data: {
                labels: chartDates,
                datasets: [{
                    label: 'Duration (seconds)',
                    data: chartDurations,
                    backgroundColor: '#6a5acd',
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { labels: { color: '#fff' } } },
                scales: {
                    x: { ticks: { color: '#aaa' }, grid: { color: '#3a3a3a' } },
                    y: { ticks: { color: '#aaa' }, grid: { color: '#3a3a3a' } }
                }
            }
        });

        // Cost Chart
        new Chart(document.getElementById('costChart'), {
            type: 'bar',
            data: {
                labels: chartDates,
                datasets: [{
                    label: 'Cost (USD)',
                    data: chartCosts,
                    backgroundColor: '#32cd32',
                }]
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: { legend: { labels: { color: '#fff' } } },
                scales: {
                    x: { ticks: { color: '#aaa' }, grid: { color: '#3a3a3a' } },
                    y: { ticks: { color: '#aaa' }, grid: { color: '#3a3a3a' } }
                }
            }
        });
        </script>
        '''

    elif path.startswith(f'/{stage}/memspeed/upload') or path.startswith('/memspeed/upload'):
        # Memspeed upload endpoint
        if not check_basic_auth(event, GARDENCAM_PASSWORD):
            return {
                'statusCode': 401,
                'body': json.dumps({'error': 'Unauthorized'}),
                'headers': {
                    'Content-Type': 'application/json',
                    'WWW-Authenticate': 'Basic realm="memspeed"'
                }
            }

        try:
            body = event.get('body', '{}')
            if event.get('isBase64Encoded', False):
                body = base64.b64decode(body).decode('utf-8')
            data = json.loads(body)
            success, result = save_memspeed_result(data)
            if success:
                return {
                    'statusCode': 200,
                    'body': json.dumps({'message': 'Upload successful', 'key': result}),
                    'headers': {'Content-Type': 'application/json'}
                }
            else:
                return {
                    'statusCode': 400,
                    'body': json.dumps({'error': result}),
                    'headers': {'Content-Type': 'application/json'}
                }
        except json.JSONDecodeError as e:
            return {
                'statusCode': 400,
                'body': json.dumps({'error': f'Invalid JSON: {str(e)}'}),
                'headers': {'Content-Type': 'application/json'}
            }

    elif path.startswith(f'/{stage}/memspeed/download') or path.startswith('/memspeed/download'):
        # Memspeed download endpoint - redirect to presigned URL
        if not check_basic_auth(event, GARDENCAM_PASSWORD):
            return {
                'statusCode': 401,
                'body': json.dumps({'error': 'Unauthorized'}),
                'headers': {
                    'Content-Type': 'application/json',
                    'WWW-Authenticate': 'Basic realm="memspeed"'
                }
            }

        query_params = event.get('queryStringParameters', {}) or {}
        filename = query_params.get('file', '')
        if not filename:
            return {
                'statusCode': 400,
                'body': json.dumps({'error': 'Missing file parameter'}),
                'headers': {'Content-Type': 'application/json'}
            }

        key = f"{MEMSPEED_DOWNLOADS_PREFIX}{filename}"
        url = get_memspeed_download_url(key)
        if url:
            return {
                'statusCode': 302,
                'body': '',
                'headers': {'Location': url}
            }
        else:
            return {
                'statusCode': 404,
                'body': json.dumps({'error': 'File not found'}),
                'headers': {'Content-Type': 'application/json'}
            }

    elif path.startswith(f'/{stage}/memspeed/data') or path.startswith('/memspeed/data'):
        # Memspeed JSON API
        if not check_basic_auth(event, GARDENCAM_PASSWORD):
            return {
                'statusCode': 401,
                'body': json.dumps({'error': 'Unauthorized'}),
                'headers': {
                    'Content-Type': 'application/json',
                    'WWW-Authenticate': 'Basic realm="memspeed"'
                }
            }

        results = get_memspeed_results()
        # Remove internal _key field
        for r in results:
            r.pop('_key', None)

        return {
            'statusCode': 200,
            'body': json.dumps({'results': results}),
            'headers': {
                'Content-Type': 'application/json',
                'Access-Control-Allow-Origin': '*'
            }
        }

    elif path == f'/{stage}/memspeed' or path == '/memspeed':
        # Memspeed main page
        if not check_basic_auth(event, GARDENCAM_PASSWORD):
            return {
                'statusCode': 401,
                'body': '<html><body><h1>401 Unauthorized</h1><p>Access denied.</p></body></html>',
                'headers': {
                    'Content-Type': 'text/html',
                    'WWW-Authenticate': 'Basic realm="memspeed"'
                }
            }

        results = get_memspeed_results()
        downloads = get_memspeed_downloads()
        html += render_memspeed_page(results, downloads)

    elif path == f'/{stage}/t3' or path == '/t3':
        # Terse Transport Times - K2 bus arrivals
        api_key = TFL_API_KEY

        # Get stop parameter (default to parklands)
        query_params = event.get('queryStringParameters', {}) or {}
        stop = query_params.get('stop', 'parklands').lower()
        if stop not in T3_STOPS:
            stop = 'parklands'

        arrivals, error = t3_fetch_arrivals(api_key, stop)

        # Check if JSON is requested
        headers = event.get('headers', {}) or {}
        accept = headers.get('Accept', headers.get('accept', 'text/html'))

        if 'application/json' in accept:
            # Return JSON for API consumers (e.g., Android app)
            duration_ms = (time.time() - start_time) * 1000
            log_execution_metrics(context, duration_ms, path)

            if error:
                return {
                    'statusCode': 500,
                    'body': json.dumps({'error': error}),
                    'headers': {
                        'Content-Type': 'application/json',
                        'Access-Control-Allow-Origin': '*'
                    }
                }
            return {
                'statusCode': 200,
                'body': t3_format_json(arrivals, stop),
                'headers': {
                    'Content-Type': 'application/json',
                    'Access-Control-Allow-Origin': '*'
                }
            }

        # Return HTML for browsers
        if error:
            html += f"<h1>Error</h1><p>{error}</p>"
        else:
            html += t3_format_html(arrivals)

    else:
        html += open('cv.html', 'r').read()
    content = f'<html><head>{fav}{html}</body></html>'

    # Log execution metrics
    duration_ms = (time.time() - start_time) * 1000
    log_execution_metrics(context, duration_ms, path)

    return {
        'statusCode': 200,
        'body': content,
        'headers': {
            'Content-Type': 'text/html',
        }
    }

if __name__ == "__main__":
    # mock data
    event = {
        'requestContext': {'stage':'-stage-'},
        'headers': {
            'Host':'-host-',
            'X-Forwarded-For':'-ip-',
            'referer': '-referer'
        },
    }
    class Object(object):
        pass
    context = Object()
    context.log_group_name = '-log_group_name-'
    context.log_stream_name = '-log-stream-name-'

    # test all the code
    for p in 'event contents anything-else'.split():
        event['path'] = f'/-stage-/{p}'
        print(f'{p:<20}', len(pformat(lambda_handler(event, context))))

