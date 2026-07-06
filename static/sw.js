/* Wheelhouse service worker — offline app shell + CDN asset cache.
 * Never touches API/auth/photo-upload routes: those always go to the network. */
var SHELL_CACHE = 'wh-shell-v1';
var RUNTIME_CACHE = 'wh-runtime-v1';
var CDN_HOSTS = ['unpkg.com', 'cdnjs.cloudflare.com'];
var NEVER_CACHE_PREFIXES = ['/api/', '/log-catch-photo', '/parse-catch-photo',
                            '/catch-photos/', '/post-photos/', '/login', '/admin'];

self.addEventListener('install', function(event) {
  event.waitUntil(
    caches.open(SHELL_CACHE)
      .then(function(cache) { return cache.add('/'); })
      .then(function() { return self.skipWaiting(); })
  );
});

self.addEventListener('activate', function(event) {
  event.waitUntil(
    caches.keys().then(function(keys) {
      return Promise.all(keys.filter(function(k) {
        return k !== SHELL_CACHE && k !== RUNTIME_CACHE;
      }).map(function(k) { return caches.delete(k); }));
    }).then(function() { return self.clients.claim(); })
  );
});

self.addEventListener('fetch', function(event) {
  var req = event.request;
  if (req.method !== 'GET') return;

  var url = new URL(req.url);

  // Never intercept API, auth, or photo routes — straight to the network.
  if (url.origin === self.location.origin) {
    for (var i = 0; i < NEVER_CACHE_PREFIXES.length; i++) {
      if (url.pathname.indexOf(NEVER_CACHE_PREFIXES[i]) === 0) return;
    }
  }

  // App shell navigations: network-first, cached shell when offline.
  if (req.mode === 'navigate' ||
      (url.origin === self.location.origin && url.pathname === '/')) {
    event.respondWith(
      fetch(req).then(function(res) {
        if (res && res.ok) {
          var copy = res.clone();
          caches.open(SHELL_CACHE).then(function(cache) { cache.put('/', copy); });
        }
        return res;
      }).catch(function() {
        return caches.match('/').then(function(cached) {
          return cached || Response.error();
        });
      })
    );
    return;
  }

  // CDN assets (Leaflet css/js, marker images): stale-while-revalidate.
  if (CDN_HOSTS.indexOf(url.hostname) !== -1) {
    event.respondWith(
      caches.open(RUNTIME_CACHE).then(function(cache) {
        return cache.match(req).then(function(cached) {
          var network = fetch(req).then(function(res) {
            if (res && (res.ok || res.type === 'opaque')) {
              cache.put(req, res.clone());
            }
            return res;
          });
          if (cached) {
            network.catch(function() {});  // background refresh may fail offline
            return cached;
          }
          return network;
        });
      })
    );
  }
});

// ==================== WEB PUSH ====================
self.addEventListener('push', function(event) {
  var data = {};
  try { data = event.data ? event.data.json() : {}; } catch (e) { data = {}; }
  var title = (data && data.title) || 'Wheelhouse';
  event.waitUntil(
    self.registration.showNotification(title, {
      body: (data && data.body) || '',
      icon: '/static/icon-192.png',
      badge: '/static/icon-192.png',
      data: { url: (data && data.url) || '/' }
    })
  );
});

self.addEventListener('notificationclick', function(event) {
  event.notification.close();
  var url = (event.notification.data && event.notification.data.url) || '/';
  event.waitUntil(
    self.clients.matchAll({ type: 'window', includeUncontrolled: true }).then(function(wins) {
      for (var i = 0; i < wins.length; i++) {
        if ('focus' in wins[i]) return wins[i].focus();
      }
      return self.clients.openWindow(url);
    })
  );
});
