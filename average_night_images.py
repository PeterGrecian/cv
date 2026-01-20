#!/usr/bin/env python3
"""
Lambda function to create averaged images from night captures.
Run on a schedule to process night images and create composites.
"""

import os
import boto3
from io import BytesIO
from datetime import datetime, timedelta
from PIL import Image
import numpy as np

GARDENCAM_BUCKET = "gardencam-berrylands-eu-west-1"
GARDENCAM_REGION = "eu-west-1"
DYNAMODB_TABLE = "gardencam-stats"


def get_image_mode(filename, assume_night_if_unknown=False):
    """Check DynamoDB to see if image was captured in night mode.

    Args:
        filename: The image filename to check
        assume_night_if_unknown: If True and no stats found, assume it's a night image
                                (useful when processing known nighttime hours)
    """
    try:
        dynamodb = boto3.resource('dynamodb', region_name=GARDENCAM_REGION)
        table = dynamodb.Table(DYNAMODB_TABLE)

        response = table.get_item(Key={'filename': filename})
        if 'Item' in response:
            mode = response['Item'].get('mode', 'unknown')
            if mode != 'unknown':
                return mode

        # If no stats available and we're processing nighttime hours, assume night
        # (autocontrast makes brightness-based detection unreliable)
        if assume_night_if_unknown:
            return 'night'

        return 'unknown'
    except Exception as e:
        print(f"  Error checking mode for {filename}: {e}")
        if assume_night_if_unknown:
            return 'night'
        return 'unknown'


def get_images_for_period(date_str, start_hour, end_hour):
    """Get all image keys for a specific date and hour range."""
    s3 = boto3.client('s3', region_name=GARDENCAM_REGION)

    # List all objects
    response = s3.list_objects_v2(Bucket=GARDENCAM_BUCKET)
    if 'Contents' not in response:
        return []

    images = []
    for obj in response['Contents']:
        key = obj['Key']
        # Parse filename: garden_YYYYMMDD_HHMMSS.jpg
        if key.startswith('garden_') and key.endswith('.jpg'):
            try:
                parts = key.replace('.jpg', '').split('_')
                if len(parts) >= 3:
                    file_date = parts[1]
                    file_time = parts[2]
                    file_hour = int(file_time[:2])

                    if file_date == date_str and start_hour <= file_hour < end_hour:
                        images.append(key)
            except:
                continue

    return sorted(images)


def download_image_as_array(s3_client, key):
    """Download an image from S3 and return as numpy array."""
    response = s3_client.get_object(Bucket=GARDENCAM_BUCKET, Key=key)
    img = Image.open(BytesIO(response['Body'].read()))
    return np.array(img, dtype=np.float32)


def create_averaged_image(image_keys):
    """Download images and create an averaged composite."""
    if not image_keys:
        return None

    print(f"Averaging {len(image_keys)} images...")

    s3_client = boto3.client('s3', region_name=GARDENCAM_REGION)

    # Download first image to get dimensions
    first_array = download_image_as_array(s3_client, image_keys[0])
    height, width, channels = first_array.shape

    # Accumulate pixel values
    accumulated = np.zeros((height, width, channels), dtype=np.float32)
    count = 0

    for key in image_keys:
        try:
            img_array = download_image_as_array(s3_client, key)
            accumulated += img_array
            count += 1
            print(f"  Processed {count}/{len(image_keys)}: {key}")
        except Exception as e:
            print(f"  Error processing {key}: {e}")
            continue

    if count == 0:
        return None

    # Calculate average
    averaged = (accumulated / count).astype(np.uint8)

    # Convert back to PIL Image
    result_img = Image.fromarray(averaged)

    print(f"Created averaged image from {count} source images")
    return result_img


