// Smart Attendance SW — v3 (force-kills v1/v2)
const CACHE_NAME = 'tmu-attendance-v3';
const STATIC_ASSETS = ['/', '/index.html', '/manifest.json'];

// Allow the page to tell the SW to skip waiting and take over immediately
self.addEventListener('message', e => {
  if (e.data?.type === 'SKIP_WAITING') self.skipWaiting();
});

self.addEventListener('install', e => {
  // Take over immediately — don't wait for old SW to die
  self.skipWaiting();
  e.waitUntil(
    caches.open(CACHE_NAME).then(cache =>
      Promise.allSettled(STATIC_ASSETS.map(url => cache.add(url).catch(() => {})))
    )
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    // Delete ALL old caches (v1, v2, anything else)
    caches.keys()
      .then(keys => Promise.all(
        keys.filter(k => k !== CACHE_NAME).map(k => {
          console.log('[SW v3] Deleting old cache:', k);
          return caches.delete(k);
        })
      ))
      .then(() => self.clients.claim()) // take control of all open tabs immediately
  );
});

self.addEventListener('fetch', e => {
  if (e.request.method !== 'GET') return;
  const url = new URL(e.request.url);
  if (url.origin !== self.location.origin) return;

  // NEVER cache sw.js itself
  if (url.pathname === '/sw.js') return;

  // API calls — always go to network, never cache
  if (url.pathname.startsWith('/api/') ||
      url.pathname.match(/^\/(sessions|timetable|attendance|users|courses|settings|auth)/)) {
    return;
  }

  // index.html — always network first, fall back to cache
  if (url.pathname === '/' || url.pathname === '/index.html' || e.request.mode === 'navigate') {
    e.respondWith(networkFirst(e.request));
    return;
  }

  // Static assets (images, icons) — cache first
  if (['image', 'font'].includes(e.request.destination)) {
    e.respondWith(cacheFirst(e.request));
    return;
  }

  e.respondWith(networkFirst(e.request));
});

async function cacheFirst(req) {
  const cache  = await caches.open(CACHE_NAME);
  const cached = await cache.match(req);
  if (cached) return cached;
  try {
    const res = await fetch(req);
    if (res.ok) cache.put(req, res.clone());
    return res;
  } catch { return new Response('Offline', { status: 503 }); }
}

async function networkFirst(req) {
  const cache = await caches.open(CACHE_NAME);
  try {
    const res = await fetch(req);
    if (res.ok) cache.put(req, res.clone());
    return res;
  } catch {
    const cached = await cache.match(req) || await cache.match('/index.html');
    return cached || new Response('Offline — please reconnect.', { status: 503, headers: { 'Content-Type': 'text/plain' } });
  }
}

// Push notifications
self.addEventListener('push', e => {
  let data = { title: 'Smart Attendance', body: 'You have a notification', url: '/' };
  try { if (e.data) data = JSON.parse(e.data.text()); } catch {}
  e.waitUntil(
    self.registration.showNotification(data.title, {
      body: data.body, icon: '/tmu_icon_192.png', badge: '/tmu_icon_192.png',
      vibrate: [200, 100, 200], data: { url: data.url || '/' },
      actions: [{ action: 'open', title: 'Open' }, { action: 'dismiss', title: 'Dismiss' }],
    })
  );
});

self.addEventListener('notificationclick', e => {
  e.notification.close();
  if (e.action === 'dismiss') return;
  const url = e.notification.data?.url || '/';
  e.waitUntil(
    clients.matchAll({ type: 'window', includeUncontrolled: true }).then(list => {
      for (const c of list) {
        if (c.url.includes(self.location.origin)) { c.focus(); c.navigate(url); return; }
      }
      return clients.openWindow(url);
    })
  );
});
