const DEFAULT_BASE_TOPIC = 'jj/notebook1/macro_pad';

const defaultSettings = {
  mqttUrl: 'wss://broker.emqx.io:8084/mqtt',
  clientId: `phone-macro-pad-${Math.random().toString(16).slice(2, 8)}`,
  username: '',
  password: '',
  baseTopic: DEFAULT_BASE_TOPIC
};

function readUrlSettings() {
  const params = new URLSearchParams(window.location.search);
  return {
    baseTopic: params.get('topic')?.trim() || '',
    reset: params.get('reset') === '1'
  };
}

let settings = JSON.parse(localStorage.getItem('macroPadMqtt') || JSON.stringify(defaultSettings));
let layout = JSON.parse(localStorage.getItem('macroPadLayout') || 'null');
let client = null;
let pageIndex = 0;
let deferredInstallPrompt = null;
let fullscreenAttemptArmed = false;
let manifestObjectUrl = null;
const rotaryStates = new Map();
let musicFiles = [];
let selectedMusicFile = '';

const pageTitleById = {
  apps: '啟動應用程式',
  media: '多媒體操控',
  macros: '巨集鍵盤指令'
};

const buttonLabelById = {
  'media-prev': '上一首',
  'media-play': '播放/暫停',
  'media-next': '下一首',
  'volume-rotary': '音量旋鈕'
};

const els = {
  settingsPage: document.getElementById('settingsPage'),
  deckPage: document.getElementById('deckPage'),
  mqttUrl: document.getElementById('mqttUrl'),
  clientId: document.getElementById('clientId'),
  username: document.getElementById('username'),
  password: document.getElementById('password'),
  baseTopic: document.getElementById('baseTopic'),
  connectButton: document.getElementById('connectButton'),
  installButton: document.getElementById('installButton'),
  settingsButton: document.getElementById('settingsButton'),
  status: document.getElementById('status'),
  connectionState: document.getElementById('connectionState'),
  grid: document.getElementById('grid'),
  pageNav: document.getElementById('pageNav')
};

function applyUrlSettings() {
  const overrides = readUrlSettings();
  if (overrides.reset) {
    localStorage.removeItem('macroPadMqtt');
    localStorage.removeItem('macroPadLayout');
    clearAppCaches();
    settings = { ...defaultSettings };
    layout = null;
  }
  if (!overrides.baseTopic) return;
  settings = {
    ...settings,
    baseTopic: overrides.baseTopic
  };
  localStorage.setItem('macroPadMqtt', JSON.stringify(settings));
}

function clearAppCaches() {
  if ('caches' in window) {
    caches.keys()
      .then(keys => Promise.all(keys.map(key => caches.delete(key))))
      .catch(() => {});
  }
  navigator.serviceWorker?.getRegistrations?.()
    .then(registrations => registrations.forEach(registration => registration.update()))
    .catch(() => {});
}

function updateManifestLink() {
  const topic = encodeURIComponent(settings.baseTopic || DEFAULT_BASE_TOPIC);
  const manifest = {
    name: 'Phone Macro Pad',
    short_name: 'Macro Pad',
    start_url: `./index.html?topic=${topic}`,
    scope: './',
    display: 'fullscreen',
    display_override: ['fullscreen', 'standalone'],
    orientation: 'landscape',
    background_color: '#101216',
    theme_color: '#101216',
    icons: [
      {
        src: './icon.svg',
        sizes: 'any',
        type: 'image/svg+xml',
        purpose: 'any maskable'
      }
    ]
  };
  const blob = new Blob([JSON.stringify(manifest)], { type: 'application/manifest+json' });
  const nextUrl = URL.createObjectURL(blob);
  const link = document.querySelector('link[rel="manifest"]');
  if (link) link.href = nextUrl;
  if (manifestObjectUrl) URL.revokeObjectURL(manifestObjectUrl);
  manifestObjectUrl = nextUrl;
}

