import assert from 'node:assert/strict';
import {
  assertCloudReachableInputProps,
  renderMediaViaLambda,
} from '../src/lambda-renderer.mjs';

assert.doesNotThrow(() => assertCloudReachableInputProps({
  source: {url: 'https://cdn.example.test/source.mp4'},
}));

for (const url of [
  'http://minio:9000/source.mp4',
  'http://127.0.0.1:9000/source.mp4',
  'https://192.168.1.10/source.mp4',
]) {
  assert.throws(
    () => assertCloudReachableInputProps({source: {url}}),
    /Lambda (media URL must use HTTPS|cannot reach local\/private media host)/,
  );
}

const settings = {
  timeoutInMilliseconds: 120000,
  externalCompute: {
    enabled: true,
    coordinatorUrl: 'https://budget.example.test',
    token: 'x'.repeat(40),
    maxRenderCostUsd: 0.5,
    reservationTtlSeconds: 7200,
  },
  lambda: {
    region: 'us-east-1',
    functionName: 'render-test',
    serveUrl: 'https://render.example.test/site',
    maxRetries: 2,
    framesPerLambda: 20,
    concurrencyPerLambda: 1,
    maxWaitMs: 60000,
    pollIntervalMs: 1,
  },
};
const props = {
  output_key: 'renders/example.mp4',
  output_duration_seconds: 30,
  source: {url: 'https://cdn.example.test/source.mp4'},
};

{
  const order = [];
  const fetch = async (url, options) => {
    const body = JSON.parse(options.body);
    if (url.endsWith('/reserve')) {
      order.push('reserve');
      assert.equal(body.provider, 'aws-lambda');
      assert.equal(Object.hasOwn(body, 'attempt'), false);
      return {
        ok: true,
        status: 201,
        json: async () => ({
          reservation: {
            id: 'r1',
            status: 'reserved',
            attempt: 1,
            estimated_cost_microusd: 500000,
          },
        }),
      };
    }
    order.push('settle');
    return {
      ok: true,
      status: 200,
      json: async () => ({reservation: {id: 'r1', status: 'settled'}}),
    };
  };

  const result = await renderMediaViaLambda({
    compositionId: 'Short',
    inputProps: props,
    outputPath: '/tmp/out.mp4',
    renderSettings: settings,
    dependencies: {
      fetch,
      randomUUID: () => 'u1',
      renderMediaOnLambda: async () => {
        order.push('aws');
        return {renderId: 'render-1', bucketName: 'bucket', cloudWatchLogs: 'logs'};
      },
      getRenderProgress: async () => {
        order.push('progress');
        return {done: true, overallProgress: 1, lambdasInvoked: 2, outputSizeInBytes: 12};
      },
      downloadMedia: async () => {
        order.push('download');
        return {sizeInBytes: 12};
      },
    },
  });
  assert.deepEqual(order, ['reserve', 'aws', 'progress', 'download', 'settle']);
  assert.equal(result.budgetSettlementPending, false);
  assert.equal(result.budgetReservedMicrousd, 500000);
}

{
  let awsCalled = false;
  await assert.rejects(
    renderMediaViaLambda({
      compositionId: 'Short',
      inputProps: props,
      outputPath: '/tmp/out.mp4',
      renderSettings: settings,
      dependencies: {
        fetch: async () => ({
          ok: false,
          status: 402,
          statusText: 'Denied',
          json: async () => ({error: 'monthly budget exceeded'}),
        }),
        renderMediaOnLambda: async () => {
          awsCalled = true;
        },
        getRenderProgress: async () => ({}),
        downloadMedia: async () => ({}),
      },
    }),
    /monthly budget exceeded/,
  );
  assert.equal(awsCalled, false);
}

{
  let calls = 0;
  const result = await renderMediaViaLambda({
    compositionId: 'Short',
    inputProps: props,
    outputPath: '/tmp/out.mp4',
    renderSettings: settings,
    dependencies: {
      fetch: async (url) => {
        calls += 1;
        if (url.endsWith('/reserve')) {
          return {
            ok: true,
            status: 201,
            json: async () => ({
              reservation: {
                id: 'r-held',
                status: 'reserved',
                attempt: 2,
                estimated_cost_microusd: 500000,
              },
            }),
          };
        }
        return {
          ok: false,
          status: 503,
          statusText: 'Unavailable',
          json: async () => ({error: 'temporarily unavailable'}),
        };
      },
      renderMediaOnLambda: async () => ({
        renderId: 'render-held',
        bucketName: 'bucket',
        cloudWatchLogs: 'logs',
      }),
      getRenderProgress: async () => ({
        done: true,
        overallProgress: 1,
        lambdasInvoked: 1,
        outputSizeInBytes: 10,
      }),
      downloadMedia: async () => ({sizeInBytes: 10}),
    },
  });
  assert.equal(calls, 2);
  assert.equal(result.budgetSettlementPending, true);
}

{
  const order = [];
  await assert.rejects(
    renderMediaViaLambda({
      compositionId: 'Short',
      inputProps: props,
      outputPath: '/tmp/out.mp4',
      renderSettings: settings,
      dependencies: {
        fetch: async (url) => {
          if (url.endsWith('/reserve')) {
            order.push('reserve');
            return {
              ok: true,
              status: 201,
              json: async () => ({
                reservation: {
                  id: 'r-failed',
                  status: 'reserved',
                  attempt: 3,
                  estimated_cost_microusd: 500000,
                },
              }),
            };
          }
          order.push('settle');
          return {
            ok: true,
            status: 200,
            json: async () => ({reservation: {status: 'settled'}}),
          };
        },
        renderMediaOnLambda: async () => {
          order.push('aws');
          throw new Error('launch outcome uncertain');
        },
        getRenderProgress: async () => ({}),
        downloadMedia: async () => ({}),
      },
    }),
    /launch outcome uncertain/,
  );
  assert.deepEqual(order, ['reserve', 'aws', 'settle']);
}

console.log('PASS: Lambda budget authorization precedes AWS invocation.');
