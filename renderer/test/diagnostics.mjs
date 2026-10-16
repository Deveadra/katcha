import assert from 'node:assert/strict';
import {redactDiagnostic, sanitizedError} from '../src/diagnostics.mjs';

const source = 'https://media.example.test/trailer.mp4?X-Amz-Credential=PRIVATEKEY%2Fscope&X-Amz-Security-Token=PRIVATESESSION%2Bvalue&X-Amz-Signature=PRIVATESIGNATURE';
for (const value of [source, encodeURIComponent(source), encodeURIComponent(encodeURIComponent(source)), `http://localhost:3000/proxy?src=${encodeURIComponent(encodeURIComponent(source))}`, source.replaceAll('/', '\\/')]) {
  const diagnostic = redactDiagnostic(`decode failed: ${value} phase=decode render_id=render-123`);
  for (const secret of ['PRIVATEKEY', 'PRIVATESESSION', 'PRIVATESIGNATURE']) assert.ok(!diagnostic.includes(secret), diagnostic);
  assert.match(diagnostic, /phase=decode render_id=render-123/);
  assert.match(diagnostic, /(?:trailer.mp4|\/proxy)/);
  assert.equal(redactDiagnostic(diagnostic), diagnostic);
}
assert.equal(redactDiagnostic('frame 24 decode failed'), 'frame 24 decode failed');
assert.ok(!redactDiagnostic(`invalid %XX ${encodeURIComponent(source)}`).includes('PRIVATESESSION'));
assert.ok(!redactDiagnostic('X-Amz-Security-Token=PRIVATESESSION&phase=decode').includes('PRIVATESESSION'));
assert.equal(redactDiagnostic('https://user:password@media.example.test/trailer.mp4'), 'https://media.example.test/trailer.mp4');
const raw = new Error(source, {cause: {request: {url: source}}});
const safe = sanitizedError(raw);
assert.equal(safe.cause, undefined);
assert.ok(!safe.stack.includes('PRIVATESESSION'));
console.log('renderer diagnostic redaction passed');