function fillSettings() {
  els.mqttUrl.value = settings.mqttUrl;
  els.clientId.value = settings.clientId;
  els.username.value = settings.username;
  els.password.value = settings.password;
  els.baseTopic.value = settings.baseTopic;
  updateManifestLink();
}

function readSettings() {
  settings = {
    mqttUrl: els.mqttUrl.value.trim(),
    clientId: els.clientId.value.trim() || defaultSettings.clientId,
    username: els.username.value.trim(),
    password: els.password.value,
    baseTopic: els.baseTopic.value.trim() || DEFAULT_BASE_TOPIC
  };
  localStorage.setItem('macroPadMqtt', JSON.stringify(settings));
  updateManifestLink();
}

function isStandalonePwa() {
  return window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
}

async function enterFullscreen() {
  if (document.fullscreenElement) {
    await lockLandscape();
    return;
  }
  const root = document.documentElement;
  if (root.requestFullscreen) {
    try {
      await root.requestFullscreen({ navigationUI: 'hide' });
    } catch {
      // Some browsers only allow this from a direct user gesture.
    }
  }
  await lockLandscape();
}

async function lockLandscape() {
  const orientation = screen.orientation;
  if (!orientation?.lock) return;
  try {
    await orientation.lock('landscape');
  } catch {
    // Android browsers may reject orientation lock outside fullscreen or PWA mode.
  }
}

function armStandaloneFullscreen() {
  if (fullscreenAttemptArmed) return;
  fullscreenAttemptArmed = true;
  const run = () => enterFullscreen();
  window.addEventListener('pointerdown', run, { once: true, passive: true });
  window.addEventListener('keydown', run, { once: true });
}

function refreshInstallButton() {
  if (!els.installButton) return;
  if (isStandalonePwa()) {
    els.installButton.textContent = '已加入主畫面';
    els.installButton.disabled = true;
  } else {
    els.installButton.textContent = '加入主畫面';
    els.installButton.disabled = false;
  }
}

async function installPwa() {
  readSettings();
  updateManifestLink();
  if (isStandalonePwa()) {
    setStatus('已經以 PWA 模式開啟。');
    refreshInstallButton();
    enterFullscreen();
    return;
  }
  if (deferredInstallPrompt) {
    deferredInstallPrompt.prompt();
    await deferredInstallPrompt.userChoice;
    deferredInstallPrompt = null;
    refreshInstallButton();
    return;
  }
  setStatus('請使用瀏覽器選單的「加入主畫面」或「安裝應用程式」。');
}

function connect() {
  enterFullscreen();
  readSettings();
  if (!window.mqtt) {
    setStatus('找不到 MQTT WebSocket 函式庫，請確認手機可連網載入 mqtt.js。');
    return;
  }
  if (client) client.end(true);

  setStatus('連線中...');
  client = mqtt.connect(settings.mqttUrl, {
    clientId: settings.clientId,
    username: settings.username || undefined,
    password: settings.password || undefined,
    reconnectPeriod: 2000,
    clean: true
  });

  client.on('connect', () => {
    setStatus('已連線，等待電腦傳送 layout。');
    els.connectionState.textContent = '已連線';
    client.subscribe(`${settings.baseTopic}/layout`, { qos: 1 });
    client.subscribe(`${settings.baseTopic}/status`, { qos: 0 });
    client.subscribe(`${settings.baseTopic}/music/list`, { qos: 0 });
    client.publish(`${settings.baseTopic}/hello`, JSON.stringify({ clientId: settings.clientId, at: Date.now() }));
    requestMusicList();
  });

  client.on('reconnect', () => {
    els.connectionState.textContent = '重新連線中';
  });

  client.on('close', () => {
    els.connectionState.textContent = '離線';
  });

  client.on('error', error => {
    setStatus(`連線錯誤：${error.message || error}`);
  });

  client.on('message', (topic, payload) => {
    if (topic.endsWith('/music/list')) {
      const message = JSON.parse(payload.toString());
      musicFiles = Array.isArray(message.files) ? message.files.map(String) : [];
      if (selectedMusicFile && !musicFiles.includes(selectedMusicFile)) selectedMusicFile = '';
      if (!selectedMusicFile && musicFiles.length) selectedMusicFile = musicFiles[0];
      render();
      return;
    }
    if (topic.endsWith('/layout')) {
      layout = JSON.parse(payload.toString());
      localStorage.setItem('macroPadLayout', JSON.stringify(layout));
      pageIndex = 0;
      showDeck();
      render();
      setStatus('已收到 layout。');
    } else if (topic.endsWith('/status')) {
      const message = JSON.parse(payload.toString());
      els.connectionState.textContent = message.ok ? '已執行' : '執行失敗';
    }
  });
}

