/* ringkeeper dashboard service worker (minimal).
 * - real-data.json + page navigations: network-first, cached copy only when offline
 * - versioned static shell (styles/app ?v=, icons, manifest): cache-first
 * - /api/*: never cached (live local AI answers)
 * Bump VERSION together with the ?v= stamps in index.html; old caches are deleted on activate.
 */
const VERSION = '1795669065';
const CACHE = 'ringkeeper-' + VERSION;
const SHELL = ['./', 'index.html', 'styles.css?v=' + VERSION, 'app.js?v=' + VERSION, 'manifest.json',
  'icons/icon-192.png', 'icons/icon-512.png', 'icons/icon-maskable-512.png', 'icons/apple-touch-icon.png'];

self.addEventListener('install', e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', e => {
  e.waitUntil(caches.keys()
    .then(keys => Promise.all(keys.filter(k => k.startsWith('ringkeeper-') && k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});

async function networkFirst(req) {
  const c = await caches.open(CACHE);
  try {
    const r = await fetch(req);
    if (r.ok) c.put(req, r.clone());
    return r;
  } catch (err) {
    const hit = await c.match(req, { ignoreSearch: true });
    if (hit) return hit;
    throw err;
  }
}

async function cacheFirst(req) {
  const c = await caches.open(CACHE);
  const hit = await c.match(req);
  if (hit) return hit;
  const r = await fetch(req);
  if (r.ok) c.put(req, r.clone());
  return r;
}

self.addEventListener('fetch', e => {
  const req = e.request;
  if (req.method !== 'GET') return;
  const url = new URL(req.url);
  if (url.origin !== self.location.origin || url.pathname.includes('/api/')) return;
  if (req.mode === 'navigate' || url.pathname.endsWith('/real-data.json') || url.pathname.endsWith('/index.html')) {
    e.respondWith(networkFirst(req));
    return;
  }
  e.respondWith(cacheFirst(req));
});
