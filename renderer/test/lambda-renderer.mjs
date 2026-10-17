import assert from 'node:assert/strict';
import {
  assertCloudReachableInputProps,
  renderMediaViaLambda,
  resolveLambdaFramesPerFunction,
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
  fps: 30,
  source: {url: 'https://cdn.example.test/source.mp4'},
};

const partitionCases = [
  // 4746 frames previously spawned 198 workers and exceeded AWS capacity.
  {seconds: 158.181, fps: 30, configuredFrames: 20, expected: 1187},
  {seconds: 180, fps: 30, configuredFrames: 20, expected: 1350},
  {seconds: 1800, fps: 30, configuredFrames: 20, expected: 13500},
  {seconds: 180, fps: 60, configuredFrames: 20, expected: 2700},
  {seconds: 30, fps: 30, configuredFrames: 20, expected: 225},
  {seconds: 158.181, fps: 30, configuredFrames: 2000, expected: 2000},
  {seconds: 1, fps: 30, configuredFrames: 4, expected: 8},
  {seconds: 0.05, fps: 30, configuredFrames: 4, expected: 4},
  {seconds: 158.181, fps: 30, configuredFrames: 20, maxWorkerFunctions: 1, expected: 4746},
  {seconds: 158.181, fps: 30, configuredFrames: 20, maxWorkerFunctions: 10, expected: 475},
  {seconds: 158.181, fps: 30, configuredFrames: 20, maxWorkerFunctions: 200, expected: 24},
];
for (const {seconds, fps, configuredFrames, maxWorkerFunctions = 4, expected} of partitionCases) {
  const effective = resolveLambdaFramesPerFunction({
    inputProps: {output_duration_seconds: seconds, fps}, configuredFrames, maxWorkerFunctions,
  });
  assert.equal(effective, expected);
  assert.ok(effective >= configuredFrames);
  assert.ok(Math.ceil(Math.ceil(seconds * fps) / effective) <= maxWorkerFunctions);
}

for (const cap of [1, 2, 4, 10, 200]) {
  const effective = resolveLambdaFramesPerFunction({
    inputProps: {...props, output_duration_seconds: 158.181},
    configuredFrames: 20, maxWorkerFunctions: cap,
  });
  assert.ok(Math.ceil(4746 / effective) <= cap);
}
for (const cap of [0, -1, 201, 1.5, NaN, Infinity]) {
  let budgetCalled = false;
  await assert.rejects(renderMediaViaLambda({
    compositionId: 'BlueprintVideo', inputProps: props,
    outputPath: '/tmp/invalid-fanout.mp4',
    renderSettings: {...settings, lambda: {...settings.lambda, maxWorkerFunctions: cap}},
    dependencies: {fetch: async () => {budgetCalled = true;}},
  }), /max worker functions/);
  assert.equal(budgetCalled, false);
}

// Verify the actual SDK launch receives the partition, while budget reservations
// and retry/concurrency limits retain their existing values. No AWS calls occur.
for (const {seconds, fps, configuredFrames, maxWorkerFunctions = 4, expected} of partitionCases) {
  const requests = [];
  await renderMediaViaLambda({
    compositionId: seconds >= 1800 ? 'Longform' : 'BlueprintVideo',
    inputProps: {...props, output_duration_seconds: seconds, fps},
    outputPath: '/tmp/partition-fixture.mp4',
    renderSettings: {...settings, lambda: {...settings.lambda, framesPerLambda: configuredFrames, maxWorkerFunctions}},
    dependencies: {
      fetch: async (url, options) => {
        const body = JSON.parse(options.body);
        requests.push({url, body});
        return {ok: true, status: 200, json: async () => ({reservation: {
          id: 'partition-fixture', status: url.endsWith('/reserve') ? 'reserved' : 'settled',
          estimated_cost_microusd: 500000,
        }})};
      },
      renderMediaOnLambda: async (options) => {
        assert.equal(options.framesPerLambda, expected);
        assert.equal(options.maxRetries, settings.lambda.maxRetries);
        assert.equal(options.concurrencyPerLambda, settings.lambda.concurrencyPerLambda);
        assert.equal(Object.hasOwn(options, 'concurrency'), false);
        return {renderId: 'partition-fixture', bucketName: 'fixture'};
      },
      getRenderProgress: async () => ({done: true, overallProgress: 1}),
      downloadMedia: async () => ({sizeInBytes: 10}),
    },
  });
  assert.equal(requests.length, 2);
  assert.equal(requests[0].body.estimated_cost_microusd, 500000);
  assert.equal(requests[1].body.actual_cost_microusd, 500000);
}

for (const configuredFrames of [0, -1, 1.5, NaN, Infinity, Number.MAX_SAFE_INTEGER + 1]) {
  assert.throws(() => resolveLambdaFramesPerFunction({
    inputProps: props, configuredFrames,
  }), /positive safe integer/);
}

for (const invalidProps of [
  {...props, fps: undefined}, {...props, fps: 0}, {...props, fps: Infinity},
  {...props, output_duration_seconds: -1}, {...props, output_duration_seconds: NaN},
  {...props, output_duration_seconds: Number.MAX_SAFE_INTEGER},
]) {
  let budgetCalled = false;
  await assert.rejects(renderMediaViaLambda({
    compositionId: 'BlueprintVideo', inputProps: invalidProps,
    outputPath: '/tmp/invalid-partition.mp4', renderSettings: settings,
    dependencies: {fetch: async () => {budgetCalled = true;}},
  }), /positive duration and frame rate/);
  assert.equal(budgetCalled, false);
}

{
  const order = [];
  const fetch = async (url, options) => {
    const body = JSON.parse(options.body);
    if (url.endsWith('/reserve')) {
      order.push('reserve');
      assert.equal(body.provider, 'aws-lambda');
      assert.equal(Object.hasOwn(body, 'attempt'), false);
      assert.equal(body.settle_on_expiry, true);
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

// Fatal provider diagnostics must not persist presigned credentials into either
// the returned Error or the budget settlement receipt.
{
  const secretUrl = 'https://media.example.test/source.mp4?X-Amz-Security-Token=PRIVATESESSION&X-Amz-Signature=PRIVATESIGNATURE';
  let settlement;
  await assert.rejects(renderMediaViaLambda({
    compositionId: 'Short', inputProps: props, outputPath: '/tmp/out.mp4', renderSettings: settings,
    dependencies: {
      fetch: async (url, options) => {
        if (url.endsWith('/reserve')) return {ok: true, json: async () => ({reservation: {id: 'r-redacted', status: 'reserved'}})};
        settlement = JSON.parse(options.body);
        return {ok: true, json: async () => ({reservation: {status: 'settled'}})};
      },
      renderMediaOnLambda: async () => ({renderId: 'render-redacted', bucketName: 'bucket', cloudWatchLogs: 'logs'}),
      getRenderProgress: async () => ({fatalErrorEncountered: true, errors: [{message: `decode failed ${encodeURIComponent(encodeURIComponent(secretUrl))}`}]}),
      downloadMedia: async () => { throw new Error('must not download after fatal error'); },
    },
  }), (error) => {
    assert.match(error.message, /decode failed.*media.example.test\/source.mp4/);
    assert.match(error.message, /render_id=render-redacted/);
    assert.ok(!error.stack.includes('PRIVATESESSION'));
    assert.ok(!error.stack.includes('PRIVATESIGNATURE'));
    return true;
  });
  assert.ok(!JSON.stringify(settlement).includes('PRIVATESESSION'));
  assert.ok(!JSON.stringify(settlement).includes('PRIVATESIGNATURE'));
}
