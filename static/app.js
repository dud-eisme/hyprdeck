const WS_URL = `ws://${location.host}/ws`;
const NUM_WORKSPACES = 10; // matches the usual hyprland 1-10 keybinds, change if you use more

const wsGrid = document.getElementById('wsGrid');
const connEl = document.getElementById('conn');
const clockEl = document.getElementById('clock');
const trackTitle = document.getElementById('trackTitle');
const trackArtist = document.getElementById('trackArtist');
const playBtn = document.getElementById('playBtn');
const playIcon = document.getElementById('playIcon');
const prevBtn = document.getElementById('prevBtn');
const nextBtn = document.getElementById('nextBtn');
const muteBtn = document.getElementById('muteBtn');
const volIcon = document.getElementById('volIcon');
const volSlider = document.getElementById('volSlider');
const volValue = document.getElementById('volValue');
const brightSlider = document.getElementById('brightSlider');
const brightValue = document.getElementById('brightValue');

let socket = null;
let sliderDragging = { vol: false, bright: false };

const trackArt = document.getElementById('trackArt');

const cpuFill = document.getElementById('cpuFill');
const cpuValue = document.getElementById('cpuValue');
const memFill = document.getElementById('memFill');
const memValue = document.getElementById('memValue');
const gpuRow = document.getElementById('gpuRow');
const gpuFill = document.getElementById('gpuFill');
const gpuValue = document.getElementById('gpuValue');
const hotspotDot = document.getElementById('hotspotDot');
const hotspotSsid = document.getElementById('hotspotSsid');
const hotspotClients = document.getElementById('hotspotClients');

// ---------- tiles ----------

function buildTiles() {
  wsGrid.innerHTML = '';
  for (let i = 1; i <= NUM_WORKSPACES; i++) {
    const tile = document.createElement('button');
    tile.className = 'ws-tile empty';
    tile.dataset.id = i;
    tile.textContent = i;
    // pointerdown fires the instant a finger touches down, instead of
    // waiting for the full tap+release+disambiguation that 'click' does
    tile.addEventListener('pointerdown', () => {
      send('workspace', { id: i });
    });
    wsGrid.appendChild(tile);
  }
}

// Some apps/classes don't have a real brand logo on simple-icons (Binary
// Ninja, virt-manager/KVM, and "cybersecurity" as a generic concept all 404
// on the CDN). Rather than reproduce those tools' actual trademarked logos,
// these are small generic local icons instead - checked first, before
// falling back to the simple-icons CDN for anything else.
const LOCAL_ICONS = {
  'binaryninja': 'icons/binaryninja.svg',
  'virt-manager': 'icons/vm.svg',
  'org.virt-manager.virt-manager': 'icons/vm.svg',
  'cybersec': 'icons/shield.svg',
};

const iconMap = {
  // browsers
  'firefox': 'firefox',
  'org.mozilla.firefox': 'firefox',
  'google-chrome': 'googlechrome',
  'brave-browser': 'brave',

  // terminals
  'alacritty': 'alacritty',
  'kitty': 'gnometerminal',
  'org.wezfurlong.wezterm': 'wezterm',
  'xterm': 'gnu',           // no real xterm icon in simple-icons; placeholder
  'uxterm': 'gnu',          // same

  // chat / media
  'discord': 'discord',
  'vesktop': 'discord',
  'spotify': 'spotify',
  'spotify_player': 'spotify',
  'vlc': 'vlcmediaplayer',
  'audacity': 'audacity',

  // dev tools
  'code': 'visualstudiocode',
  'cmake': 'cmake',
  'neovim': 'neovim',
  'nvim': 'neovim',
  'burpsuite': 'burpsuite',
  'sqlitebrowser': 'sqlite', // DB Browser for SQLite — close enough visually
  'org.sqlitebrowser.sqlitebrowser': 'sqlite',

  // virtualization
  'qemu': 'qemu',
  'virt-manager': 'kvm',     // Virtual Machine Manager — no exact icon, kvm is closest
  'org.virt-manager.virt-manager': 'kvm',

  // wine/windows compat
  'wine': 'wine',            // verify — may not exist

  // file manager
  'org.kde.dolphin': 'kde',  // no dedicated Dolphin icon; falls back to KDE logo
  'dolphin': 'kde',

  // custom terminal workspaces (see --class flag on the alacritty binds)
  // icon handled via LOCAL_ICONS above (no matching brand logo on the CDN)
  'github': 'github',

  // java runtime windows (if any app shows as this)
  'java': 'openjdk',
  'openjdk': 'openjdk',
};

const titleOverrides = [
  { pattern: /nvim|neovim/i, slug: 'neovim' },
  { pattern: /htop/i, slug: 'htop' },      // verify slug exists
  { pattern: /^git /i, slug: 'git' },
];

