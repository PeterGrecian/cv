from pprint import pformat
import os
import base64
from io import BytesIO

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


def get_latest_gardencam_images(count=3):
    """Get presigned URLs for the latest N images from S3."""
    if not BOTO3_AVAILABLE:
        return []
    s3 = boto3.client("s3", region_name=GARDENCAM_REGION)

    # List objects and find the most recent
    response = s3.list_objects_v2(Bucket=GARDENCAM_BUCKET)
    if "Contents" not in response:
        return []

    # Sort by LastModified, get newest N
    objects = sorted(response["Contents"], key=lambda x: x["LastModified"], reverse=True)
    images = []

    for obj in objects[:count]:
        key = obj["Key"]
        timestamp = obj["LastModified"].strftime("%Y-%m-%d %H:%M:%S")

        # Generate presigned URL (expires in 1 hour)
        url = s3.generate_presigned_url(
            'get_object',
            Params={'Bucket': GARDENCAM_BUCKET, 'Key': key},
            ExpiresIn=3600
        )

        images.append({
            'url': url,
            'timestamp': timestamp,
            'key': key
        })

    return images


def lambda_handler(event, context):
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
                h1 { margin-bottom: 1.5rem; }
                .gallery { display: flex; gap: 1rem; justify-content: center; flex-wrap: wrap; max-width: 1024px; margin: 0 auto; }
                .image-container { flex: 1; min-width: 280px; max-width: 340px; }
                .image-container img { width: 100%; height: auto; border-radius: 8px; box-shadow: 0 4px 6px rgba(0,0,0,0.3); }
                .timestamp { color: #888; margin-top: 0.5rem; font-size: 0.9rem; }
                .label { color: #aaa; font-weight: bold; margin-bottom: 0.5rem; }
                @media (max-width: 768px) {
                    .image-container { max-width: 100%; }
                }
            </style>
            <h1>Garden Camera</h1>
            <div class="gallery">
            '''
            labels = ['Latest', 'Previous', 'Earlier']
            for idx, img in enumerate(images):
                label = labels[idx] if idx < len(labels) else f'Image {idx+1}'
                html += f'''
                <div class="image-container">
                    <div class="label">{label}</div>
                    <img src="{img['url']}" alt="{label} capture">
                    <p class="timestamp">{img['timestamp']} UTC</p>
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

