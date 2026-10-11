#!/bin/sh
# Creates a tiny Node repo (in the current directory) with a token-expiry bug:
# the clock-skew grace is subtracted instead of added, so tokens expire 60s early.
set -eu
mkdir -p src test
cat > package.json <<'J'
{ "name": "token-bug", "version": "1.0.0", "type": "module", "scripts": { "test": "node --test" } }
J
cat > src/token.js <<'J'
export const GRACE_SECONDS = 60; // clock-skew allowance

export function isExpired(payload, nowSeconds = Math.floor(Date.now() / 1000)) {
  if (!payload || typeof payload.exp !== 'number') return true;
  return payload.exp - GRACE_SECONDS < nowSeconds;
}
J
cat > src/requireAuth.js <<'J'
import { isExpired } from './token.js';

export function requireAuth(payload) {
  if (isExpired(payload)) return { status: 401 };
  return { status: 200 };
}
J
cat > src/sessionCleanup.js <<'J'
import { isExpired } from './token.js';

export function cleanup(sessions, now) {
  return sessions.filter((s) => !isExpired(s, now));
}
J
cat > test/token.test.js <<'J'
import test from 'node:test';
import assert from 'node:assert';
import { isExpired } from '../src/token.js';

test('missing exp is expired', () => assert.equal(isExpired({}), true));
test('long-past exp is expired', () => assert.equal(isExpired({ exp: 1000 }, 5000), true));
J
cat > README.md <<'J'
# token-bug
Users report being logged out about a minute before their token's `exp`.
J
git init -q
git -c user.email=fixture@example.com -c user.name=fixture add -A
git -c user.email=fixture@example.com -c user.name=fixture commit -qm "fixture"
