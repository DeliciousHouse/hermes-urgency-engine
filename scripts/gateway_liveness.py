"""One-shot, local-only API Server health check."""
import asyncio
import json
import re
import sys

TIMEOUT_SECONDS = 3
MAX_HEADER_BYTES = 8192
MAX_BODY_BYTES = 4096


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('duplicate key')
        result[key] = value
    return result


async def probe(port):
    reader, writer = await asyncio.open_connection('127.0.0.1', port, limit=MAX_HEADER_BYTES)
    try:
        writer.write(b'GET /health HTTP/1.1\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n')
        await writer.drain()
        header = await reader.readuntil(b'\r\n\r\n')
        if len(header) > MAX_HEADER_BYTES:
            return False
        lines = header[:-4].split(b'\r\n')
        if not re.fullmatch(rb'HTTP/1\.[01] 200(?: [\x20-\x7e]*)?', lines[0]):
            return False
        lengths = []
        for line in lines[1:]:
            if not re.fullmatch(rb"[!#$%&'*+.^_`|~0-9A-Za-z-]+:[\t\x20-\x7e]*", line):
                return False
            name, value = line.split(b':', 1)
            name, value = name.lower(), value.strip()
            if name == b'transfer-encoding':
                return False
            if name == b'content-length':
                lengths.append(value)
        if len(lengths) != 1 or not re.fullmatch(rb'[0-9]{1,5}', lengths[0]):
            return False
        length = int(lengths[0])
        if length > MAX_BODY_BYTES:
            return False
        body = await reader.readexactly(length)
        data = json.loads(body.decode('utf-8'), object_pairs_hook=unique_object)
        return data == {'status': 'ok'} or (
            isinstance(data, dict)
            and set(data) == {'status', 'platform', 'version'}
            and data['status'] == 'ok' and data['platform'] == 'hermes-agent'
            and isinstance(data['version'], str) and bool(data['version'])
        )
    finally:
        writer.close()


def main():
    args = sys.argv[1:]
    if (len(args) != 1 or not re.fullmatch(r'[0-9]+', args[0])
            or len(args[0].lstrip('0')) > 5
            or not 1 <= int(args[0].lstrip('0') or '0') <= 65535):
        print('Usage: gateway_liveness.py PORT', file=sys.stderr)
        return 2
    try:
        healthy = asyncio.run(asyncio.wait_for(probe(int(args[0].lstrip('0'))), TIMEOUT_SECONDS))
    except (ValueError, OSError, asyncio.TimeoutError, asyncio.IncompleteReadError,
            asyncio.LimitOverrunError, RecursionError):
        healthy = False
    if not healthy:
        print('Gateway health check failed.', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
