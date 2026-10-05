"""Preload the small local chat model without generating a reply."""
import json
import time
import urllib.request
import argparse

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument('--model', default='nessa-chat:latest')
parser.add_argument('--url', default='http://127.0.0.1:11436')
args = parser.parse_args()

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
request = urllib.request.Request(args.url + '/api/generate',
    data=json.dumps({'model':args.model, 'keep_alive':-1, 'stream':False}).encode(),
    headers={'Content-Type':'application/json'})
for attempt in range(20):
    try:
        with opener.open(request, timeout=120) as response:
            result = json.load(response)
            if not result.get('done'):
                raise RuntimeError('Warm-up did not complete')
        print(f'{args.model} is loaded locally.')
        break
    except OSError:
        if attempt == 19:
            raise
        time.sleep(.5)
