"""Preload the small local chat model without generating a reply."""
import json
import time
import urllib.request

opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
request = urllib.request.Request('http://127.0.0.1:11436/api/generate',
    data=json.dumps({'model':'nessa-chat:latest', 'keep_alive':-1, 'stream':False}).encode(),
    headers={'Content-Type':'application/json'})
for attempt in range(20):
    try:
        with opener.open(request, timeout=120) as response:
            result = json.load(response)
            if not result.get('done'):
                raise RuntimeError('Warm-up did not complete')
        print('Nessa fast chat model is loaded.')
        break
    except OSError:
        if attempt == 19:
            raise
        time.sleep(.5)
