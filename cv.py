from pprint import pformat
import os
import base64
from io import BytesIO
from datetime import datetime

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
DYNAMODB_TABLE = "cv-access-logs"


def get_secret(secret_name):
    """Retrieve a secret from AWS Secrets Manager."""
    if not BOTO3_AVAILABLE:
        print(f"WARNING: boto3 not available. Cannot retrieve secret: {secret_name}")
        return None

    try:
        client = boto3.client('secretsmanager', region_name=GARDENCAM_REGION)
        response = client.get_secret_value(SecretId=secret_name)

        # Parse the secret (assuming it's stored as JSON with a 'password' key)
        if 'SecretString' in response:
            secret = json.loads(response['SecretString'])
            return secret.get('password')

        return None
    except Exception as e:
        print(f"ERROR: Failed to retrieve secret {secret_name}: {str(e)}")
        return None


# Initialize password from Secrets Manager on cold start
GARDENCAM_PASSWORD = get_secret(GARDENCAM_SECRET_NAME)
if not GARDENCAM_PASSWORD:
    print(f"WARNING: Could not retrieve password from Secrets Manager ({GARDENCAM_SECRET_NAME}). Gardencam will be inaccessible.")


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

    # List all objects
    response = s3.list_objects_v2(Bucket=GARDENCAM_BUCKET)
    if "Contents" not in response:
        return []

    # Sort by Key (filename contains timestamp), newest first
    objects = sorted(response["Contents"], key=lambda x: x["Key"], reverse=True)
    images = []

    for obj in objects:
        key = obj["Key"]
        # Only include main garden images (not thumbnails or averaged images)
        if not key.startswith('garden_') or not key.endswith('.jpg'):
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


def lambda_handler(event, context):
    # Log connection details to DynamoDB
    log_connection(event, context)

    html = ""
    favicon=open("favicon.png64", "r").read()
    fav = f'<link rel="icon" type="image/png" href="data:image/png;base64,{favicon}">'
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
        timestamps = []
        avg_brightness_data = []
        peak_brightness_data = []
        noise_floor_data = []
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
                noise_floor_data.append(float(item.get('noise_floor', 0)))
                modes.append(item.get('mode', 'unknown'))

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
            <div class="chart-title">Noise Floor Over Time</div>
            <canvas id="noiseChart"></canvas>
        </div>

        <script>
        const timestamps = {json.dumps(timestamps)};
        const avgBrightness = {json.dumps(avg_brightness_data)};
        const peakBrightness = {json.dumps(peak_brightness_data)};
        const noiseFloor = {json.dumps(noise_floor_data)};

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

        new Chart(document.getElementById('noiseChart'), {{
            type: 'line',
            data: {{
                labels: timestamps,
                datasets: [{{
                    label: 'Noise Floor (Std Dev)',
                    data: noiseFloor,
                    borderColor: '#10b981',
                    backgroundColor: 'rgba(16, 185, 129, 0.1)',
                    tension: 0.3
                }}]
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
            <a href="gardencam/stats" class="gallery-link" style="margin-left: 0.5rem;">View Statistics</a>
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
    else:
        html += open('cv.html', 'r').read()
    content = f'<html><head>{fav}{html}</body></html>'

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