def upload_averaged_image(img, date_str, start_hour, end_hour):
    """Upload the averaged image to S3."""
    # Create filename: averaged_YYYYMMDD_HH-HH.jpg
    # end_hour is exclusive, so use end_hour-1 for filename (e.g., 4-7 not 4-8)
    period_end = end_hour - 1
    filename = f"averaged_{date_str}_{start_hour:02d}-{period_end:02d}.jpg"

    # Convert to bytes
    buffer = BytesIO()
    img.save(buffer, format='JPEG', quality=90, optimize=True)
    buffer.seek(0)

    # Upload to S3
    s3_client = boto3.client('s3', region_name=GARDENCAM_REGION)
    s3_client.upload_fileobj(
        buffer,
        GARDENCAM_BUCKET,
        filename,
        ExtraArgs={'ContentType': 'image/jpeg'}
    )

    print(f"Uploaded averaged image: s3://{GARDENCAM_BUCKET}/{filename}")

    # Also create a medium-sized version (1200px wide)
    width, height = img.size
    if width > 1200:
        ratio = 1200 / width
        new_height = int(height * ratio)
        medium_img = img.resize((1200, new_height), Image.Resampling.LANCZOS)
    else:
        medium_img = img

    # Upload medium version
    medium_filename = f"averaged_medium_{date_str}_{start_hour:02d}-{period_end:02d}.jpg"
    buffer_medium = BytesIO()
    medium_img.save(buffer_medium, format='JPEG', quality=85, optimize=True)
    buffer_medium.seek(0)

    s3_client.upload_fileobj(
        buffer_medium,
        GARDENCAM_BUCKET,
        medium_filename,
        ExtraArgs={'ContentType': 'image/jpeg'}
    )

    print(f"Uploaded medium version: s3://{GARDENCAM_BUCKET}/{medium_filename}")

    return filename, medium_filename


def lambda_handler(event, context):
    """
    Lambda handler to create averaged night images.

    Event parameters:
    - date: Date string in YYYYMMDD format (default: yesterday)
    - start_hour: Start hour (default: 4)
    - end_hour: End hour (default: 7)
    """
    # Get parameters from event or use defaults
    date_str = event.get('date')
    if not date_str:
        # Default to yesterday
        yesterday = datetime.now() - timedelta(days=1)
        date_str = yesterday.strftime('%Y%m%d')

    start_hour = event.get('start_hour', 4)
    end_hour = event.get('end_hour', 7)

    print(f"Processing night images for {date_str}, hours {start_hour}-{end_hour}")

    # Get images for the period
    image_keys = get_images_for_period(date_str, start_hour, end_hour)

    if not image_keys:
        print(f"No images found for period {date_str} {start_hour:02d}:00-{end_hour:02d}:00")
        return {
            'statusCode': 200,
            'body': f'No images found for period'
        }

    print(f"Found {len(image_keys)} images")

    # Filter to only night mode images
    # Since we're processing designated nighttime hours (4-7am), assume night if no stats
    night_images = []
    for key in image_keys:
        mode = get_image_mode(key, assume_night_if_unknown=True)
        if mode == 'night':
            night_images.append(key)
            print(f"  {key}: mode={mode} ✓")
        else:
            print(f"  {key}: mode={mode} (skipping - daytime capture)")

    if not night_images:
        print(f"No night mode images found in period")
        return {
            'statusCode': 200,
            'body': f'No night mode images found for period'
        }

    print(f"Filtered to {len(night_images)} night mode images")

    # Create averaged image
    averaged_img = create_averaged_image(night_images)

    if averaged_img is None:
        return {
            'statusCode': 500,
            'body': 'Failed to create averaged image'
        }

    # Upload to S3
    full_filename, medium_filename = upload_averaged_image(averaged_img, date_str, start_hour, end_hour)

    return {
        'statusCode': 200,
        'body': f'Created averaged image from {len(night_images)} night mode images: {full_filename}'
    }


if __name__ == "__main__":
    # Test locally
    event = {
        'date': '20260120',
        'start_hour': 4,
        'end_hour': 7
    }

    class Context:
        pass

    result = lambda_handler(event, Context())
    print(result)
