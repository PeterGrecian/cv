from pprint import pformat
import os
import base64
import urllib.request
from io import BytesIO
from datetime import datetime, timezone, timedelta
import json

try:
    import boto3
    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False

GARDENCAM_BUCKET = "gardencam-berrylands-eu-west-1"
GARDENCAM_REGION = "eu-west-1"
GARDENCAM_PARAMETER_NAME = "/berrylands/gardencam/password"
GARDENCAM_PASSWORD = None
GARDENCAM_EARLIEST_IMAGE = "2026-01-19"  # First image: garden_20260119_185439.jpg
TFL_PARAMETER_NAME = "/berrylands/tfl/api-key"
TFL_API_KEY = None
DYNAMODB_TABLE = "cv-access-logs"

# Memspeed configuration - uses same bucket with memspeed/ prefix
MEMSPEED_PREFIX = "memspeed/"
MEMSPEED_RESULTS_PREFIX = "memspeed/results/"
MEMSPEED_DOWNLOADS_PREFIX = "memspeed/downloads/"


def get_parameter(parameter_name):
    """Retrieve a parameter from AWS Systems Manager Parameter Store (FREE!)."""
    if not BOTO3_AVAILABLE:
        print(f"WARNING: boto3 not available. Cannot retrieve parameter: {parameter_name}")
        return None

    try:
        client = boto3.client('ssm', region_name=GARDENCAM_REGION)
        response = client.get_parameter(
            Name=parameter_name,
            WithDecryption=True  # Decrypt SecureString parameters
        )

        value = response['Parameter']['Value']

        # Try to parse as JSON (for structured secrets)
        try:
            data = json.loads(value)
            # If it's a dict with 'password' or 'api_key', extract that
            if isinstance(data, dict):
                return data.get('password') or data.get('api_key') or value
            return value
        except json.JSONDecodeError:
            # Plain string value
            return value

    except Exception as e:
        print(f"ERROR: Failed to retrieve parameter {parameter_name}: {str(e)}")
        return None


# Initialize parameters from Parameter Store on cold start (FREE!)
GARDENCAM_PASSWORD = get_parameter(GARDENCAM_PARAMETER_NAME)
if not GARDENCAM_PASSWORD:
    print(f"WARNING: Could not retrieve password from Parameter Store ({GARDENCAM_PARAMETER_NAME}). Gardencam will be inaccessible.")

TFL_API_KEY = get_parameter(TFL_PARAMETER_NAME)
if TFL_API_KEY:
    print("TfL API key loaded from Parameter Store (FREE!)")
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


def get_images_for_date(date_str):
    """Get gardencam images for a specific date (YYYY-MM-DD).

    Uses S3 prefix filtering for efficiency - only fetches ~240 images instead of all 2,876.
    """
    if not BOTO3_AVAILABLE:
        return []

    # Convert date to S3 prefix: 2026-02-15 → garden_20260215
    date_prefix = f"garden_{date_str.replace('-', '')}"

    s3 = boto3.client("s3", region_name=GARDENCAM_REGION)

    try:
        response = s3.list_objects_v2(
            Bucket=GARDENCAM_BUCKET,
            Prefix=date_prefix,
            MaxKeys=1000  # One day shouldn't have more than 240 images
        )

        if "Contents" not in response:
            return []

        # Convert to standard format with timestamps
        images = []
        for obj in response["Contents"]:
            key = obj["Key"]
            # Skip thumbnails
            if key.startswith('thumb_'):
                continue

            # Extract timestamp from filename: garden_20260215_123456.jpg
            try:
                parts = key.replace('.jpg', '').split('_')
                date_part = parts[1]  # 20260215
                time_part = parts[2]  # 123456

                # Format: YYYY-MM-DD HH:MM:SS
                timestamp = f"{date_part[:4]}-{date_part[4:6]}-{date_part[6:8]} {time_part[:2]}:{time_part[2:4]}:{time_part[4:6]}"

                images.append({
                    'key': key,
                    'timestamp': timestamp,
                    'LastModified': obj['LastModified']
                })
            except:
                pass

        # Sort by timestamp descending
        images.sort(key=lambda x: x['timestamp'], reverse=True)
        return images

    except Exception as e:
        print(f"Error fetching images for date {date_str}: {e}")
        return []


def generate_week_list():
    """Generate list of weeks from GARDENCAM_EARLIEST_IMAGE to today.

    Returns list of week strings in format "2026-W07 (Feb 09-Feb 15)" in reverse chronological order.
    Uses NO S3 queries - purely deterministic date arithmetic.
    """
    from datetime import datetime, timedelta

    # Parse earliest image date
    start_date = datetime.strptime(GARDENCAM_EARLIEST_IMAGE, '%Y-%m-%d')
    end_date = datetime.now()

    weeks = []
    current = end_date

    # Walk backwards from today to start, week by week
    while current >= start_date:
        iso_year, iso_week, iso_weekday = current.isocalendar()

        # Calculate Monday of this week
        days_since_monday = iso_weekday - 1
        monday = current - timedelta(days=days_since_monday)
        sunday = monday + timedelta(days=6)

        # Format: "2026-W07 (Feb 09-Feb 15)"
        week_str = f"{iso_year}-W{iso_week:02d} ({monday.strftime('%b %d')}-{sunday.strftime('%b %d')})"

        if week_str not in weeks:  # Avoid duplicates when scanning
            weeks.append(week_str)

        # Move to previous week
        current = monday - timedelta(days=1)

    return weeks


def get_days_in_week(week_str):
    """Get list of days in a week without loading any images.

    Args:
        week_str: Week string like "2026-W07 (Feb 09-Feb 15)"

    Returns list of day strings in format "2026-02-15 (Sunday)" - NO S3 queries.
    """
    from datetime import datetime, timedelta

    try:
        # Parse week string: "2026-W07 (Feb 09-Feb 15)" → extract dates
        parts = week_str.split(' ')
        year_week = parts[0]  # "2026-W07"
        year = int(year_week.split('-')[0])
        week_num = int(year_week.split('-W')[1])

        # Find Monday of this ISO week
        jan4 = datetime(year, 1, 4)
        week1_monday = jan4 - timedelta(days=jan4.weekday())
        target_monday = week1_monday + timedelta(weeks=week_num - 1)

        # Generate all 7 days (Monday to Sunday)
        days = []
        for day_offset in range(7):
            date = target_monday + timedelta(days=day_offset)
            day_str = date.strftime('%Y-%m-%d (%A)')  # "2026-02-15 (Sunday)"
            days.append(day_str)

        return days

    except Exception as e:
        print(f"Error generating days for week {week_str}: {e}")
        return []


