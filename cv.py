from pprint import pformat
import os
import base64
from io import BytesIO

try:
    import boto3
    BOTO3_AVAILABLE = True
except ImportError:
    BOTO3_AVAILABLE = False

GARDENCAM_BUCKET = "gardencam-berrylands"


def get_latest_gardencam_image():
    """Fetch the latest image from S3 (already brightness-adjusted on capture)."""
    if not BOTO3_AVAILABLE:
        return None, None
    s3 = boto3.client("s3")

    # List objects and find the most recent
    response = s3.list_objects_v2(Bucket=GARDENCAM_BUCKET)
    if "Contents" not in response:
        return None, None

    # Sort by LastModified, get newest
    objects = sorted(response["Contents"], key=lambda x: x["LastModified"], reverse=True)
    latest = objects[0]
    key = latest["Key"]
    timestamp = latest["LastModified"].strftime("%Y-%m-%d %H:%M:%S")

    # Download the image
    img_data = BytesIO()
    s3.download_fileobj(GARDENCAM_BUCKET, key, img_data)
    img_data.seek(0)

    # Encode as base64 JPEG
    img_base64 = base64.b64encode(img_data.read()).decode("utf-8")

    return img_base64, timestamp


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
        img_base64, timestamp = get_latest_gardencam_image()
        if img_base64:
            html += f'''
            <title>Garden Camera</title>
            <style>
                body {{ font-family: Arial, sans-serif; text-align: center; margin: 2rem; background: #1a1a1a; color: #fff; }}
                img {{ max-width: 100%; height: auto; border-radius: 8px; }}
                .timestamp {{ color: #888; margin-top: 1rem; }}
            </style>
            <h1>Garden Camera</h1>
            <img src="data:image/jpeg;base64,{img_base64}" alt="Garden Camera">
            <p class="timestamp">Captured: {timestamp} UTC</p>
            '''
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