function showDeck() {
  els.settingsPage.classList.add('hidden');
  els.deckPage.classList.remove('hidden');
  enterFullscreen();
}

function showSettings() {
  els.deckPage.classList.add('hidden');
  els.settingsPage.classList.remove('hidden');
}

function render() {
  if (!layout?.pages?.length) return;
  const page = layout.pages[pageIndex] || layout.pages[0];
  const columns = layout.grid?.columns || 3;
  const rows = layout.grid?.rows || 8;
  els.grid.style.gridTemplateColumns = `repeat(${columns}, minmax(0, 1fr))`;
  els.grid.style.gridTemplateRows = `repeat(${rows}, minmax(0, 1fr))`;
  document.documentElement.style.setProperty('--accent', layout.theme?.accent || '#4cc9f0');
  applyDisplaySettings();
  els.grid.innerHTML = '';

  for (let slot = 0; slot < columns * rows; slot++) {
    if (isCoveredSlot(page.buttons || [], slot, columns)) continue;
    const button = page.buttons?.find(item => Number(item.slot) === slot);
    const isMusicList = button?.action?.type === 'music_list';
    const node = document.createElement(isMusicList ? 'section' : 'button');
    node.className = `pad-button ${button?.action?.type === 'rotary' ? 'rotary-pad' : ''} ${isMusicList ? 'music-list-panel' : ''} ${button ? '' : 'empty'}`;
    if (!isMusicList) node.disabled = !button;
    if (button?.spanColumns) node.style.gridColumn = `span ${button.spanColumns}`;
    if (button?.spanRows) node.style.gridRow = `span ${button.spanRows}`;
    if (button?.color && !isMusicList) node.style.background = `linear-gradient(145deg, ${button.color}, #0f172a)`;
    node.innerHTML = button ? renderButton(button) : '';
    if (isMusicList) {
      setupMusicList(node);
    } else if (button?.action?.type === 'rotary') {
      setupRotary(node, button);
    } else {
      node.addEventListener('click', () => sendAction(button));
    }
    els.grid.appendChild(node);
  }

  els.pageNav.innerHTML = '';
  layout.pages.forEach((item, index) => {
    const tab = document.createElement('button');
    tab.textContent = displayPageTitle(item, index);
    tab.className = index === pageIndex ? 'active' : '';
    tab.addEventListener('click', () => {
      pageIndex = index;
      render();
    });
    els.pageNav.appendChild(tab);
  });
}

function isCoveredSlot(buttons, slot, columns) {
  return buttons.some(button => {
    const origin = Number(button.slot);
    const spanColumns = Number(button.spanColumns || 1);
    const spanRows = Number(button.spanRows || 1);
    if (spanColumns <= 1 && spanRows <= 1) return false;
    const originColumn = origin % columns;
    const originRow = Math.floor(origin / columns);
    const column = slot % columns;
    const row = Math.floor(slot / columns);
    return slot !== origin
      && row >= originRow
      && row < originRow + spanRows
      && column >= originColumn
      && column < originColumn + spanColumns;
  });
}

function applyDisplaySettings() {
  const orientation = layout.display?.orientation || (layout.grid?.columns > layout.grid?.rows ? 'landscape' : 'portrait');
  els.deckPage.classList.toggle('landscape', orientation === 'landscape');
}

