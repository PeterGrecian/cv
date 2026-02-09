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
            'request_id': context.aws_request_id,
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


def log_execution_metrics(context, duration_ms, path='', ip='', user_agent=''):
    """Log Lambda execution metrics to DynamoDB with IP and User-Agent."""
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
            'request_id': context.aws_request_id,
            'duration_ms': Decimal(str(duration_ms)),
            'memory_limit_mb': Decimal(str(context.memory_limit_in_mb)),
            'path': path,
            'estimated_cost_usd': total_cost,
            'ip_address': ip,
            'user_agent': user_agent
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


def get_all_gardencam_images(max_keys=None):
    """Get all gardencam images from S3.

    Args:
        max_keys: If provided, only fetch the first N keys (optimization for latest images)
    """
    if not BOTO3_AVAILABLE:
        return []
    s3 = boto3.client("s3", region_name=GARDENCAM_REGION)

    # List objects with garden_ prefix (excludes thumbnails and averaged images)
    if max_keys:
        # Fast path: only get the most recent images
        # Since keys are reverse sorted by default when listed, we need to get more than we need
        # and then sort properly
        response = s3.list_objects_v2(
            Bucket=GARDENCAM_BUCKET,
            Prefix='garden_',
            MaxKeys=max_keys * 10  # Get extra to account for non-jpg files
        )
        all_objects = response.get("Contents", [])
    else:
        # Full scan: use paginator to handle >1000 objects
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

        # If we have a limit and reached it, stop
        if max_keys and len(images) >= max_keys:
            break

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
    """Get presigned URLs for the latest N images from S3.

    Optimized version that only fetches what's needed and uses thumbnails.
    """
    if not BOTO3_AVAILABLE:
        return []

    # Only fetch the images we need (not all thousands)
    all_images = get_all_gardencam_images(max_keys=count)
    images = []

    s3 = boto3.client("s3", region_name=GARDENCAM_REGION)

    for img in all_images[:count]:
        # Use thumbnail for display (they're pre-generated by gardencam.py)
        thumb_key = f"thumb_{img['key']}"
        display_url = get_presigned_url(thumb_key)

        # Full-res URL for click-through
        full_url = get_presigned_url(img['key'])

        # Try to get dimensions from DynamoDB stats (much faster than downloading image)
        resolution = ""
        try:
            dynamodb = boto3.resource('dynamodb', region_name=GARDENCAM_REGION)
            table = dynamodb.Table('gardencam-stats')
            response = table.get_item(Key={'filename': img['key']})
            if 'Item' in response:
                item = response['Item']
                width = item.get('width')
                height = item.get('height')
                if width and height:
                    resolution = f"{int(width)}×{int(height)}"
        except Exception as e:
            # If DynamoDB lookup fails, just skip resolution
            print(f"Could not get dimensions from DynamoDB for {img['key']}: {e}")
            pass

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


def get_all_lambda_functions():
    """Get list of all Lambda functions."""
    if not BOTO3_AVAILABLE:
        return []

    try:
        lambda_client = boto3.client('lambda', region_name=GARDENCAM_REGION)
        response = lambda_client.list_functions()
        functions = response.get('Functions', [])

        # Filter for cv and gardencam functions
        relevant = [f['FunctionName'] for f in functions
                   if 'cv' in f['FunctionName'].lower() or 'gardencam' in f['FunctionName'].lower()]
        return sorted(relevant)
    except Exception as e:
        print(f"Error listing Lambda functions: {e}")
        return []


def get_cloudwatch_metrics(function_name, days=30):
    """Get CloudWatch metrics for Lambda function."""
    if not BOTO3_AVAILABLE:
        return {}

    try:
        from datetime import timedelta
        cloudwatch = boto3.client('cloudwatch', region_name=GARDENCAM_REGION)

        end_time = datetime.utcnow()
        start_time = end_time - timedelta(days=days)

        metrics = {}

        # Get invocations
        response = cloudwatch.get_metric_statistics(
            Namespace='AWS/Lambda',
            MetricName='Invocations',
            Dimensions=[{'Name': 'FunctionName', 'Value': function_name}],
            StartTime=start_time,
            EndTime=end_time,
            Period=86400,  # 1 day
            Statistics=['Sum']
        )
        metrics['invocations'] = response.get('Datapoints', [])

        # Get errors
        response = cloudwatch.get_metric_statistics(
            Namespace='AWS/Lambda',
            MetricName='Errors',
            Dimensions=[{'Name': 'FunctionName', 'Value': function_name}],
            StartTime=start_time,
            EndTime=end_time,
            Period=86400,
            Statistics=['Sum']
        )
        metrics['errors'] = response.get('Datapoints', [])

        # Get throttles
        response = cloudwatch.get_metric_statistics(
            Namespace='AWS/Lambda',
            MetricName='Throttles',
            Dimensions=[{'Name': 'FunctionName', 'Value': function_name}],
            StartTime=start_time,
            EndTime=end_time,
            Period=86400,
            Statistics=['Sum']
        )
        metrics['throttles'] = response.get('Datapoints', [])

        # Get duration
        response = cloudwatch.get_metric_statistics(
            Namespace='AWS/Lambda',
            MetricName='Duration',
            Dimensions=[{'Name': 'FunctionName', 'Value': function_name}],
            StartTime=start_time,
            EndTime=end_time,
            Period=86400,
            Statistics=['Average', 'Maximum']
        )
        metrics['duration'] = response.get('Datapoints', [])

        return metrics
    except Exception as e:
        print(f"Error fetching CloudWatch metrics: {e}")
        return {}


def get_all_lambda_metrics(days=30):
    """Get CloudWatch metrics for all Lambda functions."""
    functions = get_all_lambda_functions()
    all_metrics = {}

    for func_name in functions:
        metrics = get_cloudwatch_metrics(func_name, days)
        if metrics:
            # Calculate totals
            total_invocations = sum(dp.get('Sum', 0) for dp in metrics.get('invocations', []))
            total_errors = sum(dp.get('Sum', 0) for dp in metrics.get('errors', []))
            total_throttles = sum(dp.get('Sum', 0) for dp in metrics.get('throttles', []))

            durations = [dp.get('Average', 0) for dp in metrics.get('duration', []) if dp.get('Average')]
            maxes = [dp.get('Maximum', 0) for dp in metrics.get('duration', []) if dp.get('Maximum')]

            avg_duration = sum(durations) / len(durations) if durations else 0
            max_duration = max(maxes) if maxes else 0
            error_rate = (total_errors / total_invocations * 100) if total_invocations > 0 else 0

            all_metrics[func_name] = {
                'invocations': total_invocations,
                'errors': total_errors,
                'throttles': total_throttles,
                'avg_duration': avg_duration,
                'max_duration': max_duration,
                'error_rate': error_rate,
                'raw_metrics': metrics
            }

    return all_metrics


def categorize_path(path):
    """Categorize path into application."""
    if not path or path == '/':
        return 'root'

    path = path.lower()

    if 'gardencam' in path:
        return 'gardencam'
    elif 't3' in path or 'parklands' in path or 'surbiton' in path:
        return 't3-bus'
    elif 'lambda-stats' in path:
        return 'lambda-stats'
    elif 'memspeed' in path:
        return 'memspeed'
    elif 'contents' in path:
        return 'contents'
    elif 'gitinfo' in path:
        return 'gitinfo'
    elif 'event' in path:
        return 'debug'
    else:
        return 'other'


