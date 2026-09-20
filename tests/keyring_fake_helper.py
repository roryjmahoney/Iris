"""Synthetic-only native PAM test helper, never installed."""
import os
import sys
import time
op, user = sys.argv[1:]
with open(__file__ + '.events', 'a') as events:
    events.write(op + ' ' + user + '\n')
assert sys.flags.isolated and sys.dont_write_bytecode
assert 'PYTHONPATH' not in os.environ
if user == 'timeout':
    os.fork()
    time.sleep(30)
elif user == 'oversized':
    sys.stdout.buffer.write(b'x' * 4097)
elif user == 'nul':
    sys.stdout.buffer.write(b'a\0b')
elif user == 'failure':
    sys.exit(1)
elif op == 'capture':
    assert sys.stdin.buffer.read() == b'synthetic-only'
elif op == 'unlock':
    sys.stdout.buffer.write(b'synthetic-only')
