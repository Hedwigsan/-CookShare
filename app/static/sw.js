// v2 — CookShare SW
const VERSION = "v2.0.0";
const PRECACHE = `precache-${VERSION}`;
const RUNTIME = `runtime-${VERSION}`;

const PRECACHE_ASSETS = [
  "/",                    // HTMLシェル
  "/offline",             // オフライン用ページ
  "/static/logo.svg",
  "/static/icons/icon-192.png",
  "/static/icons/icon-512.png",
];

// install: 重要アセットをプリキャッシュ
self.addEventListener("install", (e) => {
  e.waitUntil(
    caches.open(PRECACHE).then((cache) => cache.addAll(PRECACHE_ASSETS))
  );
  self.skipWaiting();
});

// activate: 古いキャッシュを掃除
self.addEventListener("activate", (e) => {
  e.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter(k => ![PRECACHE, RUNTIME].includes(k)).map(k => caches.delete(k)))
    )
  );
  self.clients.claim();
});

// 便利: HTMLかどうか判定
const isHTML = (req) =>
  req.method === "GET" &&
  req.headers.get("accept") &&
  req.headers.get("accept").includes("text/html");

// fetch: ルーティング別に戦略を変える
self.addEventListener("fetch", (e) => {
  const req = e.request;
  const url = new URL(req.url);

  // ナビゲーション（ページ遷移）は「ネット優先→失敗時オフラインページ」
  if (isHTML(req)) {
    e.respondWith(
      fetch(req)
        .then((res) => {
          // 成功レスポンスはランタイムキャッシュに保存
          const copy = res.clone();
          caches.open(RUNTIME).then((c) => c.put(req, copy));
          return res;
        })
        .catch(async () => {
          const cached = await caches.match(req);
          return cached || caches.match("/offline");
        })
    );
    return;
  }

  // 画像（/media/*）は「キャッシュ優先・あれば即表示 / 無ければ取得して保存」
  if (url.pathname.startsWith("/media/")) {
    e.respondWith(
      caches.match(req).then((hit) =>
        hit ||
        fetch(req)
          .then((res) => {
            const copy = res.clone();
            caches.open(RUNTIME).then((c) => c.put(req, copy));
            return res;
          })
          .catch(() => caches.match("/static/logo.svg")) // 代替
      )
    );
    return;
  }

  // その他の静的（/static/*）は「キャッシュ優先」
  if (url.pathname.startsWith("/static/")) {
    e.respondWith(
      caches.match(req).then((hit) => hit || fetch(req).then((res) => {
        const copy = res.clone();
        caches.open(RUNTIME).then((c) => c.put(req, copy));
        return res;
      }))
    );
    return;
  }

  // API（GET）は「ネット優先 / 失敗時キャッシュ」
  if (url.pathname.startsWith("/api/") || url.pathname.startsWith("/recipes")) {
    if (req.method === "GET") {
      e.respondWith(
        fetch(req).then((res) => {
          const copy = res.clone();
          caches.open(RUNTIME).then((c) => c.put(req, copy));
          return res;
        }).catch(() => caches.match(req))
      );
      return;
    }
    // POSTなどは fetch に任せる（オフラインはクライアント側でキュー）
  }
});