function renderButton(button) {
  if (button.action?.type === 'music_list') return renderMusicList();
  if (button.action?.type === 'rotary') return renderRotary(button);
  const image = button.iconUrl ? `<img src="${escapeHtml(resolveIconUrl(button.iconUrl))}" alt="">` : `<div class="glyph">${escapeHtml(button.icon || '')}</div>`;
  return `<span class="pad-inner">${image}<span class="label">${escapeHtml(displayButtonLabel(button))}</span></span>`;
}

function renderMusicList() {
  const rows = musicFiles.length
    ? musicFiles.map(file => `
        <button class="music-row ${file === selectedMusicFile ? 'selected' : ''}" type="button" data-file="${escapeHtml(file)}">
          <span>${escapeHtml(file.replace(/\.mp3$/i, ''))}</span>
        </button>`).join('')
    : '<div class="music-empty">Music 目錄沒有 mp3</div>';
  return `
    <div class="music-header">
      <strong>Music MP3</strong>
      <button class="music-refresh" type="button" aria-label="Refresh music list">↻</button>
    </div>
    <div class="music-rows">${rows}</div>`;
}

function setupMusicList(node) {
  node.querySelector('.music-refresh')?.addEventListener('click', event => {
    event.stopPropagation();
    requestMusicList();
  });
  node.querySelectorAll('.music-row').forEach(row => {
    row.addEventListener('click', () => {
      selectedMusicFile = row.dataset.file || '';
      sendMusicSelection(selectedMusicFile);
      render();
    });
  });
}

function renderRotary(button) {
  const state = rotaryState(button);
  const segments = Array.from({ length: 28 }, (_, index) => {
    const deg = 220 + (index / 27) * 280;
    const active = state.power && index <= Math.round((state.value / 100) * 27);
    return `<span class="rotary-seg ${active ? 'on' : ''}" style="transform:rotate(${deg}deg)"></span>`;
  }).join('');
  return `
    <span class="rotary-inner ${state.power ? '' : 'off'}" style="--rotary-angle:${valueToRotation(state.value)}deg">
      <span class="rotary-title">${escapeHtml(displayButtonLabel(button) || '音量')}</span>
      <span class="rotary-wrap">
        <span class="rotary-base"></span>
        <span class="rotary-segments">${segments}</span>
        <span class="rotary-ring"><span class="rotary-dial"></span></span>
        <span class="rotary-scale"><span>min</span><span>max</span></span>
      </span>
      <span class="rotary-value"><span>${String(state.value).padStart(2, '0')}</span><small>VOL</small></span>
      <span class="rotary-power" role="switch" aria-checked="${state.power}"><span></span>${state.power ? 'ON' : 'OFF'}</span>
    </span>`;
}

function rotaryState(button) {
  if (!rotaryStates.has(button.id)) {
    rotaryStates.set(button.id, {
      value: Number(button.value ?? 50),
      power: true,
      dragging: false,
      remainder: 0
    });
  }
  return rotaryStates.get(button.id);
}

function valueToRotation(value) {
  return -140 + (value / 100) * 280;
}

function pointToRotaryValue(node, clientX, clientY) {
  const rect = node.querySelector('.rotary-wrap').getBoundingClientRect();
  const cx = rect.left + rect.width / 2;
  const cy = rect.top + rect.height / 2;
  const dx = clientX - cx;
  const dy = clientY - cy;
  let deg = Math.atan2(dy, dx) * 180 / Math.PI + 90;
  if (deg < 0) deg += 360;
  let relative = deg - 220;
  if (relative < 0) relative += 360;
  relative = Math.min(280, Math.max(0, relative));
  return Math.round((relative / 280) * 100);
}

