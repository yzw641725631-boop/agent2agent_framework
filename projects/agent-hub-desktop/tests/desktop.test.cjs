'use strict';

const assert = require('node:assert/strict');
const { test } = require('node:test');
const { preferences, changes, notificationContent } = require('../core/desktop-settings.cjs');
const { Controller } = require('../core/controller.cjs');

test('legacy vaults enable background and notifications without enabling login startup', () => {
  const settings = preferences({ version: 1, profiles: {} });
  assert.deepEqual(settings, { closeToTray: true, notifications: true,
    showNotificationDetails: false, startAtLogin: false });
  assert.equal(preferences({ desktop: { notifications: false } }).notifications, false);
  assert.equal(preferences({ desktop: { startAtLogin: 'true' } }).startAtLogin, false);
  assert.throws(() => changes({ executable: 'arbitrary.exe' }), { code: 'INVALID_DESKTOP_REQUEST' });
  assert.throws(() => changes({ startAtLogin: 'false' }), { code: 'INVALID_DESKTOP_REQUEST' });
});

test('notification preferences suppress pushes and protect work summaries until enabled', () => {
  const settings = preferences({});
  const data = { title: '项目有新进度', body: '内部项目的工作摘要' };
  assert.ok(!notificationContent(data, settings).body.includes(data.body));
  assert.equal(notificationContent(data, { ...settings, showNotificationDetails: true }).body, data.body);
  assert.equal(notificationContent(data, { ...settings, notifications: false }), null);
  assert.equal(notificationContent(data, { ...settings, notifications: false }, { test: true }).body, data.body);
});

test('verified root enrollment finishes login immediately while member enrollment keeps approval pending', async () => {
  for (const required of [false, true]) {
    const profile = { sessions: {}, bindings: [], events: [], cursors: {}, challengeId: 'test-challenge',
      enrollment: { enrollment_token: 'test-enrollment', device_status: 'pending' } };
    const vault = { data: { current: 'one', profiles: { one: profile } }, save: async () => {} };
    const controller = new Controller({ vault, directory: 'unused' });
    controller.connection = 'pending';
    controller.remote = { raw: async () => ({ verified: true, device_approval_required: required }) };
    let finished = false;
    controller.finishLogin = async () => { finished = true; controller.connection = 'active'; };
    const state = await controller.verifyEmail('12345678');
    assert.equal(finished, !required);
    assert.equal(state.connection, required ? 'pending' : 'active');
    assert.equal(profile.enrollment.email_verification_required, false);
  }
});

test('live and reconnect progress deduplicate, including this device audit events', async () => {
  const profile = { sessions: { platform: {} }, bindings: [], events: [], cursors: {},
    deviceId: 'this-device', user: { platform_role: 'member' } };
  const vault = { data: { current: 'one', profiles: { one: profile } }, save: async () => {},
    pending: Promise.resolve() };
  const controller = new Controller({ vault, directory: 'unused' });
  controller.connection = 'active';
  controller.projectSlot = async () => 'project-slot';
  const stop = new AbortController(), notices = [];
  let requests = 0;
  const progress = (sequence, device = 'other-device', kind = 'task.updated') => `id: ${sequence}\nevent: progress\ndata: ${JSON.stringify({
    project_id: 'project-one', device_id: device, summary: `progress-${sequence}`, status: 'checkpoint',
    kind,
  })}\n\n`;
  controller.remote = { close() {}, async request() {
    requests++;
    const data = requests === 1 ? progress(1) + 'event: ready\ndata: {}\n\n' +
      progress(2) + progress(2) + progress(3, 'this-device') +
      progress(4, 'this-device', 'code_audit.queued') + progress(4, 'this-device', 'code_audit.queued') :
      progress(5, 'this-device') + progress(6) + 'event: ready\ndata: {}\n\n';
    return { body: new ReadableStream({ start(output) {
      output.enqueue(new TextEncoder().encode(data)); output.close();
    } }) };
  } };
  controller.on('notification', message => {
    notices.push(message);
    if (notices.length === 3) stop.abort();
  });
  try {
    await controller.stream('project-one', AbortSignal.any([stop.signal, AbortSignal.timeout(4000)]));
    assert.deepEqual(notices.map(n => n.body), ['progress-2', 'progress-4', 'progress-6']);
    assert.equal(notices[1].title, '代码规范审计有新进度');
    assert.equal(profile.cursors['project-one'], '6');
    assert.equal(profile.events.length, 6);
  } finally { stop.abort(); await controller.close(); }
});