function resolveIconSlug(winClass, winTitle) {
  const title = winTitle || '';
  for (const { pattern, slug } of titleOverrides) {
    if (pattern.test(title)) return slug;
  }
  return iconMap[winClass] || winClass.replace(/[^a-z0-9]/g, '');
}

function updateTiles(workspaces, activeId) {
  const workspaceMap = new Map((workspaces || []).map(w => [w.id, w]));

  document.querySelectorAll('.ws-tile').forEach(tile => {
    const id = Number(tile.dataset.id);
    const wsData = workspaceMap.get(id);

    // Toggle active and empty states
    tile.classList.toggle('empty', !wsData);
    tile.classList.toggle('active', id === activeId);

    // Reset tile content with a dedicated label for the workspace number
    tile.innerHTML = `<div class="ws-label">${id}</div>`;

    // Render windows if they exist
    if (wsData && wsData.windows) {
      wsData.windows.forEach(win => {
        const winEl = document.createElement('div');
        winEl.className = 'ws-window';
        if (win.focused) winEl.classList.add('focused');

        // Apply relative positioning and sizing provided by server.py
        winEl.style.left = `${win.x * 100}%`;
        winEl.style.top = `${win.y * 100}%`;
        winEl.style.width = `${win.w * 100}%`;
        winEl.style.height = `${win.h * 100}%`;

        // Map common Arch/Hyprland app classes to Simple Icons slugs
        const winClass = (win.class || 'sys').toLowerCase();

        const img = document.createElement('img');
        img.className = 'ws-icon';

        if (LOCAL_ICONS[winClass]) {
          // known local icon (no real brand logo exists for this app)
          img.src = LOCAL_ICONS[winClass];
        } else {
          // Clean the class name to match expected Simple Icon slugs
          const slug = resolveIconSlug(winClass, win.title);
          // Fetch the SVG logo from the jsDelivr CDN
          img.src = `https://cdn.jsdelivr.net/npm/simple-icons@11/icons/${slug}.svg`;
        }

        // If the icon 404s (e.g. a CDN slug that doesn't exist), fallback to text
        img.onerror = () => {
          img.style.display = 'none';
          winEl.textContent = (win.class || 'sys').substring(0, 4).toLowerCase();
        };

        winEl.appendChild(img);
        tile.appendChild(winEl);
      });
    }
  });
}

// ---------- state application ----------

function applyState(s) {
  updateTiles(s.workspaces, s.active_workspace);

  if (s.media && s.media.status) {
    trackTitle.textContent = s.media.title || '(untitled)';
    trackArtist.textContent = s.media.artist || '';
    playIcon.innerHTML = s.media.status === 'Playing'
      ? '<path d="M6 5h4v14H6zM14 5h4v14h-4z"/>'
      : '<path d="M8 5v14l11-7z"/>';

    if (s.media.art_url) {
      trackArt.src = s.media.art_url;
      trackArt.style.display = 'block';
    } else {
      trackArt.style.display = 'none';
    }
  } else {
    trackTitle.textContent = 'nothing playing';
    trackArtist.textContent = '';
    playIcon.innerHTML = '<path d="M8 5v14l11-7z"/>';
    trackArt.style.display = 'none';
  }

  if (!sliderDragging.vol) {
    volSlider.value = s.volume;
    volValue.textContent = s.muted ? 'mute' : s.volume;
  }
  muteBtn.classList.toggle('muted', !!s.muted);
  volIcon.innerHTML = s.muted
    ? '<path d="M3 10v4h4l5 5V5L7 10H3zM19 9l-4 4M15 9l4 4"/>'
    : '<path d="M3 10v4h4l5 5V5L7 10H3zM16 8a5 5 0 010 8"/>';

  if (s.brightness != null && !sliderDragging.bright) {
    brightSlider.value = s.brightness;
    brightValue.textContent = s.brightness;
  }

  if (s.system) {
    if (s.system.cpu != null) {
      cpuFill.style.width = `${s.system.cpu}%`;
      cpuValue.textContent = `${s.system.cpu}%`;
    }
    if (s.system.mem != null) {
      memFill.style.width = `${s.system.mem}%`;
      memValue.textContent = `${s.system.mem}%`;
    }
    if (s.system.gpu) {
      gpuRow.style.display = 'flex';
      gpuFill.style.width = `${s.system.gpu.util}%`;
      gpuValue.textContent = `${s.system.gpu.temp}°`;
    }
  }

  if (s.hotspot) {
    hotspotDot.classList.toggle('active', !!s.hotspot.active);
    hotspotSsid.textContent = s.hotspot.ssid || 'inactive';
    hotspotClients.textContent = `${s.hotspot.client_count} connected`;
  }
}