function setupRotary(node, button) {
  const state = rotaryState(button);
  const update = nextValue => {
    if (!state.power) return;
    const value = Math.min(100, Math.max(0, nextValue));
    const delta = value - state.value;
    state.value = value;
    state.remainder += delta;
    const stepSize = Number(button.action?.stepSize || 4);
    const steps = Math.trunc(state.remainder / stepSize);
    if (steps !== 0) {
      state.remainder -= steps * stepSize;
      const command = steps > 0 ? button.action?.upCommand || 'volume_up' : button.action?.downCommand || 'volume_down';
      for (let count = 0; count < Math.min(8, Math.abs(steps)); count++) {
        sendAction(button, { type: 'media', command });
      }
      navigator.vibrate?.(10);
    }
    node.innerHTML = renderRotary(button);
  };

  node.addEventListener('pointerdown', event => {
    event.preventDefault();
    const powerButton = event.target instanceof Element ? event.target.closest('.rotary-power') : null;
    if (powerButton) {
      state.power = !state.power;
      sendAction(button, { type: 'media', command: button.action?.muteCommand || 'mute' });
      node.innerHTML = renderRotary(button);
      return;
    }
    state.dragging = true;
    node.setPointerCapture(event.pointerId);
    update(pointToRotaryValue(node, event.clientX, event.clientY));
  });
  node.addEventListener('pointermove', event => {
    if (!state.dragging) return;
    event.preventDefault();
    update(pointToRotaryValue(node, event.clientX, event.clientY));
  });
  node.addEventListener('pointerup', () => { state.dragging = false; });
  node.addEventListener('pointercancel', () => { state.dragging = false; });
}

function resolveIconUrl(url) {
  if (!url) return '';
  if (/^(data:|https?:|blob:)/i.test(url)) return url;
  return `../${url}`;
}

function displayPageTitle(page, index) {
  return pageTitleById[page?.id] || cleanDisplayText(page?.title) || `頁面 ${index + 1}`;
}

function displayButtonLabel(button) {
  return buttonLabelById[button?.id] || cleanDisplayText(button?.label);
}

function cleanDisplayText(value) {
  const text = String(value ?? '').trim();
  if (!text) return '';
  return looksMojibake(text) ? '' : text;
}

function looksMojibake(text) {
  return /[�]|[-]|[-]|[-]/.test(text);
}

function sendAction(button, actionOverride = null) {
  if (!button || !client?.connected) return;
  const payload = {
    id: button.id,
    label: button.label,
    page: layout.pages[pageIndex]?.id,
    slot: button.slot,
    action: actionOverride || button.action,
    at: Date.now()
  };
  client.publish(`${settings.baseTopic}/action`, JSON.stringify(payload), { qos: 1 });
  navigator.vibrate?.(18);
}

function sendMusicSelection(filename) {
  if (!filename || !client?.connected) return;
  client.publish(`${settings.baseTopic}/action`, JSON.stringify({
    id: 'music-list',
    label: filename,
    page: layout.pages[pageIndex]?.id,
    slot: 0,
    action: { type: 'music_play', filename },
    at: Date.now()
  }), { qos: 1 });
  navigator.vibrate?.(18);
}

function requestMusicList() {
  if (!client?.connected) return;
  client.publish(`${settings.baseTopic}/music/request`, JSON.stringify({
    clientId: settings.clientId,
    at: Date.now()
  }), { qos: 0 });
}

function setStatus(text) {
  els.status.textContent = text;
}

function escapeHtml(value) {
  return String(value ?? '').replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[ch]));
}

els.connectButton.addEventListener('click', connect);
els.installButton.addEventListener('click', installPwa);
els.settingsButton.addEventListener('click', showSettings);

window.addEventListener('beforeinstallprompt', event => {
  event.preventDefault();
  readSettings();
  updateManifestLink();
  deferredInstallPrompt = event;
  refreshInstallButton();
});

window.addEventListener('appinstalled', () => {
  deferredInstallPrompt = null;
  refreshInstallButton();
  setStatus('已加入主畫面。');
});

applyUrlSettings();
fillSettings();
refreshInstallButton();
armStandaloneFullscreen();
if ('serviceWorker' in navigator) {
  navigator.serviceWorker.register('sw.js').catch(() => {});
}
if (layout) {
  showDeck();
  render();
}
