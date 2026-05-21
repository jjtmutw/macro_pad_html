const defaultSettings = {
  mqttUrl: 'wss://broker.emqx.io:8084/mqtt',
  clientId: `phone-macro-pad-${Math.random().toString(16).slice(2, 8)}`,
  username: '',
  password: '',
  baseTopic: 'macro-pad'
};

let settings = JSON.parse(localStorage.getItem('macroPadMqtt') || JSON.stringify(defaultSettings));
let layout = JSON.parse(localStorage.getItem('macroPadLayout') || 'null');
let client = null;
let pageIndex = 0;
let deferredInstallPrompt = null;
let fullscreenAttemptArmed = false;

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
  pageTitle: document.getElementById('pageTitle'),
  grid: document.getElementById('grid'),
  pageNav: document.getElementById('pageNav')
};

function fillSettings() {
  els.mqttUrl.value = settings.mqttUrl;
  els.clientId.value = settings.clientId;
  els.username.value = settings.username;
  els.password.value = settings.password;
  els.baseTopic.value = settings.baseTopic;
}

function readSettings() {
  settings = {
    mqttUrl: els.mqttUrl.value.trim(),
    clientId: els.clientId.value.trim() || defaultSettings.clientId,
    username: els.username.value.trim(),
    password: els.password.value,
    baseTopic: els.baseTopic.value.trim() || 'macro-pad'
  };
  localStorage.setItem('macroPadMqtt', JSON.stringify(settings));
}

function isStandalonePwa() {
  return window.matchMedia('(display-mode: standalone)').matches || window.navigator.standalone === true;
}

async function enterFullscreen() {
  if (!isStandalonePwa() || document.fullscreenElement) return;
  const root = document.documentElement;
  if (!root.requestFullscreen) return;
  try {
    await root.requestFullscreen({ navigationUI: 'hide' });
  } catch {
    // Some browsers only allow this from a direct user gesture.
  }
}

function armStandaloneFullscreen() {
  if (!isStandalonePwa() || fullscreenAttemptArmed) return;
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
    client.publish(`${settings.baseTopic}/hello`, JSON.stringify({ clientId: settings.clientId, at: Date.now() }));
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
  els.pageTitle.textContent = page.title || '控制頁';
  document.documentElement.style.setProperty('--accent', layout.theme?.accent || '#4cc9f0');
  applyDisplaySettings();
  els.grid.innerHTML = '';

  for (let slot = 0; slot < columns * rows; slot++) {
    const button = page.buttons?.find(item => Number(item.slot) === slot);
    const node = document.createElement('button');
    node.className = `pad-button ${button ? '' : 'empty'}`;
    node.disabled = !button;
    if (button?.color) node.style.background = `linear-gradient(145deg, ${button.color}, #0f172a)`;
    node.innerHTML = button ? renderButton(button) : '';
    node.addEventListener('click', () => sendAction(button));
    els.grid.appendChild(node);
  }

  els.pageNav.innerHTML = '';
  layout.pages.forEach((item, index) => {
    const tab = document.createElement('button');
    tab.textContent = item.title || `頁面 ${index + 1}`;
    tab.className = index === pageIndex ? 'active' : '';
    tab.addEventListener('click', () => {
      pageIndex = index;
      render();
    });
    els.pageNav.appendChild(tab);
  });
}

function applyDisplaySettings() {
  const orientation = layout.display?.orientation || (layout.grid?.columns > layout.grid?.rows ? 'landscape' : 'portrait');
  els.deckPage.classList.toggle('landscape', orientation === 'landscape');
  const backgroundImage = layout.display?.backgroundImage || '';
  els.deckPage.style.setProperty('--deck-bg-image', backgroundImage ? `url("${backgroundImage.replaceAll('"', '\\"')}")` : 'none');
}

function renderButton(button) {
  const image = button.iconUrl ? `<img src="${escapeHtml(resolveIconUrl(button.iconUrl))}" alt="">` : `<div class="glyph">${escapeHtml(button.icon || '')}</div>`;
  return `<span class="pad-inner">${image}<span class="label">${escapeHtml(button.label || '')}</span></span>`;
}

function resolveIconUrl(url) {
  if (!url) return '';
  if (/^(data:|https?:|blob:)/i.test(url)) return url;
  return `../${url}`;
}

function sendAction(button) {
  if (!button || !client?.connected) return;
  const payload = {
    id: button.id,
    label: button.label,
    page: layout.pages[pageIndex]?.id,
    slot: button.slot,
    action: button.action,
    at: Date.now()
  };
  client.publish(`${settings.baseTopic}/action`, JSON.stringify(payload), { qos: 1 });
  navigator.vibrate?.(18);
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
  deferredInstallPrompt = event;
  refreshInstallButton();
});

window.addEventListener('appinstalled', () => {
  deferredInstallPrompt = null;
  refreshInstallButton();
  setStatus('已加入主畫面。');
});

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