// ---------- networking ----------

// Fallback for when the socket isn't open yet/dropped - same REST
// endpoints as before, used only if send() can't reach the socket.
function post(path, body) {
  fetch(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body || {}),
  }).catch(() => { });
}

// Primary control path: send commands over the already-open websocket
// instead of firing a brand new HTTP request per tap. This avoids paying
// a fresh TCP+HTTP round trip for every single interaction - the socket
// is already connected and warm, so this is just a single frame write.
// REST_FALLBACK maps a websocket action back to its REST equivalent, only
// used if the socket happens to be down when a control is used.
const REST_FALLBACK = {
  workspace: (p) => post('/api/workspace', { id: p.id }),
  window_focus: (p) => post('/api/window/focus', { address: p.address }),
  media: (p) => post(`/api/media/${p.action}`, {}),
  volume: (p) => post('/api/volume', { level: p.level }),
  volume_mute: () => post('/api/volume/mute', {}),
  brightness: (p) => post('/api/brightness', { level: p.level }),
  launch: (p) => post(`/api/launch/${p.name}`, {}),
};

function send(type, payload = {}) {
  if (socket && socket.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ type, ...payload }));
  } else {
    const fallback = REST_FALLBACK[type];
    if (fallback) fallback(payload);
  }
}

function connect() {
  socket = new WebSocket(WS_URL);

  socket.addEventListener('open', () => {
    // Only add the class to change the icon color, don't overwrite HTML
    connEl.classList.add('live');
  });

  socket.addEventListener('message', (ev) => {
    const msg = JSON.parse(ev.data);
    if (msg.type === 'state') applyState(msg.data);
  });

  socket.addEventListener('close', () => {
    connEl.classList.remove('live');
    setTimeout(connect, 1500);
  });

  socket.addEventListener('error', () => socket.close());
}

// ---------- controls ----------

playBtn.addEventListener('pointerdown', () => send('media', { action: 'play-pause' }));
prevBtn.addEventListener('pointerdown', () => send('media', { action: 'previous' }));
nextBtn.addEventListener('pointerdown', () => send('media', { action: 'next' }));
muteBtn.addEventListener('pointerdown', () => send('volume_mute'));

document.querySelectorAll('.launch-btn').forEach(btn => {
  btn.addEventListener('pointerdown', () => {
    send('launch', { name: btn.dataset.app });
  });

  // if a given icon 404s (e.g. binja isn't in simple-icons), just hide
  // the broken image and keep the text label so the button still works
  const img = btn.querySelector('img');
  if (img) {
    img.onerror = () => { img.style.display = 'none'; };
  }
});

connEl.addEventListener('click', () => {
  if (!document.fullscreenElement) {
    document.documentElement.requestFullscreen().catch(() => { });
  } else {
    document.exitFullscreen().catch(() => { });
  }
});

let volDebounce;
volSlider.addEventListener('input', () => {
  sliderDragging.vol = true;
  volValue.textContent = volSlider.value;
  clearTimeout(volDebounce);
  volDebounce = setTimeout(() => send('volume', { level: Number(volSlider.value) }), 30);
});
volSlider.addEventListener('change', () => {
  sliderDragging.vol = false;
});

let brightDebounce;
brightSlider.addEventListener('input', () => {
  sliderDragging.bright = true;
  brightValue.textContent = brightSlider.value;
  clearTimeout(brightDebounce);
  brightDebounce = setTimeout(() => send('brightness', { level: Number(brightSlider.value) }), 30);
});
brightSlider.addEventListener('change', () => {
  sliderDragging.bright = false;
});

// ---------- clock ----------

function tickClock() {
  const d = new Date();
  clockEl.textContent = d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
}
setInterval(tickClock, 1000 * 30);
tickClock();



// Vertical sliders use a rotated horizontal <input>, so their "width" is
// actually their visual length once rotated. Since that depends on the
// panel's real rendered height (which varies by screen), we measure and
// set it in JS rather than hardcoding a pixel value.
function sizeVerticalSliders() {
  document.querySelectorAll('.slider-wrapper').forEach(wrapper => {
    const slider = wrapper.querySelector('.slider.vertical');
    if (slider) slider.style.width = `${wrapper.clientHeight}px`;
  });
}

window.addEventListener('resize', sizeVerticalSliders);
window.addEventListener('orientationchange', () => setTimeout(sizeVerticalSliders, 100));

// ---------- boot ----------

buildTiles();
connect();
sizeVerticalSliders();

// keep the screen from feeling stale if the tab is backgrounded and returns
document.addEventListener('visibilitychange', () => {
  if (document.visibilityState === 'visible' && (!socket || socket.readyState !== 1)) {
    connect();
  }
});
