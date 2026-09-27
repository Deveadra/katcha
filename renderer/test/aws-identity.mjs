import assert from 'node:assert/strict';
import {verifyExpectedAwsIdentity} from '../src/aws-identity.mjs';

const calls = [];
const goodClient = {
  async send(command) {
    calls.push(command.constructor.name);
    return {
      Account: '123456789012',
      Arn: 'arn:aws:sts::123456789012:assumed-role/katcha-renderer/session',
    };
  },
};

const verified = await verifyExpectedAwsIdentity({
  expectedAccountId: '123456789012',
  region: 'us-east-1',
  client: goodClient,
});
assert.deepEqual(verified, {
  account: '123456789012',
  arn: 'arn:aws:sts::123456789012:assumed-role/katcha-renderer/session',
});
assert.deepEqual(calls, ['GetCallerIdentityCommand']);

await assert.rejects(
  () => verifyExpectedAwsIdentity({
    expectedAccountId: '999999999999',
    region: 'us-east-1',
    client: goodClient,
  }),
  /AWS account mismatch/,
);

await assert.rejects(
  () => verifyExpectedAwsIdentity({
    expectedAccountId: '',
    region: 'us-east-1',
    client: goodClient,
  }),
  /explicit 12-digit AWS account ID/,
);

console.log('PASS: Lambda runtime fails closed on AWS account identity mismatch.');
