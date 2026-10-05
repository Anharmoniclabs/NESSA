"""Preload the small local chat model without generating a reply."""
import argparse
import json
import time
import urllib.request

ATTEMPTS = 20
RETRY_SECONDS = .5
LOAD_TIMEOUT = 120


def warm(url: str, model: str, attempts: int = ATTEMPTS, retry_seconds: float = RETRY_SECONDS) -> None:
    """Ask the server to load `model` and keep it resident. Retries while the server is starting."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    request = urllib.request.Request(
        url + '/api/generate', headers={'Content-Type': 'application/json'},
        data=json.dumps({'model': model, 'keep_alive': -1, 'stream': False}).encode())
    for attempt in range(1, attempts + 1):
        try:
            with opener.open(request, timeout=LOAD_TIMEOUT) as response:
                result = json.load(response)
        except OSError:
            if attempt == attempts:
                raise
            time.sleep(retry_seconds)
            continue
        if not result.get('done'):
            raise RuntimeError(f'{model} did not finish loading')
        return


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--model', default='nessa-chat:latest')
    parser.add_argument('--url', default='http://127.0.0.1:11436')
    args = parser.parse_args()
    warm(args.url, args.model)
    print(f'{args.model} is loaded locally.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
