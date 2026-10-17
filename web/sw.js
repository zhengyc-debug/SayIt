// 极简 service worker：只为让 PWA 可安装。不做离线缓存，避免开发时缓存干扰。
self.addEventListener("install", (e) => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", () => {});
