from pprint import pformat
import os

def lambda_handler(event, context):
    html = ""
    favicon=open("favicon.png64", "r").read()
    fav = f'<title>Peter Grecian</title><link rel="icon" type="image/png" href="data:image/png;base64,{favicon}">'
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
        html = open("gitinfo.html", "r").read()
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
            html += pformat(event[key]).replace(',', ',<br>')   + "<br><br>"            
    elif path == f'/{stage}/contents' or path == '/contents':
        html += open('contents.html', 'r').read()
    else:
        html += open('cv.html', 'r').read()
    content = f'<html><head>{fav}</head><body>{html}</body></html>'

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

