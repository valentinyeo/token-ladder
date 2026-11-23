const test = require('node:test');
const assert = require('node:assert/strict');

const extension = import('./pi-lane-pins.mjs');

test('worker tags contain only an opaque brief directory and an explicit ticket id', async () => {
  const { workerTags } = await extension;
  const fixture = require('../tests/fixtures/gateway-worker.json');
  assert.deepEqual(workerTags(fixture.cwd, fixture.brief), fixture.tags);
  assert.deepEqual(workerTags(fixture.cwd, 'Send user@example.com to VSTA-999'), fixture.tags.slice(0, 2));
  assert.deepEqual(workerTags(fixture.cwd, 'Ticket user@example.com'), fixture.tags.slice(0, 2));
});

test('vercel payload is pinned to the token-pass gateway only', async () => {
  const { pinned, pinnedLanes } = await extension;
  const payload = { model: 'deepseek/deepseek-v4.1-flash', messages: [] };
  const got = pinned(payload, pinnedLanes());
  assert.deepEqual(got.providerOptions, { gateway: { only: ['token-pass'], tags: ['caller=plan-pick-worker'] } });
  assert.deepEqual(got.messages, []);
  assert.equal(payload.providerOptions, undefined);
  const judge = pinned({ model: 'zai/glm-5.3-flash' }, pinnedLanes());
  assert.deepEqual(judge.providerOptions, { gateway: { only: ['token-pass'], tags: ['caller=plan-pick-worker'] } });
});

test('openrouter payload keeps one provider and no fallbacks', async () => {
  const { pinned, pinnedLanes } = await extension;
  const lanes = JSON.parse(require('node:fs').readFileSync(`${__dirname}/../ci/lanes.json`, 'utf8'));
  const openrouter = lanes.find((lane) => lane.name === 'openrouter');
  const got = pinned({ model: 'deepseek/deepseek-v4.1-flash-1017' }, pinnedLanes());
  assert.deepEqual(got.provider, openrouter.provider);
  assert.deepEqual(openrouter.provider, { only: ['StreamLake'], allow_fallbacks: false });
});

test('unpinned models and non-object payloads pass through', async () => {
  const { pinned, pinnedLanes } = await extension;
  assert.equal(pinned({ model: 'deepseek-flash' }, pinnedLanes()), undefined);
  assert.equal(pinned({ model: 'deepseek-v4.1-flash' }, pinnedLanes()), undefined);
  assert.equal(pinned(null, pinnedLanes()), undefined);
  assert.equal(pinned('x', pinnedLanes()), undefined);
});

test('the extension reads the brief once and sends its safe tags on each request', async () => {
  const { default: register, workerTags } = await extension;
  const { mkdtempSync, writeFileSync, rmSync } = require('node:fs');
  const { join } = require('node:path');
  const cwd = process.cwd();
  const dir = mkdtempSync(join(require('node:os').tmpdir(), 'tagged-worker-'));
  const brief = require('../tests/fixtures/gateway-worker.json').brief;
  writeFileSync(join(dir, 'brief.txt'), brief);
  const handlers = {};
  try {
    process.chdir(dir);
    register({ on: (name, handler) => { handlers[name] = handler; } });
    for (let i = 0; i < 2; i++) {
      const payload = { model: 'deepseek/deepseek-v4.1-flash', providerOptions: { other: { setting: true } } };
      const got = handlers.before_provider_request({ payload });
      assert.deepEqual(got.providerOptions.gateway.tags, workerTags(dir, brief));
      assert.deepEqual(got.providerOptions.gateway.only, ['token-pass']);
      assert.deepEqual(got.providerOptions.other, { setting: true });
      assert.equal(payload.providerOptions.gateway, undefined);
    }
  } finally {
    process.chdir(cwd);
    rmSync(dir, { recursive: true });
  }
});

test('the extension registers the provider request hook', async () => {
  const { default: register, pinnedLanes } = await extension;
  const handlers = {};
  register({ on: (name, handler) => { handlers[name] = handler; } });
  const got = handlers.before_provider_request({ payload: { model: 'deepseek/deepseek-v4.1-flash' } });
  assert.deepEqual(got.providerOptions.gateway.only, ['token-pass']);
  assert.equal(got.providerOptions.gateway.tags[0], 'caller=plan-pick-worker');
  assert.match(got.providerOptions.gateway.tags[1], /^brief=[0-9a-f]{12}$/);
  assert.equal(handlers.before_provider_request({ payload: { model: 'gemini-2.5-flash' } }), undefined);
  assert.equal(pinnedLanes().length, 3);
});
