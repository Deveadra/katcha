import {GetCallerIdentityCommand, STSClient} from '@aws-sdk/client-sts';

export const verifyExpectedAwsIdentity = async ({
  expectedAccountId,
  region,
  client = null,
}) => {
  if (!/^\d{12}$/.test(String(expectedAccountId || ''))) {
    throw new Error('KATCHA_AWS_EXPECTED_ACCOUNT_ID must be an explicit 12-digit AWS account ID');
  }
  if (!region) {
    throw new Error('AWS region is required for identity verification');
  }

  const sts = client || new STSClient({region});
  const identity = await sts.send(new GetCallerIdentityCommand({}));
  const account = String(identity?.Account || '').trim();
  const arn = String(identity?.Arn || '').trim();

  if (!/^\d{12}$/.test(account)) {
    throw new Error('AWS STS did not return a valid 12-digit account ID');
  }
  if (account !== expectedAccountId) {
    throw new Error(
      `AWS account mismatch: expected ${expectedAccountId}, got ${account}`
      + (arn ? ` (caller ${arn})` : ''),
    );
  }

  return {account, arn};
};
