// BETTEREAT PWA service worker v1
// 頁面（HTML）：network-first，永遠先試網絡，斷網先用快取——唔會困住舊版。
// 同源圖片：cache-first，第二次開快好多。
// API／數據（Worker、GitHub raw、CDN）：一律唔經 cache，唔做，保證係新數。
var CACHE = "bettereat-v1";
self.addEventListener("install", function (e) { self.skipWaiting(); });
self.addEventListener("activate", function (e) {
  e.waitUntil(
    caches.keys().then(function (keys) {
      return Promise.all(keys.filter(function (k) { return k !== CACHE; }).map(function (k) { return caches.delete(k); }));
    }).then(function () { return self.clients.claim(); })
  );
});
self.addEventListener("fetch", function (e) {
  if (e.request.method !== "GET") return;
  var url = new URL(e.request.url);
  if (url.origin !== self.location.origin) return; // 跨域（Worker／GitHub／CDN）：唔 cache
  if (e.request.mode === "navigate") {
    e.respondWith(
      fetch(e.request).then(function (res) {
        if (res && res.ok) {
          var copy = res.clone();
          caches.open(CACHE).then(function (c) { c.put(e.request, copy); }).catch(function () {});
        }
        return res;
      }).catch(function () { return caches.match(e.request); })
    );
    return;
  }
  if (e.request.destination === "image") {
    e.respondWith(
      caches.match(e.request).then(function (hit) {
        if (hit) return hit;
        return fetch(e.request).then(function (res) {
          if (res && res.ok) {
            var copy = res.clone();
            caches.open(CACHE).then(function (c) { c.put(e.request, copy); }).catch(function () {});
          }
          return res;
        });
      })
    );
    return;
  }
  // 其他同源（manifest 等）：唔 cache，直接行網絡。
});
