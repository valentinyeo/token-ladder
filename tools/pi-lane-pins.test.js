const test = require('node:test');
const assert = require('node:assert/strict');

const extension = import('./pi-lane-pins.mjs');

test('vercel payload is pinned to the token-pass gateway only', async () => {
  const { pinned, pinnedLanes } = await extension;
  const payload = { model: 'deepseek/deepseek-v4.1-flash', messages: [] };
  const got = pinned(payload, pinnedLanes());
  assert.deepEqual(got.providerOptions, { gateway: { only: ['token-pass'] } });
  assert.deepEqual(got.messages, []);
  assert.equal(payload.providerOptions, undefined);
  const judge = pinned({ model: 'zai/glm-5.3-flash' }, pinnedLanes());
  assert.deepEqual(judge.providerOptions, { gateway: { only: ['token-pass'] } });
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

test('the extension registers the provider request hook', async () => {
  const { default: register, pinnedLanes } = await extension;
  const handlers = {};
  register({ on: (name, handler) => { handlers[name] = handler; } });
  const got = handlers.before_provider_request({ payload: { model: 'deepseek/deepseek-v4.1-flash' } });
  assert.deepEqual(got.providerOptions, { gateway: { only: ['token-pass'] } });
  assert.equal(handlers.before_provider_request({ payload: { model: 'gemini-2.5-flash' } }), undefined);
  assert.equal(pinnedLanes().length, 3);
});
