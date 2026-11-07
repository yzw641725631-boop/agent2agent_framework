'use strict';

const { HubError } = require('./remote.cjs');
const defaults = Object.freeze({ closeToTray: true, notifications: true,
  showNotificationDetails: false, startAtLogin: false });

function preferences(data) {
  const result = { ...defaults };
  for (const key of Object.keys(defaults)) {
    if (typeof data.desktop?.[key] === 'boolean') result[key] = data.desktop[key];
  }
  return result;
}

function changes(patch) {
  if (!patch || typeof patch !== 'object' || Array.isArray(patch) ||
      Object.entries(patch).some(([key, value]) => !Object.hasOwn(defaults, key) || typeof value !== 'boolean')) {
    throw new HubError('INVALID_DESKTOP_REQUEST', '桌面设置格式无效。');
  }
  return patch;
}

function notificationContent(data, settings, { test = false } = {}) {
  if (!test && !settings.notifications) return null;
  return {
    title: typeof data.title === 'string' ? data.title.slice(0, 100) : 'Agent Hub',
    body: test || settings.showNotificationDetails ? String(data.body || '').slice(0, 240) :
      '项目动态或连接状态有变化，点击打开 Agent Hub 查看。',
  };
}

module.exports = { preferences, changes, notificationContent };