def get_ip_geolocation(ip_address):
    """Get geolocation data for an IP address using ip-api.com (free, no key needed)."""
    if not ip_address or ip_address == 'Unknown':
        return {'country': 'Unknown', 'city': 'Unknown', 'lat': 0, 'lon': 0}

    # Handle multiple IPs in X-Forwarded-For (take first one)
    if ',' in ip_address:
        ip_address = ip_address.split(',')[0].strip()

    try:
        import urllib.request
        import json

        url = f'http://ip-api.com/json/{ip_address}?fields=status,country,city,lat,lon'
        with urllib.request.urlopen(url, timeout=2) as response:
            data = json.loads(response.read().decode())

            if data.get('status') == 'success':
                return {
                    'country': data.get('country', 'Unknown'),
                    'city': data.get('city', 'Unknown'),
                    'lat': data.get('lat', 0),
                    'lon': data.get('lon', 0)
                }
    except Exception as e:
        print(f"Error getting geolocation for {ip_address}: {e}")

    return {'country': 'Unknown', 'city': 'Unknown', 'lat': 0, 'lon': 0}


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

    # Handle both REST API and HTTP API (v2) event formats
    if 'rawPath' in event:
        # HTTP API v2 format
        path = event['rawPath']
        stage = event.get('requestContext', {}).get('stage', 'default')
        host = event.get('headers', {}).get('host', '')
    else:
        # REST API format
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
    # Handle different header formats
    headers = event.get('headers', {})
    ip = headers.get('X-Forwarded-For') or headers.get('x-forwarded-for', 'Unknown')
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

    elif path.startswith(f'/{stage}/gardencam/timelapse/schedule') or path.startswith('/gardencam/timelapse/schedule'):
        # Timelapse schedule page
        if not check_basic_auth(event, GARDENCAM_PASSWORD):
            return {
                'statusCode': 401,
                'body': '<html><body><h1>401 Unauthorized</h1><p>Access denied.</p></body></html>',
                'headers': {
                    'Content-Type': 'text/html',
                    'WWW-Authenticate': 'Basic realm="Garden Camera"'
                }
            }

        html += '''
        <meta charset="UTF-8">
        <title>Timelapse Schedule - Garden Camera</title>
        <style>
            body { font-family: Arial, sans-serif; margin: 0; padding: 1rem; background: #1a1a1a; color: #fff; }
            .nav { text-align: center; margin-bottom: 1.5rem; }
            .nav a { color: #4a9eff; text-decoration: none; margin: 0 1rem; padding: 0.5rem 1rem; background: #2a2a2a; border-radius: 6px; display: inline-block; }
            .nav a:hover { background: #3a3a3a; }
            h1 { text-align: center; margin-bottom: 2rem; }
            .container { max-width: 1200px; margin: 0 auto; }
            .schedule-section { background: #2a2a2a; border-radius: 8px; padding: 1.5rem; margin-bottom: 2rem; }
            .schedule-section h2 { margin-top: 0; color: #4a9eff; }
            .schedule-item { background: #1a1a1a; padding: 1rem; margin: 1rem 0; border-radius: 6px; border-left: 4px solid #4a9eff; }
            .schedule-item h3 { margin: 0 0 0.5rem 0; color: #fff; }
            .schedule-item p { margin: 0.25rem 0; color: #aaa; }
            .status { display: inline-block; padding: 0.25rem 0.75rem; border-radius: 4px; font-size: 0.85rem; margin-left: 0.5rem; }
            .status.active { background: #10b981; color: #fff; }
            .status.dryrun { background: #f59e0b; color: #000; }
            table { width: 100%; border-collapse: collapse; margin-top: 1rem; }
            th, td { padding: 0.75rem; text-align: left; border-bottom: 1px solid #3a3a3a; }
            th { color: #4a9eff; background: #1a1a1a; }
        </style>
        <div class="nav">
            <a href="../../contents">Home</a>
            <a href="../gardencam">Latest</a>
            <a href="timelapse">Timelapse Index</a>
            <a href="timelapse/videos">All Videos</a>
        </div>
        <div class="container">
            <h1>Timelapse Automation Schedule</h1>

            <div class="schedule-section">
                <h2>Video Generation</h2>
                <div class="schedule-item">
                    <h3>Weekly Timelapse Creation <span class="status active">ACTIVE</span></h3>
                    <p><strong>Schedule:</strong> Every Sunday at 2:00 AM UTC</p>
                    <p><strong>Duration:</strong> 20 seconds per video (480 frames at 24fps)</p>
                    <p><strong>Output:</strong> videos/timelapse_YYYYMMDD-YYYYMMDD.mp4</p>
                    <p><strong>Lambda:</strong> gardencam-timelapse-generator (3GB memory, 15 min timeout)</p>
                    <p><strong>Next Run:</strong> Next Sunday 02:00 UTC</p>
                </div>
            </div>

            <div class="schedule-section">
                <h2>Image Culling</h2>
                <div class="schedule-item">
                    <h3>Daily Cleanup Check <span class="status dryrun">DRY-RUN</span></h3>
                    <p><strong>Schedule:</strong> Every day at 12:00 PM UTC</p>
                    <p><strong>Mode:</strong> Dry-run (no actual deletion yet)</p>
                    <p><strong>Protection:</strong> ALL night images preserved forever</p>
                    <p><strong>Retention:</strong> Latest 14 days always kept</p>
                    <p><strong>Lambda:</strong> gardencam-image-culling (512MB memory, 5 min timeout)</p>
                </div>

                <h3 style="margin-top: 2rem;">Retention Policy</h3>
                <table>
                    <tr>
                        <th>Age</th>
                        <th>Day Images</th>
                        <th>Night Images</th>
                    </tr>
                    <tr>
                        <td>0-14 days</td>
                        <td>Keep all</td>
                        <td>Keep all</td>
                    </tr>
                    <tr>
                        <td>15-30 days</td>
                        <td>Keep 1 per day</td>
                        <td>Keep all</td>
                    </tr>
                    <tr>
                        <td>31-90 days</td>
                        <td>Keep 1 per week</td>
                        <td>Keep all</td>
                    </tr>
                    <tr>
                        <td>90+ days</td>
                        <td>Keep 1 per month</td>
                        <td>Keep all</td>
                    </tr>
                </table>
            </div>

            <div class="schedule-section">
                <h2>Storage Summary</h2>
                <div class="schedule-item">
                    <h3>Hourly Storage Stats Update <span class="status active">ACTIVE</span></h3>
                    <p><strong>Schedule:</strong> Every hour</p>
                    <p><strong>Function:</strong> Calculate S3 storage statistics</p>
                    <p><strong>Lambda:</strong> gardencam-storage-summary</p>
                </div>
            </div>

            <div class="schedule-section">
                <h2>System Information</h2>
                <p><strong>Region:</strong> eu-west-1 (Ireland)</p>
                <p><strong>S3 Bucket:</strong> gardencam-berrylands-eu-west-1</p>
                <p><strong>Video Format:</strong> MP4 (H.264, 1920x1080, 24fps)</p>
                <p><strong>Frame Selection:</strong> Evenly distributed from all images in date range</p>
            </div>
        </div>
        '''

    elif path.startswith(f'/{stage}/gardencam/timelapse/videos') or path.startswith('/gardencam/timelapse/videos'):
        # Redirect to /gardencam/videos
        return {
            'statusCode': 302,
            'headers': {
                'Location': f'/{stage}/gardencam/videos'
            }
        }

    elif path.startswith(f'/{stage}/gardencam/timelapse') or path.startswith('/gardencam/timelapse'):
        # Timelapse index page
        if not check_basic_auth(event, GARDENCAM_PASSWORD):
            return {
                'statusCode': 401,
                'body': '<html><body><h1>401 Unauthorized</h1><p>Access denied.</p></body></html>',
                'headers': {
                    'Content-Type': 'text/html',
                    'WWW-Authenticate': 'Basic realm="Garden Camera"'
                }
            }

        # Get latest 3 videos
        s3 = boto3.client("s3", region_name=GARDENCAM_REGION)
        videos = []

        try:
            response = s3.list_objects_v2(
                Bucket=GARDENCAM_BUCKET,
                Prefix='videos/timelapse_'
            )

            if 'Contents' in response:
                for obj in response['Contents']:
                    key = obj['Key']
                    if key.endswith('.mp4'):
                        video_id = key.replace('videos/', '').replace('.mp4', '')
                        date_part = video_id.replace('timelapse_', '')
                        try:
                            start_date, end_date = date_part.split('-')
                            start_formatted = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
                            end_formatted = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
                        except:
                            start_formatted = "Unknown"
                            end_formatted = "Unknown"

                        videos.append({
                            'id': video_id,
                            'key': key,
                            'size_mb': obj['Size'] / 1048576,
                            'start_date': start_formatted,
                            'end_date': end_formatted
                        })

            videos.sort(key=lambda v: v['id'], reverse=True)
            latest_videos = videos[:3]
            total_videos = len(videos)

        except Exception as e:
            print(f"Error listing videos: {e}")
            latest_videos = []
            total_videos = 0

        html += f'''
        <title>Timelapse Videos - Garden Camera</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 0; padding: 1rem; background: #1a1a1a; color: #fff; }}
            .nav {{ text-align: center; margin-bottom: 1.5rem; }}
            .nav a {{ color: #4a9eff; text-decoration: none; margin: 0 1rem; padding: 0.5rem 1rem; background: #2a2a2a; border-radius: 6px; display: inline-block; }}
            .nav a:hover {{ background: #3a3a3a; }}
            h1 {{ text-align: center; margin-bottom: 2rem; }}
            .container {{ max-width: 1200px; margin: 0 auto; }}
            .info-section {{ background: #2a2a2a; border-radius: 8px; padding: 1.5rem; margin-bottom: 2rem; }}
            .info-section h2 {{ margin-top: 0; color: #4a9eff; }}
            .stats {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin: 1.5rem 0; }}
            .stat-box {{ background: #1a1a1a; padding: 1.5rem; border-radius: 6px; text-align: center; }}
            .stat-value {{ font-size: 2.5rem; font-weight: bold; color: #4a9eff; }}
            .stat-label {{ color: #888; margin-top: 0.5rem; }}
            .latest-videos {{ margin-top: 2rem; }}
            .video-list {{ list-style: none; padding: 0; }}
            .video-list li {{ background: #1a1a1a; padding: 1rem; margin: 0.5rem 0; border-radius: 6px; display: flex; justify-content: space-between; align-items: center; }}
            .video-list li:hover {{ background: #252525; }}
            .video-title {{ color: #4a9eff; font-weight: bold; }}
            .video-date {{ color: #888; font-size: 0.9rem; }}
            .button {{ display: inline-block; padding: 0.75rem 1.5rem; background: #4a9eff; color: #fff; text-decoration: none; border-radius: 6px; margin: 0.5rem; }}
            .button:hover {{ background: #3a8eef; }}
            .button.secondary {{ background: #2a2a2a; }}
            .button.secondary:hover {{ background: #3a3a3a; }}
        </style>
        <div class="nav">
            <a href="../../contents">Home</a>
            <a href="../gardencam">Latest</a>
            <a href="videos">All Videos</a>
            <a href="timelapse/schedule">Schedule</a>
        </div>
        <div class="container">
            <h1>Garden Timelapse Videos</h1>

            <div class="info-section">
                <h2>Overview</h2>
                <p>Automated weekly timelapse videos created from garden camera images. Each video condenses a week of captures into a smooth 20-second timelapse.</p>

                <div class="stats">
                    <div class="stat-box">
                        <div class="stat-value">{total_videos}</div>
                        <div class="stat-label">Total Videos</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-value">20s</div>
                        <div class="stat-label">Duration Each</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-value">480</div>
                        <div class="stat-label">Frames Each</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-value">24fps</div>
                        <div class="stat-label">Frame Rate</div>
                    </div>
                </div>

                <div style="text-align: center; margin-top: 1.5rem;">
                    <a href="videos" class="button">View All Videos</a>
                    <a href="timelapse/schedule" class="button secondary">View Schedule</a>
                </div>
            </div>

            <div class="info-section latest-videos">
                <h2>Latest Videos</h2>
                <ul class="video-list">
        '''

        for video in latest_videos:
            html += f'''
                <li>
                    <div>
                        <div class="video-title">{video['id'].replace('timelapse_', 'Week of ')}</div>
                        <div class="video-date">{video['start_date']} to {video['end_date']} • {video['size_mb']:.1f} MB</div>
                    </div>
                    <a href="videos" class="button">Watch</a>
                </li>
            '''

        html += '''
                </ul>
            </div>
        </div>
        '''

    elif path.startswith(f'/{stage}/gardencam/video') or path.startswith('/gardencam/video'):
        # Single timelapse video player page
        if not check_basic_auth(event, GARDENCAM_PASSWORD):
            return {
                'statusCode': 401,
                'body': '<html><body><h1>401 Unauthorized</h1><p>Access denied.</p></body></html>',
                'headers': {
                    'Content-Type': 'text/html',
                    'WWW-Authenticate': 'Basic realm="Garden Camera"'
                }
            }

        # Get video ID from query parameters
        query_params = event.get('queryStringParameters') or {}
        video_id = query_params.get('id')

        if not video_id:
            return {
                'statusCode': 400,
                'body': '<html><body><h1>400 Bad Request</h1><p>Missing video ID parameter.</p></body></html>',
                'headers': {'Content-Type': 'text/html'}
            }

        # Get video from S3
        s3 = boto3.client("s3", region_name=GARDENCAM_REGION)
        key = f"videos/{video_id}.mp4"

        try:
            # Check if video exists
            s3.head_object(Bucket=GARDENCAM_BUCKET, Key=key)

            # Parse date range from video ID
            try:
                date_part = video_id.replace('timelapse_', '')
                start_date, end_date = date_part.split('-')
                start_formatted = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
                end_formatted = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
            except:
                start_formatted = "Unknown"
                end_formatted = "Unknown"

            # Get metadata from DynamoDB
            frame_count = 0
            duration = 20
            try:
                dynamodb = boto3.resource('dynamodb', region_name=GARDENCAM_REGION)
                metadata_table = dynamodb.Table('gardencam-video-metadata')
                metadata_response = metadata_table.get_item(Key={'video_id': video_id})

                if 'Item' in metadata_response:
                    item = metadata_response['Item']
                    frame_count = int(float(item.get('frame_count', 0)))
                    duration = int(float(item.get('duration_seconds', 20)))
            except Exception as e:
                print(f"Error getting metadata for {video_id}: {e}")

            # Generate presigned URL
            presigned_url = s3.generate_presigned_url(
                'get_object',
                Params={'Bucket': GARDENCAM_BUCKET, 'Key': key},
                ExpiresIn=3600
            )

            # Get file size
            obj_info = s3.head_object(Bucket=GARDENCAM_BUCKET, Key=key)
            size_mb = obj_info['ContentLength'] / 1048576

            # Render single video page
            html += f'''
            <meta charset="UTF-8">
            <title>Video: {start_formatted} to {end_formatted} - Garden Camera</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 0; padding: 1rem; background: #1a1a1a; color: #fff; }}
                .nav {{ text-align: center; margin-bottom: 1.5rem; }}
                .nav a {{ color: #4a9eff; text-decoration: none; margin: 0 1rem; padding: 0.5rem 1rem; background: #2a2a2a; border-radius: 6px; display: inline-block; }}
                .nav a:hover {{ background: #3a3a3a; }}
                .container {{ max-width: 1400px; margin: 0 auto; }}
                .video-container {{ background: #2a2a2a; border-radius: 8px; padding: 2rem; }}
                .video-container video {{ width: 100%; max-width: 1200px; display: block; margin: 0 auto; border-radius: 6px; background: #000; }}
                .video-info {{ margin-top: 2rem; padding: 1.5rem; background: #1a1a1a; border-radius: 6px; }}
                .video-info h2 {{ margin: 0 0 1rem 0; color: #4a9eff; }}
                .info-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin: 1rem 0; }}
                .info-item {{ padding: 1rem; background: #2a2a2a; border-radius: 6px; }}
                .info-label {{ color: #888; font-size: 0.9rem; margin-bottom: 0.5rem; }}
                .info-value {{ font-size: 1.5rem; font-weight: bold; color: #4a9eff; }}
                .download-btn {{ display: inline-block; margin-top: 1rem; padding: 0.75rem 1.5rem; background: #4a9eff; color: #fff; text-decoration: none; border-radius: 6px; font-size: 1rem; }}
                .download-btn:hover {{ background: #3a8eef; }}
            </style>
            <div class="nav">
                <a href="../../contents">Home</a>
                <a href="../gardencam">Latest</a>
                <a href="timelapse">Timelapse Index</a>
                <a href="videos">All Videos</a>
            </div>
            <div class="container">
                <div class="video-container">
                    <video controls loop autoplay preload="auto">
                        <source src="{presigned_url}" type="video/mp4">
                        Your browser does not support the video tag.
                    </video>
                    <div class="video-info">
                        <h2>Week of {start_formatted} to {end_formatted}</h2>
                        <div class="info-grid">
                            <div class="info-item">
                                <div class="info-label">Frames</div>
                                <div class="info-value">{frame_count}</div>
                            </div>
                            <div class="info-item">
                                <div class="info-label">Duration</div>
                                <div class="info-value">{duration}s</div>
                            </div>
                            <div class="info-item">
                                <div class="info-label">File Size</div>
                                <div class="info-value">{size_mb:.1f} MB</div>
                            </div>
                        </div>
                        <a href="{presigned_url}" download class="download-btn">Download Video (MP4)</a>
                    </div>
                </div>
            </div>
            '''

        except s3.exceptions.NoSuchKey:
            html += f'''
            <title>Video Not Found - Garden Camera</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 0; padding: 1rem; background: #1a1a1a; color: #fff; }}
                .error {{ max-width: 800px; margin: 2rem auto; padding: 2rem; background: #2a2a2a; border-radius: 8px; text-align: center; }}
            </style>
            <div class="error">
                <h1>Video Not Found</h1>
                <p>The requested video "{video_id}" does not exist.</p>
                <p><a href="videos" style="color: #4a9eff;">View all videos</a></p>
            </div>
            '''
        except Exception as e:
            print(f"Error loading video {video_id}: {e}")
            html += f'''
            <title>Error - Garden Camera</title>
            <style>
                body {{ font-family: Arial, sans-serif; margin: 0; padding: 1rem; background: #1a1a1a; color: #fff; }}
                .error {{ max-width: 800px; margin: 2rem auto; padding: 2rem; background: #2a2a2a; border-radius: 8px; text-align: center; }}
            </style>
            <div class="error">
                <h1>Error Loading Video</h1>
                <p>An error occurred while loading the video.</p>
                <p><a href="videos" style="color: #4a9eff;">View all videos</a></p>
            </div>
            '''

    elif path.startswith(f'/{stage}/gardencam/videos') or path.startswith('/gardencam/videos'):
        # Timelapse video gallery page with thumbnails
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
                                # Convert Decimal to int
                                frame_count = int(float(item.get('frame_count', 0)))
                                duration = int(float(item.get('duration_seconds', 5)))
                                print(f"Video {video_id}: {frame_count} frames, {duration}s")
                            else:
                                print(f"No metadata found for {video_id}")
                                frame_count = 0
                                duration = 5
                        except Exception as e:
                            print(f"Error getting metadata for {video_id}: {e}")
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

        # Render HTML with thumbnails
        html += f'''
        <meta charset="UTF-8">
        <title>Timelapse Videos - Garden Camera</title>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 0; padding: 1rem; background: #1a1a1a; color: #fff; }}
            .nav {{ text-align: center; margin-bottom: 1.5rem; }}
            .nav a {{ color: #4a9eff; text-decoration: none; margin: 0 1rem; padding: 0.5rem 1rem; background: #2a2a2a; border-radius: 6px; display: inline-block; }}
            .nav a:hover {{ background: #3a3a3a; }}
            h1 {{ text-align: center; margin-bottom: 2rem; }}
            .video-grid {{ max-width: 1400px; margin: 0 auto; display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 1.5rem; }}
            .video-card {{ background: #2a2a2a; border-radius: 8px; overflow: hidden; transition: transform 0.2s, box-shadow 0.2s; text-decoration: none; display: block; color: inherit; }}
            .video-card:hover {{ transform: translateY(-4px); box-shadow: 0 8px 16px rgba(0,0,0,0.3); }}
            .video-thumbnail {{ width: 100%; height: 180px; background: linear-gradient(135deg, #2a2a2a 0%, #1a1a1a 100%); display: flex; align-items: center; justify-content: center; position: relative; }}
            .play-icon {{ width: 60px; height: 60px; background: rgba(74, 158, 255, 0.9); border-radius: 50%; display: flex; align-items: center; justify-content: center; }}
            .play-icon::after {{ content: ''; width: 0; height: 0; border-style: solid; border-width: 12px 0 12px 20px; border-color: transparent transparent transparent #fff; margin-left: 4px; }}
            .video-metadata {{ padding: 1rem; }}
            .video-metadata h3 {{ margin: 0 0 0.75rem 0; color: #4a9eff; font-size: 1rem; }}
            .video-metadata p {{ margin: 0.25rem 0; color: #aaa; font-size: 0.85rem; }}
            .video-stats {{ display: flex; justify-content: space-between; margin-top: 0.75rem; padding-top: 0.75rem; border-top: 1px solid #3a3a3a; }}
            .stat {{ text-align: center; flex: 1; }}
            .stat-value {{ font-weight: bold; color: #4a9eff; display: block; }}
            .stat-label {{ font-size: 0.75rem; color: #666; }}
            .no-videos {{ max-width: 800px; margin: 2rem auto; padding: 2rem; background: #2a2a2a; border-radius: 8px; text-align: center; }}
        </style>
        <div class="nav">
            <a href="../../contents">Home</a>
            <a href="../gardencam">Latest</a>
            <a href="timelapse">Timelapse Index</a>
            <a href="timelapse/schedule">Schedule</a>
            <a href="gallery">Gallery</a>
            <a href="stats">Capture Stats</a>
        </div>
        <h1>Timelapse Videos</h1>
        '''

        if videos:
            html += '<div class="video-grid">'
            for video in videos:
                html += f'''
                <a href="video?id={video['id']}" class="video-card">
                    <div class="video-thumbnail">
                        <div class="play-icon"></div>
                    </div>
                    <div class="video-metadata">
                        <h3>Week of {video['start_date']} to {video['end_date']}</h3>
                        <div class="video-stats">
                            <div class="stat">
                                <span class="stat-value">{video['frame_count']}</span>
                                <span class="stat-label">frames</span>
                            </div>
                            <div class="stat">
                                <span class="stat-value">{video['duration']}s</span>
                                <span class="stat-label">duration</span>
                            </div>
                            <div class="stat">
                                <span class="stat-value">{video['size_mb']:.1f} MB</span>
                                <span class="stat-label">size</span>
                            </div>
                        </div>
                    </div>
                </a>
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

        # Prepare chart data - last 10 CALENDAR days from today
        chart_dates = []
        chart_counts = []
        chart_durations = []
        chart_costs = []
        chart_path_data = defaultdict(list)

        # Get last 10 calendar days
        today = datetime.utcnow().date()
        last_10_days = [(today - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(9, -1, -1)]

        for date in last_10_days:
            chart_dates.append(date)
            if date in daily_stats:
                chart_counts.append(daily_stats[date]['count'])
                chart_durations.append(float(daily_stats[date]['total_duration_ms']))
                chart_costs.append(float(daily_stats[date]['total_cost_usd']) * 1_000_000)

                # Collect path counts for stacked chart
                for path_name in all_paths:
                    chart_path_data[path_name].append(daily_stats[date]['paths'].get(path_name, 0))
            else:
                # No data for this day - fill with zeros
                chart_counts.append(0)
                chart_durations.append(0)
                chart_costs.append(0)
                for path_name in all_paths:
                    chart_path_data[path_name].append(0)

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

        # Get CloudWatch metrics for all Lambda functions
        all_lambda_metrics = get_all_lambda_metrics(days=30)

        # Calculate aggregated CloudWatch stats across all functions
        total_cw_invocations = sum(m['invocations'] for m in all_lambda_metrics.values())
        total_cw_errors = sum(m['errors'] for m in all_lambda_metrics.values())
        total_cw_throttles = sum(m['throttles'] for m in all_lambda_metrics.values())

        # Calculate weighted average duration
        total_duration_weighted = sum(m['avg_duration'] * m['invocations'] for m in all_lambda_metrics.values())
        avg_cw_duration = total_duration_weighted / total_cw_invocations if total_cw_invocations > 0 else 0
        max_cw_duration = max((m['max_duration'] for m in all_lambda_metrics.values()), default=0)

        error_rate = (total_cw_errors / total_cw_invocations * 100) if total_cw_invocations > 0 else 0

        # Sort functions by invocation count
        sorted_functions = sorted(all_lambda_metrics.items(), key=lambda x: x[1]['invocations'], reverse=True)

        # Calculate stats by application
        app_stats = defaultdict(lambda: {
            'count': 0,
            'total_duration_ms': Decimal('0'),
            'total_cost_usd': Decimal('0')
        })

        for item in stats:
            path = item.get('path', '')
            app = categorize_path(path)
            app_stats[app]['count'] += 1
            app_stats[app]['total_duration_ms'] += Decimal(str(item.get('duration_ms', 0)))
            app_stats[app]['total_cost_usd'] += Decimal(str(item.get('estimated_cost_usd', 0)))

        # Sort apps by count
        sorted_apps = sorted(app_stats.items(), key=lambda x: x[1]['count'], reverse=True)

        # Determine free tier status color
        free_tier_status = 'good' if request_usage_pct < 50 else ('warning' if request_usage_pct < 80 else 'danger')
        status_colors = {'good': '#32cd32', 'warning': '#ffa500', 'danger': '#ff4444'}

        html += f'''
        <!DOCTYPE html>
        <html lang="en">
        <head>
        <meta charset="UTF-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
        <title>Lambda Execution Statistics</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
        <script src="https://cdn.plot.ly/plotly-2.27.0.min.js"></script>
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
        </head>
        <body>
        <div class="nav">
            <a href="../contents">Home</a>
        </div>
        <h1>Lambda Execution Statistics</h1>

        <div class="stats-summary">
            <h2>AWS Free Tier Usage (Current Month - DynamoDB Logs Only)</h2>
            <div class="free-tier-box">
                <div class="metric-row">
                    <span class="metric-name">Requests Logged</span>
                    <span class="metric-value">{current_month_stats['requests']:,} / {FREE_TIER_REQUESTS:,} ({request_usage_pct:.2f}%)</span>
                </div>
                <div class="progress-bar">
                    <div class="progress-fill" style="width: {min(request_usage_pct, 100):.1f}%; background: {status_colors[free_tier_status]};"></div>
                </div>

                <div class="metric-row" style="margin-top: 1.5rem;">
                    <span class="metric-name">Compute (GB-seconds)</span>
                    <span class="metric-value">{float(current_month_stats['gb_seconds']):,.1f} / {FREE_TIER_GB_SECONDS:,} ({gb_seconds_usage_pct:.2f}%)</span>
                </div>
                <div class="progress-bar">
                    <div class="progress-fill" style="width: {min(gb_seconds_usage_pct, 100):.1f}%; background: {status_colors[free_tier_status]};"></div>
                </div>

                <div style="margin-top: 1rem; padding: 0.75rem; background: #1a3a1a; border-radius: 6px; border-left: 3px solid #32cd32;">
                    <div style="font-size: 0.9rem; color: #aaa;">
                        <strong style="color: #32cd32;">✓ Free tier will not be exceeded</strong><br>
                        All costs shown are theoretical - actual cost this month: <strong>$0.00</strong>
                    </div>
                </div>
            </div>
        </div>

        <div class="stats-summary">
            <h2>🔍 Data Availability Check</h2>
            <div style="font-size: 0.9rem; color: #888; margin-bottom: 1rem;">
                Showing which dates have execution data in DynamoDB
            </div>
            <table class="daily-table">
                <thead>
                    <tr>
                        <th>Date</th>
                        <th>Executions</th>
                        <th>In Last 10 Days?</th>
                    </tr>
                </thead>
                <tbody>
        '''

        # Show last 30 dates to help debug
        today = datetime.utcnow().date()
        last_30_days = [(today - timedelta(days=i)).strftime('%Y-%m-%d') for i in range(29, -1, -1)]

        for date in last_30_days:
            if date in daily_stats:
                count = daily_stats[date]['count']
                in_range = "✓ Yes" if date in chart_dates else "No"
                html += f'''
                    <tr style="{'background: #1a3a1a;' if date in chart_dates else ''}">
                        <td><strong>{date}</strong></td>
                        <td>{count:,}</td>
                        <td>{in_range}</td>
                    </tr>
                '''

        html += '''
                </tbody>
            </table>
        </div>

        <div class="stats-summary">
            <h2>📊 Historical CloudWatch Metrics (All Functions - Last 30 Days)</h2>
            <div style="font-size: 0.9rem; color: #888; margin-bottom: 1rem; text-align: center;">
                Includes all Lambda invocations from CloudWatch monitoring
            </div>
            <div class="stats-grid">
                <div class="stat-box">
                    <div class="stat-value">{int(total_cw_invocations):,}</div>
                    <div class="stat-label">Total Invocations</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value" style="color: {'#ff4444' if total_cw_errors > 0 else '#32cd32'};">{int(total_cw_errors):,}</div>
                    <div class="stat-label">Errors</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value" style="color: {'#ff4444' if total_cw_throttles > 0 else '#32cd32'};">{int(total_cw_throttles):,}</div>
                    <div class="stat-label">Throttles</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value" style="color: {'#ff4444' if error_rate > 5 else ('#ffa500' if error_rate > 1 else '#32cd32')};">{error_rate:.2f}%</div>
                    <div class="stat-label">Error Rate</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">{avg_cw_duration:.0f}ms</div>
                    <div class="stat-label">Avg Duration (CW)</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">{max_cw_duration:.0f}ms</div>
                    <div class="stat-label">Max Duration (CW)</div>
                </div>
            </div>
        </div>

        <div class="stats-summary">
            <h2>Statistics by Lambda Function (Last 30 Days)</h2>
            <table class="daily-table">
                <thead>
                    <tr>
                        <th>Function Name</th>
                        <th>Invocations</th>
                        <th>Errors</th>
                        <th>Error Rate</th>
                        <th>Avg Duration</th>
                        <th>Max Duration</th>
                    </tr>
                </thead>
                <tbody>
        '''

        for func_name, func_metrics in sorted_functions:
            func_invocations = int(func_metrics['invocations'])
            func_errors = int(func_metrics['errors'])
            func_error_rate = func_metrics['error_rate']
            func_avg_duration = func_metrics['avg_duration']
            func_max_duration = func_metrics['max_duration']

            error_color = '#ff4444' if func_error_rate > 5 else ('#ffa500' if func_error_rate > 1 else '#32cd32')

            html += f'''
                    <tr>
                        <td><strong>{func_name}</strong></td>
                        <td>{func_invocations:,}</td>
                        <td style="color: {error_color};">{func_errors:,}</td>
                        <td style="color: {error_color};">{func_error_rate:.2f}%</td>
                        <td>{func_avg_duration:.0f}ms</td>
                        <td>{func_max_duration:.0f}ms</td>
                    </tr>
            '''

        html += '''
                </tbody>
            </table>
        </div>

        <div class="stats-summary">
            <h2>Statistics by Application (Recent DynamoDB Logs)</h2>
            <table class="daily-table">
                <thead>
                    <tr>
                        <th>Application</th>
                        <th>Requests</th>
                        <th>Avg Duration</th>
                        <th>Cost (µ$)</th>
                        <th>Percentage</th>
                    </tr>
                </thead>
                <tbody>
        '''

        for app_name, app_data in sorted_apps:
            app_count = app_data['count']
            app_avg_duration = float(app_data['total_duration_ms']) / app_count if app_count > 0 else 0
            app_cost_microdollars = float(app_data['total_cost_usd']) * 1_000_000
            app_percentage = (app_count / total_executions * 100) if total_executions > 0 else 0

            html += f'''
                    <tr>
                        <td><strong>{app_name}</strong></td>
                        <td>{app_count:,}</td>
                        <td>{app_avg_duration:.0f}ms</td>
                        <td>{app_cost_microdollars:.2f}</td>
                        <td>{app_percentage:.1f}%</td>
                    </tr>
            '''

        html += f'''
                </tbody>
            </table>
        </div>

        <div class="stats-summary">
            <h2>📝 Recent Execution Logs from DynamoDB ({len(stats)} executions)</h2>
            <div style="font-size: 0.9rem; color: #888; margin-bottom: 1rem; text-align: center;">
                Detailed logs since execution logging was enabled (~1 hour ago)
            </div>
            <div class="stats-grid">
                <div class="stat-box">
                    <div class="stat-value">{total_executions:,}</div>
                    <div class="stat-label">Total Executions Logged</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">{float(total_cost)*1_000_000:.1f} µ$</div>
                    <div class="stat-label">Total Cost (microdollars)<br><span style="font-size: 0.8rem; color: #666;">After free tier</span></div>
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
        '''

        # Prepare data for duration analysis (matching Jupyter notebook Section 6)
        durations = [float(item.get('duration_ms', 0)) for item in stats if item.get('duration_ms', 0) > 0]

        # Calculate log-spaced bins for histogram (matching notebook exactly)
        import math
        histogram_data = {'bin_edges': [], 'counts': []}
        if durations:
            min_duration = min(durations)
            max_duration = max(durations)

            # Create 50 log-spaced bins using logspace formula: 10^(log10(min) + i*(log10(max)-log10(min))/n)
            num_bins = 50
            log_min = math.log10(min_duration)
            log_max = math.log10(max_duration)
            log_bins = [10**(log_min + i * (log_max - log_min) / num_bins) for i in range(num_bins + 1)]

            # Calculate histogram
            bin_counts = [0] * num_bins
            for duration in durations:
                # Find which bin this duration belongs to
                for i in range(num_bins):
                    if log_bins[i] <= duration < log_bins[i+1]:
                        bin_counts[i] += 1
                        break
                else:
                    # Handle edge case: duration == max_duration
                    if duration == max_duration:
                        bin_counts[-1] += 1

            # Prepare data for Plotly (use bin centers)
            histogram_data['bin_edges'] = [(log_bins[i] + log_bins[i+1]) / 2 for i in range(num_bins)]
            histogram_data['counts'] = bin_counts

        # Prepare boxplot data by path (top 5 paths)
        path_durations = defaultdict(list)
        for item in stats:
            path = item.get('path', '')
            if path:  # Only non-empty paths
                duration = float(item.get('duration_ms', 0))
                if duration > 0:
                    path_durations[path].append(duration)

        # Get top 5 paths by count
        top_5_paths = sorted(path_totals.items(), key=lambda x: x[1], reverse=True)[:5]
        top_5_path_names = [p[0] for p in top_5_paths if p[0]]  # Exclude empty paths

        # Prepare boxplot data
        boxplot_data = []
        colors = ['#4a9eff', '#32cd32', '#ffa500', '#ff69b4', '#9370db']
        for idx, path_name in enumerate(top_5_path_names):
            if path_name in path_durations:
                boxplot_data.append({
                    'y': path_durations[path_name],
                    'name': path_name,
                    'type': 'box',
                    'marker': {'color': colors[idx % len(colors)]}
                })

        html += f'''
        <div class="chart-container">
            <div class="chart-title">📊 Duration Distribution (Log Scale)</div>
            <div style="font-size: 0.9rem; color: #888; text-align: center; margin-bottom: 1rem;">
                Similar to Jupyter notebook analysis - log-scaled bins for better visualization
            </div>
            <div id="durationHistogram" style="width: 100%; height: 400px;"></div>
        </div>

        <div class="chart-container">
            <div class="chart-title">📦 Duration by Path (Top 5)</div>
            <div style="font-size: 0.9rem; color: #888; text-align: center; margin-bottom: 1rem;">
                Boxplots showing duration distribution for each endpoint
            </div>
            <div id="durationBoxplot" style="width: 100%; height: 400px;"></div>
        </div>

        <div class="chart-container">
            <div class="chart-title">Executions by Endpoint (Last 10 Days)</div>
            <canvas id="pathChart"></canvas>
        </div>

        <div class="chart-container">
            <div class="chart-title">Daily Execution Count (Last 10 Days)</div>
            <canvas id="countChart"></canvas>
        </div>

        <div class="chart-container">
            <div class="chart-title">Daily Total Duration (Last 10 Days)</div>
            <canvas id="durationChart"></canvas>
        </div>

        <div class="chart-container">
            <div class="chart-title">Daily Cost in Microdollars (µ$) - Last 10 Days</div>
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
            <h2>Daily Breakdown (Recent DynamoDB Logs)</h2>
            <table class="daily-table">
                <thead>
                    <tr>
                        <th>Date</th>
                        <th>Executions</th>
                        <th>Total Duration</th>
                        <th>Avg Duration</th>
                        <th>Cost (µ$)</th>
                    </tr>
                </thead>
                <tbody>
        '''

        for date in sorted_dates[:30]:  # Show last 30 days
            day_data = daily_stats[date]
            avg_duration = float(day_data['total_duration_ms']) / day_data['count'] if day_data['count'] > 0 else 0
            cost_microdollars = float(day_data['total_cost_usd']) * 1_000_000
            html += f'''
                    <tr>
                        <td>{date}</td>
                        <td>{day_data['count']:,}</td>
                        <td>{float(day_data['total_duration_ms'])/1000:.1f}s</td>
                        <td>{avg_duration:.0f}ms</td>
                        <td>{cost_microdollars:.2f}</td>
                    </tr>
            '''

        html += '''
                </tbody>
            </table>
        </div>
        '''

        # IP Address and User-Agent Analysis
        from collections import Counter

        ip_data = defaultdict(lambda: {'count': 0, 'paths': Counter(), 'timestamps': [], 'user_agents': Counter()})
        ua_data = Counter()

        for item in stats:
            ip = item.get('ip_address', 'Unknown')
            ua = item.get('user_agent', 'Unknown')
            path = item.get('path', 'unknown')
            timestamp = item.get('timestamp', '')

            if ip and ip != 'Unknown':
                ip_data[ip]['count'] += 1
                ip_data[ip]['paths'][path] += 1
                ip_data[ip]['user_agents'][ua] += 1
                if timestamp:
                    try:
                        ts = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                        ip_data[ip]['timestamps'].append(ts)
                    except:
                        pass

            if ua and ua != 'Unknown':
                ua_data[ua] += 1

        # Detect automated patterns
        ip_patterns = []
        for ip, data in ip_data.items():
            if len(data['timestamps']) > 1:
                timestamps = sorted(data['timestamps'])
                intervals = [(timestamps[i+1] - timestamps[i]).total_seconds() / 60 for i in range(len(timestamps)-1)]
                if intervals:
                    avg_interval = sum(intervals) / len(intervals)
                    if avg_interval < 30:  # Less than 30 minutes average
                        top_path = data['paths'].most_common(1)[0] if data['paths'] else ('unknown', 0)
                        top_ua = data['user_agents'].most_common(1)[0] if data['user_agents'] else ('Unknown', 0)
                        ip_patterns.append({
                            'ip': ip,
                            'count': data['count'],
                            'avg_interval': avg_interval,
                            'top_path': top_path[0],
                            'top_ua': top_ua[0]
                        })

        # Sort by count
        top_ips = sorted(ip_data.items(), key=lambda x: x[1]['count'], reverse=True)[:20]
        top_uas = ua_data.most_common(20)
        sorted_patterns = sorted(ip_patterns, key=lambda x: x['avg_interval'])

        # Get geolocation for top IPs
        import time as time_module
        ip_geo_data = []
        country_counts = Counter()

        for ip, data in top_ips[:15]:  # Limit to 15 to avoid rate limiting
            time_module.sleep(0.15)  # Rate limit: ~6 requests/second
            geo = get_ip_geolocation(ip)
            ip_geo_data.append({
                'ip': ip,
                'count': data['count'],
                'country': geo['country'],
                'city': geo['city'],
                'paths': data['paths'],
                'user_agents': data['user_agents']
            })
            country_counts[geo['country']] += data['count']

        html += f'''
        <div class="stats-summary">
            <h2>🌍 IP Address Analysis with Geolocation ({len(ip_data)} unique IPs)</h2>
            <div style="font-size: 0.9rem; color: #888; margin-bottom: 1rem;">
                Geographic distribution and top IP addresses
            </div>

            <div style="display: grid; grid-template-columns: 1fr 1fr; gap: 2rem; margin-bottom: 2rem;">
                <div>
                    <div class="chart-title">Top 10 IP Addresses</div>
                    <div id="ipPieChart" style="height: 400px;"></div>
                </div>
                <div>
                    <div class="chart-title">Requests by Country</div>
                    <div id="countryPieChart" style="height: 400px;"></div>
                </div>
            </div>

            <table class="daily-table">
                <thead>
                    <tr>
                        <th>IP Address</th>
                        <th>Country</th>
                        <th>City</th>
                        <th>Requests</th>
                        <th>Top Path</th>
                    </tr>
                </thead>
                <tbody>
        '''

        for ip_info in ip_geo_data:
            top_path = ip_info['paths'].most_common(1)[0] if ip_info['paths'] else ('unknown', 0)
            display_path = top_path[0] if top_path[0] else '(root)'

            html += f'''
                    <tr>
                        <td><code>{ip_info['ip']}</code></td>
                        <td>{ip_info['country']}</td>
                        <td>{ip_info['city']}</td>
                        <td>{ip_info['count']:,}</td>
                        <td><code>{display_path}</code></td>
                    </tr>
            '''

        html += '''
                </tbody>
            </table>
        </div>

        <div class="stats-summary">
            <h2>📱 User-Agent Analysis</h2>
            <div style="font-size: 0.9rem; color: #888; margin-bottom: 1rem;">
                Showing top 20 browsers/devices by request count
            </div>
            <table class="daily-table">
                <thead>
                    <tr>
                        <th>User-Agent</th>
                        <th>Requests</th>
                        <th>Percentage</th>
                    </tr>
                </thead>
                <tbody>
        '''

        total_ua_requests = sum(ua_data.values())
        for ua, count in top_uas:
            percentage = (count / total_ua_requests * 100) if total_ua_requests > 0 else 0
            ua_short = (ua[:100] + '...') if len(ua) > 100 else ua

            html += f'''
                    <tr>
                        <td style="font-size: 0.85rem; word-break: break-all;">{ua_short}</td>
                        <td>{count:,}</td>
                        <td>{percentage:.1f}%</td>
                    </tr>
            '''

        html += '''
                </tbody>
            </table>
        </div>
        '''

        if sorted_patterns:
            html += f'''
        <div class="stats-summary">
            <h2>🤖 Automated Request Detection</h2>
            <div style="font-size: 0.9rem; color: #888; margin-bottom: 1rem;">
                IPs making requests at regular intervals (< 30 min average)
            </div>
            <table class="daily-table">
                <thead>
                    <tr>
                        <th>IP Address</th>
                        <th>Requests</th>
                        <th>Avg Interval</th>
                        <th>Top Path</th>
                        <th>User-Agent</th>
                    </tr>
                </thead>
                <tbody>
            '''

            for pattern in sorted_patterns:
                interval_str = f"{pattern['avg_interval']:.1f} min"
                ua_short = (pattern['top_ua'][:60] + '...') if len(pattern['top_ua']) > 60 else pattern['top_ua']
                warning_style = 'background: #3a1a1a; color: #ffa500;' if pattern['avg_interval'] < 5 else ''

                html += f'''
                    <tr style="{warning_style}">
                        <td><code>{pattern['ip']}</code></td>
                        <td>{pattern['count']:,}</td>
                        <td><strong>{interval_str}</strong></td>
                        <td>{pattern['top_path'] if pattern['top_path'] else '(root)'}</td>
                        <td style="font-size: 0.85rem; color: #aaa;">{ua_short}</td>
                    </tr>
                '''

            html += '''
                </tbody>
            </table>
            <div style="margin-top: 1rem; padding: 1rem; background: #2a2a1a; border-left: 4px solid #ffa500; border-radius: 4px;">
                <strong style="color: #ffa500;">⚠️ Note:</strong> Rows highlighted in orange indicate very frequent automated requests (< 5 min intervals).
                These may be widgets, auto-refresh browsers, or cron jobs.
            </div>
        </div>
            '''

        html += '''
        <script>
        // Duration histogram data with log-spaced bins (matching Jupyter notebook EXACTLY)
        const histogramBinEdges = ''' + str(histogram_data['bin_edges']) + ''';
        const histogramCounts = ''' + str(histogram_data['counts']) + ''';

        // Duration boxplot data
        const boxplotData = ''' + str(boxplot_data) + ''';

        // Render duration histogram with Plotly (using pre-calculated log bins)
        if (histogramBinEdges.length > 0) {
            const trace = {
                x: histogramBinEdges,
                y: histogramCounts,
                type: 'bar',
                marker: {
                    color: '#4a9eff',
                    line: { color: '#1a1a1a', width: 1 }
                },
                name: 'Duration (ms)'
            };

            const layout = {
                paper_bgcolor: '#2a2a2a',
                plot_bgcolor: '#1a1a1a',
                font: { color: '#fff' },
                xaxis: {
                    title: 'Duration (ms)',
                    type: 'log',
                    gridcolor: '#3a3a3a',
                    dtick: Math.log10(10)  // Major ticks at powers of 10
                },
                yaxis: {
                    title: 'Count',
                    gridcolor: '#3a3a3a'
                },
                margin: { l: 60, r: 30, t: 30, b: 60 },
                bargap: 0.05
            };

            Plotly.newPlot('durationHistogram', [trace], layout, {responsive: true});
        }

        // Render boxplots with Plotly
        if (boxplotData.length > 0) {
            const layout = {
                paper_bgcolor: '#2a2a2a',
                plot_bgcolor: '#1a1a1a',
                font: { color: '#fff' },
                yaxis: {
                    title: 'Duration (ms)',
                    type: 'log',
                    gridcolor: '#3a3a3a'
                },
                xaxis: {
                    gridcolor: '#3a3a3a'
                },
                margin: { l: 60, r: 30, t: 30, b: 100 },
                showlegend: false
            };

            Plotly.newPlot('durationBoxplot', boxplotData, layout, {responsive: true});
        }

        // IP Address Pie Chart
        const ipLabels = ''' + str([ip_info['ip'] for ip_info in ip_geo_data[:10]]) + ''';
        const ipValues = ''' + str([ip_info['count'] for ip_info in ip_geo_data[:10]]) + ''';

        if (ipLabels.length > 0) {
            Plotly.newPlot('ipPieChart', [{
                labels: ipLabels,
                values: ipValues,
                type: 'pie',
                textinfo: 'label+percent',
                marker: {
                    colors: ['#e74c3c', '#3498db', '#2ecc71', '#f39c12', '#9b59b6',
                             '#1abc9c', '#34495e', '#16a085', '#27ae60', '#2980b9']
                }
            }], {
                paper_bgcolor: '#2a2a2a',
                plot_bgcolor: '#1a1a1a',
                font: { color: '#fff', size: 11 },
                margin: { l: 10, r: 10, t: 10, b: 10 },
                showlegend: true,
                legend: { font: { size: 10 } }
            }, {responsive: true});
        }

        // Country Pie Chart
        const countryLabels = ''' + str(list(country_counts.keys())) + ''';
        const countryValues = ''' + str(list(country_counts.values())) + ''';

        if (countryLabels.length > 0) {
            Plotly.newPlot('countryPieChart', [{
                labels: countryLabels,
                values: countryValues,
                type: 'pie',
                textinfo: 'label+percent',
                marker: {
                    colors: ['#2ecc71', '#3498db', '#f39c12', '#e74c3c', '#9b59b6',
                             '#1abc9c', '#34495e', '#16a085', '#27ae60', '#2980b9']
                }
            }], {
                paper_bgcolor: '#2a2a2a',
                plot_bgcolor: '#1a1a1a',
                font: { color: '#fff', size: 12 },
                margin: { l: 10, r: 10, t: 10, b: 10 },
                showlegend: true
            }, {responsive: true});
        }

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
                    label: 'Cost (µ$ - microdollars)',
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
        </body>
        </html>
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
            ip = headers.get('X-Forwarded-For', headers.get('x-forwarded-for', 'Unknown'))
            user_agent = headers.get('User-Agent', headers.get('user-agent', 'Unknown'))
            log_execution_metrics(context, duration_ms, path, ip, user_agent)

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

    # If html already has complete structure (DOCTYPE), don't wrap it
    if html.strip().startswith('<!DOCTYPE') or html.strip().startswith('<html'):
        content = html
    else:
        content = f'<html><head>{fav}{html}</body></html>'

    # Log execution metrics
    duration_ms = (time.time() - start_time) * 1000
    headers = event.get('headers', {}) or {}
    ip = headers.get('X-Forwarded-For', headers.get('x-forwarded-for', 'Unknown'))
    user_agent = headers.get('User-Agent', headers.get('user-agent', 'Unknown'))
    log_execution_metrics(context, duration_ms, path, ip, user_agent)

    return {
        'statusCode': 200,
        'body': content,
        'headers': {
            'Content-Type': 'text/html; charset=utf-8',
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

