/* 底部标签栏：全站共用一份，改这里所有页面同步 */
(() => {
  // ---- 登录失效兜底：任何 /api/ 请求返回 401 就整页跳登录页 ----
  // 各页面都自己写 fetch，逐个加判断容易漏，包一层最省事。
  const origFetch = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const res = await origFetch(...args);
    if (res.status === 401) {
      const url = String(args[0] && args[0].url ? args[0].url : args[0] || '');
      if (url.includes('/api/') && !url.includes('/api/auth-state')) {
        location.replace('/login?next=' + encodeURIComponent(location.pathname));
      }
    }
    return res;
  };

  // 登录页不显示标签栏
  if (location.pathname === '/login') return;

  const I = {
    call: '<path d="M4.5 4.5h3l1.5 4-2 1.5a11 11 0 0 0 5 5l1.5-2 4 1.5v3a1.5 1.5 0 0 1-1.6 1.5A16.5 16.5 0 0 1 3 6.1 1.5 1.5 0 0 1 4.5 4.5z"/>',
    history: '<circle cx="12" cy="12" r="8.2"/><path d="M12 7.6V12l3 1.8"/>',
    stats: '<path d="M5 4.5h11.5a2 2 0 0 1 2 2v14L12 17l-6.5 3.5V6.5a2 2 0 0 1 2-2z"/><path d="M8.6 9.4h6.8M8.6 12.6h4.4"/>',
    words: '<path d="M4.2 5.4A2 2 0 0 1 6.2 3.4H19v13.2H6.2a2 2 0 0 0-2 2z"/><path d="M19 16.6v4H6.2a2 2 0 0 1-2-2"/><path d="M8 7.6h7M8 11h5"/>',
    me: '<circle cx="12" cy="8.4" r="3.6"/><path d="M4.8 20c.6-3.6 3.6-5.6 7.2-5.6s6.6 2 7.2 5.6"/>',
  };
  // 五个标签：通话 / 记录 / 生词 / 错题本 / 我的。
  // 生词本必须有一条看得见的路 —— 藏在复盘弹层里的功能等于没做。
  const items = [
    { href: '/', label: '通话', icon: I.call },
    { href: '/history', label: '记录', icon: I.history },
    { href: '/words', label: '生词本', icon: I.words },
    { href: '/stats', label: '错题本', icon: I.stats },
    { href: '/me', label: '我的', icon: I.me },
  ];
  let path = location.pathname.replace(/\/+$/, '') || '/';
  if (path === '/index.html') path = '/';

  const nav = document.createElement('nav');
  nav.className = 'tabbar';
  nav.innerHTML = items.map(i => `
    <a href="${i.href}" class="${i.href === path ? 'on' : ''}">
      <svg viewBox="0 0 24 24" fill="none" stroke="currentColor"
           stroke-linecap="round" stroke-linejoin="round">${i.icon}</svg>
      <span>${i.label}</span>
    </a>`).join('');
  document.body.appendChild(nav);
})();
