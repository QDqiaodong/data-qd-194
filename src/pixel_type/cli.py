import argparse
import hashlib
import json
import sys

from .io import load, parse_json as load_from_bytes, publish, publish_bytes
from .reconcile import build_report
from .service import process
from .store import Store, StoreError, canonical_bytes


def _fail(message):
    print(f'error: {message}', file=sys.stderr)
    return 2


def _run_type(args):
    try:
        result = process(load(args.source))
        publish(args.output, result)
    except (ValueError, OSError, UnicodeError, RecursionError) as e:
        return _fail(e)
    print(json.dumps(result['summary'], ensure_ascii=False, sort_keys=True))
    return 0


def _run_seal(args):
    store = Store(args.store)
    try:
        # A refetch of an already-frozen identity never needs the source file,
        # and must not depend on its current bytes or continued existence.
        try:
            _plan, payload = store.load(args.release_id, args.request_sha256)
            sys.stdout.buffer.write(payload)
            return 0
        except StoreError as error:
            if 'no frozen release' not in str(error):
                raise
        with open(args.source, 'rb') as handle:
            raw = handle.read()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != args.request_sha256:
            return _fail('request-sha256 does not match the source file bytes')
        # Strict parse: duplicate JSON keys and NaN/Infinity are rejected.
        source = load_from_bytes(raw)
        payload = store.seal(args.release_id, args.request_sha256, source)
    except (ValueError, OSError, UnicodeError, json.JSONDecodeError, StoreError) as e:
        return _fail(e)
    # Exactly the frozen bytes, so stdout is byte-identical across retries.
    sys.stdout.buffer.write(payload)
    return 0


def _run_reconcile(args):
    try:
        store = Store(args.store)
        plan, _raw = store.load(args.release_id, args.request_sha256)
        readback = load(args.readback)
        report = build_report(plan, readback)
        publish_bytes(args.output, canonical_bytes(report))
    except (ValueError, OSError, UnicodeError, RecursionError, StoreError) as e:
        return _fail(e)
    print(json.dumps(report['status_counts'], ensure_ascii=False, sort_keys=True))
    return 0


def main(argv=None):
    if argv is None:
        argv = sys.argv[1:]
    # Manual dispatch keeps the legacy v1 invocation
    # (pixel-type --source ... --output ...) working unchanged.
    if argv and argv[0] == 'seal':
        parser = argparse.ArgumentParser(prog='pixel-type seal')
        parser.add_argument('--store', required=True)
        parser.add_argument('--release-id', required=True)
        parser.add_argument('--request-sha256', required=True)
        parser.add_argument('--source', required=True)
        return _run_seal(parser.parse_args(argv[1:]))
    if argv and argv[0] == 'reconcile':
        parser = argparse.ArgumentParser(prog='pixel-type reconcile')
        parser.add_argument('--store', required=True)
        parser.add_argument('--release-id', required=True)
        parser.add_argument('--request-sha256', required=True)
        parser.add_argument('--readback', required=True)
        parser.add_argument('--output', required=True)
        return _run_reconcile(parser.parse_args(argv[1:]))
    parser = argparse.ArgumentParser(prog='pixel-type')
    parser.add_argument('--source', required=True)
    parser.add_argument('--output', required=True)
    return _run_type(parser.parse_args(argv))


if __name__ == '__main__':
    raise SystemExit(main())