def get_all_gardencam_images(max_keys=None):
    """Get all gardencam images from S3.

    Args:
        max_keys: If provided, only return the N most recent images
    """
    if not BOTO3_AVAILABLE:
        return []
    s3 = boto3.client("s3", region_name=GARDENCAM_REGION)

    # Optimization: S3 returns objects in alphabetical order
    # Since our keys are garden_YYYYMMDD_HHMMSS.jpg, alphabetical = chronological
    # We can fetch in reverse by starting from a recent date prefix

    if max_keys and max_keys < 100:
        # Fast path: fetch recent pages only
        # Start from current date and work backwards
        all_objects = []

        # Try last 7 days worth of prefixes
        for days_ago in range(7):
            date = datetime.utcnow() - timedelta(days=days_ago)
            date_prefix = f"garden_{date.strftime('%Y%m%d')}"

            try:
                response = s3.list_objects_v2(
                    Bucket=GARDENCAM_BUCKET,
                    Prefix=date_prefix,
                    MaxKeys=200  # Should cover a full day
                )
                if "Contents" in response:
                    all_objects.extend(response["Contents"])

                # If we have enough, stop searching
                if len(all_objects) >= max_keys * 2:
                    break
            except:
                pass
    else:
        # Full scan for large requests or galleries
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
    Fetches in batches until we have enough displayable images.
    """
    if not BOTO3_AVAILABLE:
        return []

    import time
    t0 = time.time()

    # Fetch images in increasing batch sizes until we have enough displayable ones
    # This handles cases where many images are filtered out
    batch_sizes = [50, 100, 200, 500, 1000]
    images = []
    all_images = []

    s3 = boto3.client("s3", region_name=GARDENCAM_REGION)

    for batch_size in batch_sizes:
        # Fetch images up to this batch size
        t_fetch = time.time()
        all_images = get_all_gardencam_images(max_keys=batch_size)
        print(f"[TIMING] get_all_gardencam_images({batch_size}) took {(time.time()-t_fetch)*1000:.0f}ms, returned {len(all_images)} images")

        # Process and filter images
        t1 = time.time()
        images = []

        for img in all_images:
            # Fetch stats for this image first to check if it should be displayed
            stats = get_image_stats_by_filename(img['key'])

            # Skip images that don't meet display criteria
            if not should_display_image(stats):
                continue

            # Use thumbnail for display (they're pre-generated by gardencam.py)
            # Trust that thumbnails exist - they're created automatically now
            thumb_key = f"thumb_{img['key']}"
            display_url = get_presigned_url(thumb_key)

            # Full-res URL for click-through
            full_url = get_presigned_url(img['key'])

            # Skip resolution lookup - not worth 600ms per page load for cosmetic info
            # Users can see resolution when clicking through to full image

            stats_display = format_stats_for_display(stats)

            images.append({
                'url': display_url,
                'full_url': full_url,
                'timestamp': img['timestamp'],
                'key': img['key'],
                'resolution': '',  # Skipped for performance
                'stats_display': stats_display
            })

            # Stop once we have enough displayable images
            if len(images) >= count:
                break

        print(f"[TIMING] Filtering and URL generation took {(time.time()-t1)*1000:.0f}ms, got {len(images)}/{count} displayable")

        # If we have enough displayable images, we're done
        if len(images) >= count:
            break

        # If we fetched fewer images than requested, we've hit the end of available images
        if len(all_images) < batch_size:
            print(f"[INFO] Only {len(all_images)} images available, {len(images)} displayable")
            break

    print(f"[TIMING] Total get_latest_gardencam_images: {(time.time()-t0)*1000:.0f}ms")

    return images


def group_images_by_weeks(images):
    """Group images into weekly periods (Monday-Sunday)."""
    from collections import defaultdict
    from datetime import datetime

    weeks = defaultdict(list)

    for img in images:
        try:
            ts = img['timestamp']
            # Parse timestamp: YYYY-MM-DD HH:MM:SS
            dt = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')

            # Get ISO week (returns (year, week_number, weekday))
            iso_year, iso_week, iso_weekday = dt.isocalendar()

            # Calculate Monday of this week
            # ISO weekday: 1=Monday, 7=Sunday
            days_since_monday = iso_weekday - 1
            monday = dt - timedelta(days=days_since_monday)

            # Week key format: "2026-W03 (Jan 13-19)"
            sunday = monday + timedelta(days=6)
            week_key = f"{iso_year}-W{iso_week:02d} ({monday.strftime('%b %d')}-{sunday.strftime('%b %d')})"

            weeks[week_key].append(img)
        except Exception as e:
            weeks['Unknown'].append(img)

    # Sort weeks in reverse chronological order
    sorted_weeks = sorted(weeks.items(), reverse=True)
    return sorted_weeks


def group_images_by_days(images):
    """Group images by individual days."""
    from collections import defaultdict
    from datetime import datetime

    days = defaultdict(list)

    for img in images:
        try:
            ts = img['timestamp']
            # Parse timestamp: YYYY-MM-DD HH:MM:SS
            dt = datetime.strptime(ts, '%Y-%m-%d %H:%M:%S')

            # Day key format: "2026-02-15 (Saturday)"
            day_key = dt.strftime('%Y-%m-%d (%A)')

            days[day_key].append(img)
        except:
            days['Unknown'].append(img)

    # Sort days in reverse chronological order
    sorted_days = sorted(days.items(), reverse=True)
    return sorted_days


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


def get_image_stats_by_filename(filename):
    """Get statistics for a specific image from DynamoDB."""
    if not BOTO3_AVAILABLE:
        return None

    try:
        # Extract just the filename without path
        if '/' in filename:
            filename = filename.split('/')[-1]
        # Remove thumb_ prefix if present
        if filename.startswith('thumb_'):
            filename = filename[6:]

        dynamodb = boto3.resource('dynamodb', region_name=GARDENCAM_REGION)
        table = dynamodb.Table('gardencam-stats')

        response = table.get_item(Key={'filename': filename})
        return response.get('Item')
    except Exception as e:
        print(f"Error fetching stats for {filename}: {e}")
        return None


def should_display_image(stats):
    """Check if image should be displayed (filter out redundant bright daytime images)."""
    if not stats:
        return True  # Show images without stats

    brightness = float(stats.get('avg_brightness', 0))
    diff = float(stats.get('image_diff', 0))

    # Hide images with high brightness and low change (same criteria as capture script)
    if brightness > 100 and diff < 15:
        return False

    return True


def calculate_time_delta(current_timestamp, previous_timestamp):
    """Calculate time delta between two timestamps and format as string.

    Returns string like "+2m", "+15m", "+1h 5m", etc.
    Returns empty string if calculation fails.
    """
    if not current_timestamp or not previous_timestamp:
        return ""

    try:
        # Parse timestamps - handle both "YYYY-MM-DD HH:MM:SS" and "HH:MM:SS" formats
        from datetime import datetime

        # If timestamps have dates, parse full format
        if ' ' in current_timestamp and len(current_timestamp) > 10:
            current_dt = datetime.strptime(current_timestamp, "%Y-%m-%d %H:%M:%S")
            previous_dt = datetime.strptime(previous_timestamp, "%Y-%m-%d %H:%M:%S")
        else:
            # Time-only format - assume same day
            current_dt = datetime.strptime(current_timestamp, "%H:%M:%S")
            previous_dt = datetime.strptime(previous_timestamp, "%H:%M:%S")

        # Calculate delta (previous is later/more recent, current is earlier in the list)
        # So we want previous - current to get the time gap
        delta_seconds = abs((previous_dt - current_dt).total_seconds())

        # Format delta
        minutes = int(delta_seconds / 60)
        hours = minutes // 60
        remaining_minutes = minutes % 60

        if hours > 0:
            if remaining_minutes > 0:
                return f"+{hours}h {remaining_minutes}m"
            else:
                return f"+{hours}h"
        elif minutes > 0:
            return f"+{minutes}m"
        else:
            return "+<1m"

    except Exception as e:
        print(f"Error calculating time delta: {e}")
        return ""


def format_stats_for_display(stats):
    """Format brightness, image diff, and SD for display with 3 significant figures."""
    if not stats:
        return ""

    brightness = float(stats.get('avg_brightness', 0))
    # Use image_diff (difference from previous image)
    diff = float(stats.get('image_diff', 0))
    # Use noise_floor as standard deviation
    sd = float(stats.get('noise_floor', 0))

    # Format to 3 significant figures
    from decimal import Decimal

    def format_sig_figs(value, sig_figs=3):
        if value == 0:
            return "0.00"
        d = Decimal(str(value))
        return f"{d:.{sig_figs}g}"

    brightness_str = format_sig_figs(brightness)
    diff_str = format_sig_figs(diff)
    sd_str = format_sig_figs(sd)

    return f" | B:{brightness_str} Δ:{diff_str} SD:{sd_str}"


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
    print(f"Found {len(functions)} Lambda functions: {functions}")
    all_metrics = {}

    for func_name in functions:
        metrics = get_cloudwatch_metrics(func_name, days)
        print(f"Metrics for {func_name}: {len(metrics)} metric types")
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

        items = []
        response = table.scan()
        items.extend(response.get('Items', []))

        # Handle pagination - keep scanning until we have all items
        while 'LastEvaluatedKey' in response:
            response = table.scan(ExclusiveStartKey=response['LastEvaluatedKey'])
            items.extend(response.get('Items', []))

        # Sort by timestamp
        items.sort(key=lambda x: x.get('timestamp', ''), reverse=True)

        return items
    except Exception as e:
        print(f"Error fetching Lambda execution stats from DynamoDB: {e}")
        return []


# ============================================================================
# Pi Fleet Status Dashboard
# ============================================================================

def get_pi_fleet_status():
    """Get Pi fleet status from DynamoDB."""
    if not BOTO3_AVAILABLE:
        return []

    try:
        dynamodb = boto3.resource('dynamodb', region_name=GARDENCAM_REGION)
        table = dynamodb.Table('pi-fleet-status')

        response = table.scan()
        items = response.get('Items', [])

        # Handle pagination
        while 'LastEvaluatedKey' in response:
            response = table.scan(ExclusiveStartKey=response['LastEvaluatedKey'])
            items.extend(response.get('Items', []))

        # Sort by hostname
        items.sort(key=lambda x: x.get('hostname', ''))

        return items
    except Exception as e:
        print(f"Error fetching pi-fleet status from DynamoDB: {e}")
        return []


def format_uptime(seconds):
    """Format uptime in a human-readable way."""
    if not seconds:
        return "Unknown"

    days = seconds // 86400
    hours = (seconds % 86400) // 3600
    minutes = (seconds % 3600) // 60

    parts = []
    if days > 0:
        parts.append(f"{days}d")
    if hours > 0:
        parts.append(f"{hours}h")
    if minutes > 0 or not parts:
        parts.append(f"{minutes}m")

    return " ".join(parts)


def is_pi_online(last_seen):
    """Check if Pi is online based on last_seen timestamp."""
    if not last_seen:
        return False

    try:
        from dateutil import parser
        last_seen_dt = parser.parse(last_seen)
    except:
        try:
            last_seen_dt = datetime.fromisoformat(last_seen.replace('Z', '+00:00'))
        except:
            return False

    # Consider online if seen in last 2 minutes
    now = datetime.now(timezone.utc)
    delta = (now - last_seen_dt).total_seconds()
    return delta < 120


def render_pi_fleet_page(pis):
    """Render the Pi Fleet dashboard HTML."""

    # Count online/offline
    online_count = sum(1 for pi in pis if is_pi_online(pi.get('last_seen')))
    offline_count = len(pis) - online_count

    html = '''
    <!DOCTYPE html>
    <html lang="en">
    <head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pi Fleet Status</title>
    <style>
        body {
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif;
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            min-height: 100vh;
            margin: 0;
            padding: 2rem;
        }

        .container {
            max-width: 1200px;
            margin: 0 auto;
        }

        h1 {
            color: white;
            text-align: center;
            margin-bottom: 2rem;
            text-shadow: 2px 2px 4px rgba(0,0,0,0.3);
        }

        .home-link {
            text-align: center;
            margin-bottom: 1rem;
        }

        .home-link a {
            color: white;
            text-decoration: none;
            font-size: 1rem;
            opacity: 0.9;
        }

        .home-link a:hover {
            opacity: 1;
            text-decoration: underline;
        }

        .summary {
            background: rgba(255, 255, 255, 0.95);
            border-radius: 12px;
            padding: 1.5rem;
            margin-bottom: 2rem;
            box-shadow: 0 4px 6px rgba(0,0,0,0.2);
            display: flex;
            justify-content: space-around;
            flex-wrap: wrap;
            gap: 1rem;
        }

        .summary-item {
            text-align: center;
        }

        .summary-value {
            font-size: 2rem;
            font-weight: bold;
            margin-bottom: 0.25rem;
        }

        .summary-label {
            color: #666;
            font-size: 0.9rem;
        }

        .online { color: #10b981; }
        .offline { color: #ef4444; }
        .total { color: #667eea; }

        .pi-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(350px, 1fr));
            gap: 1.5rem;
        }

        .pi-card {
            background: rgba(255, 255, 255, 0.95);
            border-radius: 12px;
            padding: 1.5rem;
            box-shadow: 0 4px 6px rgba(0,0,0,0.2);
            transition: transform 0.2s;
        }

        .pi-card:hover {
            transform: translateY(-5px);
            box-shadow: 0 8px 12px rgba(0,0,0,0.3);
        }

        .pi-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1rem;
            padding-bottom: 1rem;
            border-bottom: 2px solid #e5e7eb;
        }

        .pi-hostname {
            font-size: 1.3rem;
            font-weight: bold;
            color: #333;
        }

        .pi-status {
            font-size: 0.9rem;
            font-weight: 600;
            padding: 0.25rem 0.75rem;
            border-radius: 12px;
        }

        .status-online {
            background: #d1fae5;
            color: #065f46;
        }

        .status-offline {
            background: #fee2e2;
            color: #991b1b;
        }

        .pi-info {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 0.75rem;
            margin-bottom: 1rem;
        }

        .info-item {
            font-size: 0.9rem;
        }

        .info-label {
            color: #666;
            font-weight: 500;
        }

        .info-value {
            color: #333;
            font-weight: 600;
        }

        .metrics {
            display: flex;
            gap: 1rem;
            margin-top: 1rem;
            padding-top: 1rem;
            border-top: 1px solid #e5e7eb;
        }

        .metric {
            flex: 1;
            text-align: center;
        }

        .metric-value {
            font-size: 1.5rem;
            font-weight: bold;
            color: #667eea;
        }

        .metric-label {
            font-size: 0.75rem;
            color: #666;
            text-transform: uppercase;
        }

        .boot-progress {
            margin-top: 1rem;
            padding: 0.75rem;
            background: #fef3c7;
            border-left: 4px solid #f59e0b;
            border-radius: 4px;
            font-size: 0.85rem;
        }

        .error-box {
            margin-top: 1rem;
            padding: 0.75rem;
            background: #fee2e2;
            border-left: 4px solid #ef4444;
            border-radius: 4px;
            font-size: 0.85rem;
            color: #991b1b;
        }

        .empty-state {
            background: rgba(255, 255, 255, 0.95);
            border-radius: 12px;
            padding: 3rem;
            text-align: center;
            box-shadow: 0 4px 6px rgba(0,0,0,0.2);
        }

        .empty-state h2 {
            color: #666;
            margin-bottom: 1rem;
        }

        .auto-refresh {
            text-align: center;
            color: white;
            margin-top: 2rem;
            font-size: 0.9rem;
            opacity: 0.8;
        }

        @media (max-width: 768px) {
            .pi-grid {
                grid-template-columns: 1fr;
            }

            .pi-info {
                grid-template-columns: 1fr;
            }
        }
    </style>
    </head>
    <body>
    <div class="container">
        <div class="home-link">
            <a href="contents">← Home</a>
        </div>

        <h1>Pi Fleet Status</h1>
    '''

    html += f'''
        <div class="summary">
            <div class="summary-item">
                <div class="summary-value online">{online_count}</div>
                <div class="summary-label">Online</div>
            </div>
            <div class="summary-item">
                <div class="summary-value offline">{offline_count}</div>
                <div class="summary-label">Offline</div>
            </div>
            <div class="summary-item">
                <div class="summary-value total">{len(pis)}</div>
                <div class="summary-label">Total Devices</div>
            </div>
        </div>
    '''

    if not pis:
        html += '''
        <div class="empty-state">
            <h2>No Devices Found</h2>
            <p>No Raspberry Pis have reported their status yet.</p>
            <p>Make sure the pi-fleet-reporter service is running on your devices.</p>
        </div>
        '''
    else:
        html += '<div class="pi-grid">'

        for pi in pis:
            hostname = pi.get('hostname', 'unknown')
            online = is_pi_online(pi.get('last_seen'))
            status_class = 'status-online' if online else 'status-offline'
            status_text = 'Online' if online else 'Offline'

            serial = pi.get('serial', 'unknown')
            local_ip = pi.get('local_ip', 'unknown')
            app_name = pi.get('app_name', 'unknown')
            uptime = format_uptime(pi.get('uptime_seconds', 0))

            cpu = pi.get('cpu_percent', 0)
            mem = pi.get('memory_percent', 0)
            disk = pi.get('disk_percent', 0)

            tunnel_active = pi.get('tunnel_active', False)
            tunnel_port = pi.get('tunnel_port', 0)
            bastion_host = pi.get('bastion_host', 'unknown')

            last_seen = pi.get('last_seen', 'Never')
            try:
                from dateutil import parser
                last_seen_dt = parser.parse(last_seen)
                last_seen_str = last_seen_dt.strftime('%Y-%m-%d %H:%M:%S UTC')
            except:
                try:
                    last_seen_dt = datetime.fromisoformat(last_seen.replace('Z', '+00:00'))
                    last_seen_str = last_seen_dt.strftime('%Y-%m-%d %H:%M:%S UTC')
                except:
                    last_seen_str = last_seen

            html += f'''
            <div class="pi-card">
                <div class="pi-header">
                    <div class="pi-hostname">{hostname}</div>
                    <div class="pi-status {status_class}">{status_text}</div>
                </div>

                <div class="pi-info">
                    <div class="info-item">
                        <div class="info-label">Serial</div>
                        <div class="info-value">{serial}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">Local IP</div>
                        <div class="info-value">{local_ip}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">App</div>
                        <div class="info-value">{app_name}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">Uptime</div>
                        <div class="info-value">{uptime}</div>
                    </div>
            '''

            if tunnel_active:
                html += f'''
                    <div class="info-item">
                        <div class="info-label">SSH Tunnel</div>
                        <div class="info-value">Port {tunnel_port}</div>
                    </div>
                    <div class="info-item">
                        <div class="info-label">Bastion</div>
                        <div class="info-value">{bastion_host}</div>
                    </div>
                '''

            html += f'''
                    <div class="info-item" style="grid-column: 1 / -1;">
                        <div class="info-label">Last Seen</div>
                        <div class="info-value">{last_seen_str}</div>
                    </div>
                </div>
            '''

            if online:
                html += f'''
                <div class="metrics">
                    <div class="metric">
                        <div class="metric-value">{cpu}%</div>
                        <div class="metric-label">CPU</div>
                    </div>
                    <div class="metric">
                        <div class="metric-value">{mem}%</div>
                        <div class="metric-label">Memory</div>
                    </div>
                    <div class="metric">
                        <div class="metric-value">{disk}%</div>
                        <div class="metric-label">Disk</div>
                    </div>
                </div>
                '''

            # Show boot progress if in provisioning
            boot_progress = pi.get('boot_progress') or pi.get('stage')
            if boot_progress and boot_progress.startswith('boot'):
                message = pi.get('message', 'Provisioning in progress...')
                html += f'''
                <div class="boot-progress">
                    <strong>🔄 Boot Progress:</strong> {boot_progress}<br>
                    {message}
                </div>
                '''

            # Show error if present
            error = pi.get('error')
            if error:
                html += f'''
                <div class="error-box">
                    <strong>⚠️ Error:</strong><br>
                    {error[:200]}{'...' if len(error) > 200 else ''}
                </div>
                '''

            html += '</div>'

        html += '</div>'

    html += '''
        <div class="auto-refresh">
            Page auto-refreshes every 30 seconds
        </div>
    </div>

    <script>
        // Auto-refresh every 30 seconds
        setTimeout(() => location.reload(), 30000);
    </script>
    </body>
    </html>
    '''

    return html


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

    elif path.startswith(f'/{stage}/gardencam/timing') or path.startswith('/gardencam/timing'):
        # Page load timing endpoint
        if method == 'POST':
            try:
                from decimal import Decimal

                body = event.get('body', '{}')
                timing_data = json.loads(body)

                # Log to DynamoDB
                if BOTO3_AVAILABLE:
                    dynamodb = boto3.resource('dynamodb', region_name=GARDENCAM_REGION)
                    table = dynamodb.Table('gardencam-page-timing')

                    item = {
                        'timestamp': timing_data.get('timestamp', datetime.utcnow().isoformat()),
                        'page_load_ms': Decimal(str(timing_data.get('pageLoadTime', 0))),
                        'dom_ready_ms': Decimal(str(timing_data.get('domReadyTime', 0))),
                        'server_response_ms': Decimal(str(timing_data.get('serverResponseTime', 0))),
                        'user_agent': timing_data.get('userAgent', '')[:500],
                        'ip': event.get('requestContext', {}).get('identity', {}).get('sourceIp', 'unknown')
                    }

                    table.put_item(Item=item)

                return {
                    'statusCode': 200,
                    'body': json.dumps({'status': 'logged'}),
                    'headers': {'Content-Type': 'application/json'}
                }
            except Exception as e:
                print(f"Error logging timing: {e}")
                return {
                    'statusCode': 500,
                    'body': json.dumps({'error': str(e)}),
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

        # Fetch more stats to ensure we have enough data for 8 days
        stats = get_gardencam_stats(limit=2000)

        # Get current time and define 8 time windows (last 24h + 7 previous days)
        now = datetime.now(timezone.utc)

        # Define 8 windows: today (last 24h), yesterday, day before, etc.
        windows = []
        for i in range(8):
            window_end = now - timedelta(days=i)
            window_start = window_end - timedelta(days=1)
            windows.append({
                'start': window_start,
                'end': window_end,
                'label': f'{window_start.strftime("%Y-%m-%d")} to {window_end.strftime("%Y-%m-%d")}' if i > 0 else 'Last 24 Hours',
                'data': []
            })

        # Group stats into windows
        for item in stats:
            ts_str = item.get('timestamp', '')
            if not ts_str:
                continue

            try:
                # Parse ISO timestamp
                ts = datetime.fromisoformat(ts_str.replace('Z', '+00:00'))
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=timezone.utc)

                # Find which window this belongs to
                for window in windows:
                    if window['start'] <= ts < window['end']:
                        window['data'].append({
                            'timestamp': ts,
                            'timestamp_str': ts.strftime('%H:%M'),
                            'avg_brightness': float(item.get('avg_brightness', 0)),
                            'mode': item.get('mode', 'unknown')
                        })
                        break
            except Exception as e:
                print(f"Error parsing timestamp {ts_str}: {e}")
                continue

        # Sort data within each window by timestamp
        for window in windows:
            window['data'].sort(key=lambda x: x['timestamp'])

        # Calculate summary stats
        total_images = sum(len(w['data']) for w in windows)
        all_modes = [d['mode'] for w in windows for d in w['data']]
        day_count = sum(1 for m in all_modes if m == 'day')
        night_count = sum(1 for m in all_modes if m == 'night')
        stacking_count = sum(1 for m in all_modes if m == 'stacking')
        all_brightness = [d['avg_brightness'] for w in windows for d in w['data']]
        avg_brightness = sum(all_brightness) / len(all_brightness) if all_brightness else 0

        html += f'''
        <title>Garden Camera Statistics</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
        <style>
            body {{ font-family: Arial, sans-serif; margin: 0; padding: 1rem; background: #1a1a1a; color: #fff; }}
            .nav {{ text-align: center; margin-bottom: 1.5rem; }}
            .nav a {{ color: #4a9eff; text-decoration: none; margin: 0 1rem; padding: 0.5rem 1rem; background: #2a2a2a; border-radius: 6px; display: inline-block; }}
            .nav a:hover {{ background: #3a3a3a; }}
            h1 {{ text-align: center; margin-bottom: 2rem; }}
            .chart-container {{ max-width: 1400px; margin: 0 auto 2rem auto; background: #2a2a2a; padding: 1.5rem; border-radius: 8px; }}
            .chart-title {{ font-size: 1.1rem; margin-bottom: 1rem; color: #aaa; text-align: center; }}
            .chart-subtitle {{ font-size: 0.9rem; color: #666; text-align: center; margin-top: -0.5rem; margin-bottom: 1rem; }}
            canvas {{ max-height: 300px; }}
            .stats-summary {{ max-width: 1400px; margin: 0 auto 2rem auto; padding: 1rem; background: #2a2a2a; border-radius: 8px; }}
            .stats-summary h2 {{ margin-top: 0; color: #aaa; }}
            .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; }}
            .stat-box {{ background: #1a1a1a; padding: 1rem; border-radius: 6px; text-align: center; }}
            .stat-value {{ font-size: 2rem; font-weight: bold; color: #4a9eff; }}
            .stat-label {{ color: #888; margin-top: 0.5rem; }}
            .legend {{ text-align: center; margin-bottom: 1rem; }}
            .legend-item {{ display: inline-block; margin: 0 1rem; }}
            .legend-color {{ display: inline-block; width: 20px; height: 20px; border-radius: 4px; vertical-align: middle; margin-right: 0.5rem; }}
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
            <h2>Summary (Last 8 Days)</h2>
            <div class="stats-grid">
                <div class="stat-box">
                    <div class="stat-value">{total_images}</div>
                    <div class="stat-label">Total Images</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">{day_count}</div>
                    <div class="stat-label">Day Mode</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">{night_count}</div>
                    <div class="stat-label">Night Mode</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">{stacking_count}</div>
                    <div class="stat-label">Stacking Mode</div>
                </div>
                <div class="stat-box">
                    <div class="stat-value">{avg_brightness:.1f}</div>
                    <div class="stat-label">Avg Brightness</div>
                </div>
            </div>
        </div>

        <div class="legend">
            <div class="legend-item">
                <span class="legend-color" style="background: #f59e0b;"></span>
                <span>Day Mode</span>
            </div>
            <div class="legend-item">
                <span class="legend-color" style="background: #3b82f6;"></span>
                <span>Night Mode</span>
            </div>
            <div class="legend-item">
                <span class="legend-color" style="background: #8b5cf6;"></span>
                <span>Stacking Mode</span>
            </div>
        </div>
        '''

        # Create a chart for each window
        for i, window in enumerate(windows):
            if not window['data']:
                continue

            # Separate data by mode for color coding
            day_data = []
            night_data = []
            stacking_data = []
            labels = []

            for d in window['data']:
                labels.append(d['timestamp_str'])
                if d['mode'] == 'day':
                    day_data.append(d['avg_brightness'])
                    night_data.append(None)
                    stacking_data.append(None)
                elif d['mode'] == 'stacking':
                    day_data.append(None)
                    night_data.append(None)
                    stacking_data.append(d['avg_brightness'])
                else:  # night mode
                    day_data.append(None)
                    night_data.append(d['avg_brightness'])
                    stacking_data.append(None)

            chart_id = f'chart_{i}'
            data_count = len(window['data'])

            html += f'''
        <div class="chart-container">
            <div class="chart-title">{window['label']}</div>
            <div class="chart-subtitle">{data_count} images</div>
            <canvas id="{chart_id}"></canvas>
        </div>
            '''

        # Add JavaScript to create all charts
        html += '''
        <script>
        const chartOptions = {
            responsive: true,
            maintainAspectRatio: true,
            scales: {
                x: {
                    ticks: { color: '#888', maxTicksLimit: 12 },
                    grid: { color: '#333' }
                },
                y: {
                    min: 0,
                    max: 255,
                    ticks: { color: '#888' },
                    grid: { color: '#333' },
                    title: {
                        display: true,
                        text: 'Uncorrected Brightness',
                        color: '#aaa'
                    }
                }
            },
            plugins: {
                legend: {
                    display: false
                },
                tooltip: {
                    callbacks: {
                        title: function(context) {
                            return context[0].label;
                        },
                        label: function(context) {
                            return 'Brightness: ' + context.parsed.y.toFixed(1);
                        }
                    }
                }
            },
            elements: {
                point: {
                    radius: 3,
                    hoverRadius: 6
                },
                line: {
                    borderWidth: 2
                }
            }
        };
        '''

        # Generate chart creation code for each window
        for i, window in enumerate(windows):
            if not window['data']:
                continue

            # Separate data by mode
            day_data = []
            night_data = []
            stacking_data = []
            labels = []

            for d in window['data']:
                labels.append(d['timestamp_str'])
                if d['mode'] == 'day':
                    day_data.append(d['avg_brightness'])
                    night_data.append(None)
                    stacking_data.append(None)
                elif d['mode'] == 'stacking':
                    day_data.append(None)
                    night_data.append(None)
                    stacking_data.append(d['avg_brightness'])
                else:  # night mode
                    day_data.append(None)
                    night_data.append(d['avg_brightness'])
                    stacking_data.append(None)

            chart_id = f'chart_{i}'

            html += f'''
        new Chart(document.getElementById('{chart_id}'), {{
            type: 'line',
            data: {{
                labels: {json.dumps(labels)},
                datasets: [
                    {{
                        label: 'Day',
                        data: {json.dumps(day_data)},
                        borderColor: '#f59e0b',
                        backgroundColor: '#f59e0b',
                        spanGaps: false
                    }},
                    {{
                        label: 'Night',
                        data: {json.dumps(night_data)},
                        borderColor: '#3b82f6',
                        backgroundColor: '#3b82f6',
                        spanGaps: false
                    }},
                    {{
                        label: 'Stacking',
                        data: {json.dumps(stacking_data)},
                        borderColor: '#8b5cf6',
                        backgroundColor: '#8b5cf6',
                        spanGaps: false
                    }}
                ]
            }},
            options: chartOptions
        }});
            '''

        html += '''
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

            # Fetch stats for this image
            stats = get_image_stats_by_filename(image_key)
            stats_display = format_stats_for_display(stats)

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
            <h2>{timestamp} UTC{stats_display}</h2>
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

            # Fetch stats for this image
            stats = get_image_stats_by_filename(image_key)
            stats_display = format_stats_for_display(stats)

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
            <h2>{timestamp} UTC{stats_display}</h2>
            <div class="image-container">
                <a href="fullres?key={image_key}">
                    <img src="{image_url}" alt="Display width image">
                </a>
            </div>
            '''
        else:
            html += '<h1>Error: No image specified</h1>'
    elif path.startswith(f'/{stage}/gardencam/gallery') or path.startswith('/gardencam/gallery'):
        # Gallery page with thumbnails organized by weeks
        if not check_basic_auth(event, GARDENCAM_PASSWORD):
            return {
                'statusCode': 401,
                'body': '<html><body><h1>401 Unauthorized</h1><p>Access denied.</p></body></html>',
                'headers': {
                    'Content-Type': 'text/html',
                    'WWW-Authenticate': 'Basic realm="Garden Camera"'
                }
            }

        # Get query parameters
        query_params = event.get('queryStringParameters', {}) or {}
        week_param = query_params.get('week', '')
        day_param = query_params.get('day', '')

        # Three-level navigation: Weeks → Days → Images
        # OPTIMIZED: Only load S3 data when needed
        if not week_param:
            # Week index - generate deterministically, NO S3 queries
            weeks = generate_week_list()

            html += '''
            <title>Garden Camera Gallery - Weekly Index</title>
            <style>
                body { font-family: Arial, sans-serif; margin: 0; padding: 1rem; background: #1a1a1a; color: #fff; }
                .nav { text-align: center; margin-bottom: 2rem; }
                .nav a { color: #4a9eff; text-decoration: none; margin: 0 1rem; }
                .nav a:hover { text-decoration: underline; }
                h1 { text-align: center; margin-bottom: 2rem; }
                .week-list { max-width: 800px; margin: 0 auto; }
                .week-link { display: block; padding: 1rem 1.5rem; margin-bottom: 0.75rem; background: #2a2a2a; border-radius: 8px; text-decoration: none; color: #4a9eff; font-size: 1.1rem; transition: background 0.3s; }
                .week-link:hover { background: #3a3a3a; }
                .week-count { float: right; color: #888; font-size: 0.9rem; }
            </style>
            <div class="nav">
                <a href="../../contents">Home</a>
                <a href="../gardencam">Latest</a>
                <a href="videos">Videos</a>
                <a href="stats">Statistics</a>
            </div>
            <h1>Garden Camera Gallery - By Week</h1>
            <div class="week-list">
            '''

            for week_name in weeks:
                # Just show the week - no counting, no S3 queries
                html += f'''
                <a href="gallery?week={week_name}" class="week-link">
                    {week_name}
                </a>
                '''

            html += '</div>'

        elif week_param and not day_param:
            # Show days in the selected week - OPTIMIZED: only load images for this week
            current_week_images = get_images_for_week(week_param)

            if not current_week_images:
                html += '<h1>Week not found</h1><p><a href="gallery">Back to Gallery Index</a></p>'
            else:
                # Group week's images by day
                days = group_images_by_days(current_week_images)

                html += f'''
                <title>{week_param} - Days</title>
                <style>
                    body {{ font-family: Arial, sans-serif; margin: 0; padding: 1rem; background: #1a1a1a; color: #fff; }}
                    .nav {{ text-align: center; margin-bottom: 2rem; }}
                    .nav a {{ color: #4a9eff; text-decoration: none; margin: 0 1rem; }}
                    .nav a:hover {{ text-decoration: underline; }}
                    h1 {{ text-align: center; margin-bottom: 2rem; }}
                    .day-list {{ max-width: 800px; margin: 0 auto; }}
                    .day-link {{ display: block; padding: 1rem 1.5rem; margin-bottom: 0.75rem; background: #2a2a2a; border-radius: 8px; text-decoration: none; color: #4a9eff; font-size: 1.1rem; transition: background 0.3s; }}
                    .day-link:hover {{ background: #3a3a3a; }}
                </style>
                <div class="nav">
                    <a href="../../contents">Home</a>
                    <a href="gallery">All Weeks</a>
                    <a href="../gardencam">Latest</a>
                </div>
                <h1>{week_param}</h1>
                <div class="day-list">
                '''

                for day_name, day_images in days:
                    html += f'''
                    <a href="gallery?week={week_param}&day={day_name}" class="day-link">
                        {day_name}
                    </a>
                    '''

                html += '</div>'

        else:
            # Show images for a specific day - OPTIMIZED: only fetch images for this day
            # Extract date from day_param: "2026-02-15 (Saturday)" → "2026-02-15"
            try:
                date_only = day_param.split(' ')[0]  # Get YYYY-MM-DD part
                current_day_images = get_images_for_date(date_only)
            except:
                current_day_images = []

            if not current_day_images:
                html += f'<h1>No images found for {day_param}</h1><p><a href="gallery?week={week_param}">Back to {week_param}</a></p>'
            else:
                # Get days in this week for prev/next navigation
                # Only load week data if needed for navigation
                current_week_images = get_images_for_week(week_param)
                days = group_images_by_days(current_week_images) if current_week_images else []
                day_index = None
                for idx, (day_name, _) in enumerate(days):
                    if day_name == day_param:
                        day_index = idx
                        break

                if day_index is None:
                    day_index = 0  # Fallback

                # Build navigation links
                prev_link = ''
                next_link = ''
                if day_index > 0:
                    prev_day = days[day_index - 1][0]
                    prev_link = f'<a href="gallery?week={week_param}&day={prev_day}">← Previous Day</a>'
                if day_index < len(days) - 1:
                    next_day = days[day_index + 1][0]
                    next_link = f'<a href="gallery?week={week_param}&day={next_day}">Next Day →</a>'

                html += f'''
                <title>{day_param} - Gallery</title>
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
                        <a href="gallery?week={week_param}">Week Index</a>
                        <a href="gallery">All Weeks</a>
                        <a href="../gardencam">Latest</a>
                        {next_link}
                    </div>
                    <h1>{day_param}</h1>
                '''

                html += '<div class="thumbnails">'

                displayed_count = 0
                displayed_images = []  # Track displayed images for delta calculation

                for img in current_day_images:
                    # Fetch stats for this image
                    stats = get_image_stats_by_filename(img['key'])

                    # Skip images that don't meet display criteria
                    if not should_display_image(stats):
                        continue

                    thumb_url = get_presigned_url(img['key'])
                    time_only = img['timestamp'].split()[1] if ' ' in img['timestamp'] else img['timestamp']
                    stats_display = format_stats_for_display(stats)

                    # Calculate time delta from previous displayed image
                    time_delta = ""
                    if displayed_images:
                        previous_img = displayed_images[-1]
                        time_delta = calculate_time_delta(img['timestamp'], previous_img['timestamp'])
                        if time_delta:
                            time_delta = f"{time_delta} "  # Add space after delta

                    html += f'''
                    <div class="thumb-container">
                        <a href="display?key={img['key']}">
                            <img src="{thumb_url}" alt="{img['timestamp']}">
                        </a>
                        <div class="thumb-time">{time_delta}{time_only}{stats_display}</div>
                    </div>
                    '''
                    displayed_count += 1
                    displayed_images.append(img)

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
                            if '-' in date_part:
                                # Weekly format
                                start_date, end_date = date_part.split('-')
                                video_type = 'weekly'
                            else:
                                # Daily format
                                start_date = date_part
                                end_date = date_part
                                video_type = 'daily'

                            start_formatted = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
                            end_formatted = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
                        except:
                            start_formatted = "Unknown"
                            end_formatted = "Unknown"
                            video_type = 'unknown'

                        videos.append({
                            'id': video_id,
                            'key': key,
                            'size_mb': obj['Size'] / 1048576,
                            'start_date': start_formatted,
                            'end_date': end_formatted,
                            'type': video_type
                        })

            videos.sort(key=lambda v: v['id'], reverse=True)
            weekly_videos = [v for v in videos if v['type'] == 'weekly']
            daily_videos = [v for v in videos if v['type'] == 'daily']
            latest_weekly = weekly_videos[:3]
            latest_daily = daily_videos[:3]
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
                <p>Automated timelapse videos created from garden camera images:</p>
                <ul style="color: #aaa; margin: 1rem 0;">
                    <li><strong>Weekly:</strong> 7 days condensed, 24fps, ~5 seconds</li>
                    <li><strong>Daily:</strong> 24 hours, 12fps, all captures shown</li>
                </ul>

                <div class="stats">
                    <div class="stat-box">
                        <div class="stat-value">{total_videos}</div>
                        <div class="stat-label">Total Videos</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-value">{len(weekly_videos)}</div>
                        <div class="stat-label">Weekly</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-value">{len(daily_videos)}</div>
                        <div class="stat-label">Daily</div>
                    </div>
                </div>

                <div style="text-align: center; margin-top: 1.5rem;">
                    <a href="videos" class="button">View All Videos</a>
                    <a href="timelapse/schedule" class="button secondary">View Schedule</a>
                </div>
            </div>

            <div class="info-section latest-videos">
                <h2>Latest Weekly Videos</h2>
                <ul class="video-list">
        '''

        for video in latest_weekly:
            html += f'''
                <li>
                    <div>
                        <div class="video-title">Week of {video['start_date']}</div>
                        <div class="video-date">{video['start_date']} to {video['end_date']} • {video['size_mb']:.1f} MB</div>
                    </div>
                    <a href="video?id={video['id']}" class="button">Watch</a>
                </li>
            '''

        html += '''
                </ul>
            </div>

            <div class="info-section latest-videos">
                <h2>Latest Daily Videos</h2>
                <ul class="video-list">
        '''

        for video in latest_daily:
            html += f'''
                <li>
                    <div>
                        <div class="video-title">{video['start_date']}</div>
                        <div class="video-date">{video['size_mb']:.1f} MB</div>
                    </div>
                    <a href="video?id={video['id']}" class="button">Watch</a>
                </li>
            '''

        html += '''
                </ul>
            </div>
        </div>
        '''

    elif (path.startswith(f'/{stage}/gardencam/video?') or path.startswith('/gardencam/video?') or
          path == f'/{stage}/gardencam/video' or path == '/gardencam/video'):
        # Single timelapse video player page (not /videos)
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
                        # Extract video ID from filename: videos/timelapse_YYYYMMDD-YYYYMMDD.mp4 or videos/timelapse_YYYYMMDD.mp4
                        video_id = key.replace('videos/', '').replace('.mp4', '')

                        # Parse date range from filename
                        try:
                            date_part = video_id.replace('timelapse_', '')
                            if '-' in date_part:
                                # Weekly format: YYYYMMDD-YYYYMMDD
                                start_date, end_date = date_part.split('-')
                                video_type = 'weekly'
                            else:
                                # Daily format: YYYYMMDD
                                start_date = date_part
                                end_date = date_part
                                video_type = 'daily'

                            start_formatted = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
                            end_formatted = f"{end_date[:4]}-{end_date[4:6]}-{end_date[6:]}"
                        except:
                            start_formatted = "Unknown"
                            end_formatted = "Unknown"
                            video_type = 'unknown'

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
                            'duration': duration,
                            'type': video_type
                        })

            # Sort by video ID (date) descending and separate by type
            videos.sort(key=lambda v: v['id'], reverse=True)
            weekly_videos = [v for v in videos if v['type'] == 'weekly']
            daily_videos = [v for v in videos if v['type'] == 'daily']

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

        if weekly_videos or daily_videos:
            # Weekly videos section
            if weekly_videos:
                html += f'''
                <div style="max-width: 1400px; margin: 0 auto 3rem auto;">
                    <h2 style="color: #4a9eff; margin-bottom: 1rem; padding-left: 0.5rem;">Weekly Timelapses ({len(weekly_videos)})</h2>
                    <p style="color: #888; margin-bottom: 1.5rem; padding-left: 0.5rem;">7-day timelapses at 24fps, ~5 seconds each</p>
                    <div class="video-grid">
                '''
                for video in weekly_videos:
                    html += f'''
                    <a href="video?id={video['id']}" class="video-card">
                        <div class="video-thumbnail">
                            <div class="play-icon"></div>
                        </div>
                        <div class="video-metadata">
                            <h3>Week of {video['start_date']}</h3>
                            <p>{video['start_date']} to {video['end_date']}</p>
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
                html += '</div></div>'

            # Daily videos section
            if daily_videos:
                html += f'''
                <div style="max-width: 1400px; margin: 0 auto;">
                    <h2 style="color: #4a9eff; margin-bottom: 1rem; padding-left: 0.5rem;">Daily Timelapses ({len(daily_videos)})</h2>
                    <p style="color: #888; margin-bottom: 1.5rem; padding-left: 0.5rem;">24-hour timelapses at 12fps, showing every capture</p>
                    <div class="video-grid">
                '''
                for video in daily_videos:
                    html += f'''
                    <a href="video?id={video['id']}" class="video-card">
                        <div class="video-thumbnail">
                            <div class="play-icon"></div>
                        </div>
                        <div class="video-metadata">
                            <h3>{video['start_date']}</h3>
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
                html += '</div></div>'
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

            // Page load performance tracking
            window.addEventListener('load', function() {{
                // Wait a bit for images to fully load
                setTimeout(function() {{
                    const perfData = window.performance.timing;
                    const pageLoadTime = perfData.loadEventEnd - perfData.navigationStart;
                    const domReadyTime = perfData.domContentLoadedEventEnd - perfData.navigationStart;
                    const serverResponseTime = perfData.responseEnd - perfData.requestStart;

                    // Send timing data to server
                    fetch('gardencam/timing', {{
                        method: 'POST',
                        headers: {{ 'Content-Type': 'application/json' }},
                        body: JSON.stringify({{
                            pageLoadTime: pageLoadTime,
                            domReadyTime: domReadyTime,
                            serverResponseTime: serverResponseTime,
                            timestamp: new Date().toISOString(),
                            userAgent: navigator.userAgent
                        }})
                    }}).catch(err => console.log('Timing log failed:', err));
                }}, 500);
            }});
            </script>
            <div class="gallery">
            '''
            labels = ['Latest', 'Previous', 'Earlier']
            for idx, img in enumerate(images):
                label = labels[idx] if idx < len(labels) else f'Image {idx+1}'
                resolution_display = f" • {img['resolution']}" if img.get('resolution') else ""
                stats_display = img.get('stats_display', '')

                # Calculate time delta from previous displayed image (if not first)
                time_delta = ""
                if idx > 0:
                    previous_img = images[idx - 1]
                    time_delta = calculate_time_delta(img['timestamp'], previous_img['timestamp'])
                    if time_delta:
                        time_delta = f"{time_delta} "  # Add space after delta

                html += f'''
                <div class="image-container">
                    <div class="label">{label}</div>
                    <a href="gardencam/display?key={img['key']}">
                        <img src="{img['url']}" alt="{label} capture">
                    </a>
                    <p class="timestamp">{time_delta}{img['timestamp']}{resolution_display}{stats_display}</p>
                </div>
                '''
            html += '</div>'
        else:
            html += '<title>Garden Camera</title><h1>Garden Camera</h1><p>No images available yet.</p>'

    elif path == f'/{stage}/lambda-stats/data' or path == '/lambda-stats/data':
        # Lambda statistics data endpoint - returns JSON
        # This does all the slow data fetching and returns it as JSON
        all_lambda_metrics = get_all_lambda_metrics(days=30)

        # Calculate aggregated stats
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

        # Load DynamoDB logs for IP/User-Agent and path analysis
        from collections import Counter, defaultdict
        stats = get_lambda_execution_stats()

        ip_data = defaultdict(lambda: {'count': 0, 'paths': Counter(), 'timestamps': [], 'user_agents': Counter()})
        ua_data = Counter()

        for item in stats:
            ip = item.get('ip_address', 'Unknown')
            ua = item.get('user_agent', 'Unknown')
            path_item = item.get('path', 'unknown')
            timestamp = item.get('timestamp', '')

            if ip and ip != 'Unknown':
                ip_data[ip]['count'] += 1
                ip_data[ip]['paths'][path_item] += 1
                ip_data[ip]['user_agents'][ua] += 1
                if timestamp:
                    try:
                        ts = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                        ip_data[ip]['timestamps'].append(ts)
                    except:
                        pass

            if ua and ua != 'Unknown':
                ua_data[ua] += 1

        # Get geolocation for top IPs (limit to 10 to avoid rate limits)
        import time as time_module
        top_ips = sorted(ip_data.items(), key=lambda x: x[1]['count'], reverse=True)[:10]
        ip_geo_data = []
        country_counts = Counter()

        for ip, data in top_ips:
            time_module.sleep(0.15)  # Rate limit
            geo = get_ip_geolocation(ip)
            top_path = data['paths'].most_common(1)[0] if data['paths'] else ('unknown', 0)
            top_ua = data['user_agents'].most_common(1)[0] if data['user_agents'] else ('Unknown', 0)

            ip_geo_data.append({
                'ip': ip,
                'count': data['count'],
                'country': geo['country'],
                'city': geo['city'],
                'top_path': top_path[0],
                'top_ua': top_ua[0]
            })
            country_counts[geo['country']] += data['count']

        top_uas = ua_data.most_common(10)

        # Path analysis (DynamoDB only, filter out empty paths from backfill)
        path_counts = Counter(item.get('path') for item in stats if item.get('path'))
        total_requests = sum(path_counts.values())
        top_paths = path_counts.most_common(10)

        # Generate histogram data for last 7 days with 28 buckets (6-hour intervals)
        # Aligned to midnight, 6am, noon, 6pm
        now = datetime.utcnow()

        # Find midnight 7 days ago
        seven_days_ago = now - timedelta(days=7)
        midnight_7_days_ago = seven_days_ago.replace(hour=0, minute=0, second=0, microsecond=0)

        bucket_duration = timedelta(hours=6)  # 6-hour buckets: 0-6, 6-12, 12-18, 18-24

        # Create 28 time buckets aligned to 0, 6, 12, 18 hours
        buckets = []
        for i in range(28):
            bucket_start = midnight_7_days_ago + (i * bucket_duration)
            bucket_end = bucket_start + bucket_duration
            # Calculate days ago from the middle of the bucket
            bucket_mid = bucket_start + (bucket_duration / 2)
            days_ago = (now - bucket_mid).total_seconds() / 86400
            buckets.append({
                'start': bucket_start,
                'end': bucket_end,
                'label': f'{days_ago:.1f}',
                'paths': Counter()
            })

        # Assign items to buckets
        for item in stats:
            timestamp_str = item.get('timestamp', '')
            path = item.get('path', '')
            if not timestamp_str or not path:
                continue

            try:
                ts = datetime.fromisoformat(timestamp_str.replace('Z', '+00:00'))
                if ts < midnight_7_days_ago:
                    continue  # Skip items older than 7 days (before midnight)

                # Find the correct bucket
                for bucket in buckets:
                    if bucket['start'] <= ts < bucket['end']:
                        bucket['paths'][path] += 1
                        break
            except:
                continue

        # Get top 10 paths overall for the legend
        recent_path_counts = Counter()
        for bucket in buckets:
            recent_path_counts.update(bucket['paths'])
        top_recent_paths = [path for path, _ in recent_path_counts.most_common(10)]

        # Prepare histogram data
        histogram_data = {
            'labels': [bucket['label'] for bucket in buckets],
            'datasets': []
        }

        # Create a dataset for each top path
        colors = [
            '#667eea', '#764ba2', '#f093fb', '#4facfe', '#43e97b',
            '#fa709a', '#fee140', '#30cfd0', '#a8edea', '#fed6e3'
        ]

        for i, path in enumerate(top_recent_paths):
            dataset = {
                'label': path if path else '(root)',
                'data': [bucket['paths'].get(path, 0) for bucket in buckets],
                'backgroundColor': colors[i % len(colors)]
            }
            histogram_data['datasets'].append(dataset)

        # Return JSON data
        return {
            'statusCode': 200,
            'body': json.dumps({
                'summary': {
                    'total_invocations': int(total_cw_invocations),
                    'total_errors': int(total_cw_errors),
                    'total_throttles': int(total_cw_throttles),
                    'error_rate': round(error_rate, 2),
                    'avg_duration': round(avg_cw_duration, 0),
                    'max_duration': round(max_cw_duration, 0)
                },
                'functions': [
                    {
                        'name': func_name,
                        'invocations': int(func_metrics['invocations']),
                        'errors': int(func_metrics['errors']),
                        'error_rate': round(func_metrics['error_rate'], 2),
                        'avg_duration': round(func_metrics['avg_duration'], 0),
                        'max_duration': round(func_metrics['max_duration'], 0)
                    }
                    for func_name, func_metrics in sorted_functions
                ],
                'paths': [
                    {
                        'path': path,
                        'count': count,
                        'percentage': round((count / total_requests * 100) if total_requests > 0 else 0, 1)
                    }
                    for path, count in top_paths
                ],
                'ips': ip_geo_data,
                'user_agents': [
                    {'user_agent': ua, 'count': count}
                    for ua, count in top_uas
                ],
                'histogram': histogram_data
            }),
            'headers': {'Content-Type': 'application/json'}
        }

    elif path == f'/{stage}/lambda-stats' or path == '/lambda-stats':
        # Lambda statistics page - returns HTML skeleton that loads data asynchronously

        html += '''
        <!DOCTYPE html>
        <html lang="en">
        <head>
        <meta charset="UTF-8">
        <title>Lambda CloudWatch Metrics</title>
        <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
        <style>
            body { font-family: Arial, sans-serif; margin: 2rem; background: #f5f5f5; }
            h1 { color: #333; }
            .container { max-width: 1200px; margin: 0 auto; background: white; padding: 2rem; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
            table { width: 100%; border-collapse: collapse; margin: 2rem 0; }
            th, td { padding: 0.75rem; text-align: left; border-bottom: 1px solid #ddd; }
            th { background: #667eea; color: white; font-weight: 600; }
            tr:hover { background: #f8f9fa; }
            .stats-grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 1rem; margin: 2rem 0; }
            .stat-box { background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); padding: 1.5rem; border-radius: 8px; text-align: center; color: white; }
            .stat-value { font-size: 2rem; font-weight: bold; }
            .stat-label { margin-top: 0.5rem; opacity: 0.9; }
            .nav { text-align: center; margin-bottom: 2rem; }
            .nav a { color: #667eea; text-decoration: none; padding: 0.5rem 1rem; background: #f0f0f0; border-radius: 4px; }
            .loading { text-align: center; padding: 2rem; color: #666; }
            .spinner { border: 3px solid #f3f3f3; border-top: 3px solid #667eea; border-radius: 50%; width: 40px; height: 40px; animation: spin 1s linear infinite; margin: 1rem auto; }
            @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
            .error { background: #fee; padding: 1rem; border-radius: 4px; color: #c00; margin: 1rem 0; }
            .chart-container { margin: 2rem 0; padding: 1rem; background: #f8f9fa; border-radius: 8px; }
        </style>
        </head>
        <body>
        <div class="container">
            <div class="nav"><a href="../contents">← Home</a></div>
            <h1>📊 Lambda CloudWatch Metrics (Last 30 Days)</h1>

            <div class="loading">
                <div class="spinner"></div>
                <p>Loading statistics data...</p>
            </div>

            <div id="content" style="display: none;">
                <div class="stats-grid" id="summary-stats"></div>

                <h2>📈 Path Usage - Last 7 Days (28 × 6-hour buckets)</h2>
                <div class="chart-container">
                    <canvas id="histogramChart"></canvas>
                </div>

                <h2>Functions by Invocation Count</h2>
                <table>
                    <thead>
                        <tr>
                            <th>Function Name</th>
                            <th>Invocations</th>
                            <th>Errors</th>
                            <th>Error Rate</th>
                            <th>Avg Duration (ms)</th>
                            <th>Max Duration (ms)</th>
                        </tr>
                    </thead>
                    <tbody id="functions-tbody"></tbody>
                </table>

                <h2>📍 Path Analysis (Top Endpoints)</h2>
                <table>
                    <thead>
                        <tr>
                            <th>Path</th>
                            <th>Requests</th>
                            <th>Percentage</th>
                        </tr>
                    </thead>
                    <tbody id="paths-tbody"></tbody>
                </table>

                <h2>🌍 IP Address Analysis with Geolocation</h2>
                <table>
                    <thead>
                        <tr>
                            <th>IP Address</th>
                            <th>Country</th>
                            <th>City</th>
                            <th>Requests</th>
                            <th>Top Path</th>
                            <th>User-Agent</th>
                        </tr>
                    </thead>
                    <tbody id="ips-tbody"></tbody>
                </table>

                <h2>📱 Top User-Agents (Browsers/Devices)</h2>
                <table>
                    <thead>
                        <tr>
                            <th>User-Agent</th>
                            <th>Requests</th>
                        </tr>
                    </thead>
                    <tbody id="uas-tbody"></tbody>
                </table>
            </div>
        </div>

        <script>
        async function loadStats() {
            try {
                const response = await fetch('./lambda-stats/data');
                if (!response.ok) throw new Error('Failed to load data');

                const data = await response.json();

                // Populate summary stats
                const summary = data.summary;
                document.getElementById('summary-stats').innerHTML = `
                    <div class="stat-box">
                        <div class="stat-value">${summary.total_invocations.toLocaleString()}</div>
                        <div class="stat-label">Total Invocations</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-value">${summary.total_errors.toLocaleString()}</div>
                        <div class="stat-label">Errors</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-value">${summary.total_throttles.toLocaleString()}</div>
                        <div class="stat-label">Throttles</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-value">${summary.error_rate.toFixed(2)}%</div>
                        <div class="stat-label">Error Rate</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-value">${Math.round(summary.avg_duration)}ms</div>
                        <div class="stat-label">Avg Duration</div>
                    </div>
                    <div class="stat-box">
                        <div class="stat-value">${Math.round(summary.max_duration)}ms</div>
                        <div class="stat-label">Max Duration</div>
                    </div>
                `;

                // Create histogram chart
                const ctx = document.getElementById('histogramChart').getContext('2d');
                new Chart(ctx, {
                    type: 'bar',
                    data: {
                        labels: data.histogram.labels,
                        datasets: data.histogram.datasets
                    },
                    options: {
                        responsive: true,
                        maintainAspectRatio: true,
                        aspectRatio: 3,
                        scales: {
                            x: {
                                stacked: true,
                                title: {
                                    display: true,
                                    text: 'Days Ago'
                                },
                                ticks: {
                                    maxRotation: 0,
                                    minRotation: 0,
                                    font: { size: 10 }
                                }
                            },
                            y: {
                                stacked: true,
                                beginAtZero: true,
                                title: {
                                    display: true,
                                    text: 'Requests'
                                }
                            }
                        },
                        plugins: {
                            legend: {
                                position: 'bottom',
                                labels: { boxWidth: 15, font: { size: 11 } }
                            },
                            title: {
                                display: true,
                                text: 'Request Volume by Path (Stacked)',
                                font: { size: 14 }
                            }
                        }
                    }
                });

                // Populate functions table
                document.getElementById('functions-tbody').innerHTML = data.functions.map(f => `
                    <tr>
                        <td><strong>${f.name}</strong></td>
                        <td>${f.invocations.toLocaleString()}</td>
                        <td>${f.errors.toLocaleString()}</td>
                        <td>${f.error_rate.toFixed(2)}%</td>
                        <td>${Math.round(f.avg_duration)}</td>
                        <td>${Math.round(f.max_duration)}</td>
                    </tr>
                `).join('');

                // Populate paths table
                document.getElementById('paths-tbody').innerHTML = data.paths.map(p => `
                    <tr>
                        <td><code>${p.path}</code></td>
                        <td>${p.count.toLocaleString()}</td>
                        <td>${p.percentage.toFixed(1)}%</td>
                    </tr>
                `).join('');

                // Populate IPs table
                document.getElementById('ips-tbody').innerHTML = data.ips.map(ip => {
                    const ua = ip.top_ua.length > 80 ? ip.top_ua.substring(0, 80) + '...' : ip.top_ua;
                    return `
                        <tr>
                            <td><code>${ip.ip}</code></td>
                            <td>${ip.country}</td>
                            <td>${ip.city}</td>
                            <td>${ip.count.toLocaleString()}</td>
                            <td><code>${ip.top_path || '(root)'}</code></td>
                            <td style="font-size: 0.85rem;">${ua}</td>
                        </tr>
                    `;
                }).join('');

                // Populate user agents table
                document.getElementById('uas-tbody').innerHTML = data.user_agents.map(ua => {
                    const uaDisplay = ua.user_agent.length > 120 ? ua.user_agent.substring(0, 120) + '...' : ua.user_agent;
                    return `
                        <tr>
                            <td style="font-size: 0.9rem; word-break: break-all;">${uaDisplay}</td>
                            <td>${ua.count.toLocaleString()}</td>
                        </tr>
                    `;
                }).join('');

                // Hide loading, show content
                document.querySelector('.loading').style.display = 'none';
                document.getElementById('content').style.display = 'block';
            } catch (error) {
                document.querySelector('.loading').innerHTML = `
                    <div class="error">
                        <strong>Error loading statistics:</strong><br>
                        ${error.message}
                    </div>
                `;
            }
        }

        // Load data when page loads
        loadStats();
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

    elif path == f'/{stage}/pi-fleet' or path == '/pi-fleet':
        # Pi Fleet Status Dashboard
        pis = get_pi_fleet_status()
        html += render_pi_fleet_page(pis)

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

