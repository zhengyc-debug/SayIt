"use strict";
// SayIt 主题：深色 / 浅色 / 跟随系统。
//
// 做法：<html data-theme="dark|light">，CSS 里两套变量挂在 :root 和
// :root[data-theme="light"] 上，所有颜色都走变量，所以这里只切一个属性。
//
// 三个入口共用这一份：
//   · 通话页右上角的 ☀ / ☾ 按钮（热切换，不需要刷新）
//   · 设置页「外观」那一排
//   · 系统偏好变化（theme=auto 时跟随）
// 选择存 localStorage（sayit.theme），不进服务端配置 —— 这是设备级的显示偏好，
// 换手机应该各自不同，不该跟着账号跑。
(() => {
  const KEY = "sayit.theme";
  const VALID = ["dark", "light", "auto"];
  const META = "meta[name=theme-color]";
  const COLOR = { dark: "#080b12", light: "#f5f7fb" };

  const mq = window.matchMedia ? window.matchMedia("(prefers-color-scheme: light)") : null;

  function stored() {
    const v = localStorage.getItem(KEY);
    return VALID.includes(v) ? v : "auto";
  }

  /** 当前实际生效的是深色还是浅色 */
  function resolve(theme) {
    const t = theme || stored();
    if (t === "auto") return mq && mq.matches ? "light" : "dark";
    return t;
  }

  function paint() {
    const t = stored();
    const eff = resolve(t);
    const root = document.documentElement;
    root.dataset.theme = eff;
    root.dataset.themePref = t;
    root.style.colorScheme = eff;
    const meta = document.querySelector(META);
    if (meta) meta.setAttribute("content", COLOR[eff]);
    for (const fn of subs) { try { fn(eff, t); } catch (e) {} }
  }

  const subs = [];

  function set(theme) {
    if (!VALID.includes(theme)) return;
    localStorage.setItem(KEY, theme);
    paint();
  }

  window.THEME = {
    get pref() { return stored(); },
    get effective() { return resolve(); },
    set,
    onChange(fn) { subs.push(fn); },
  };

  // 主题要在第一帧之前落地，否则浅色用户会看到一帧黑底闪一下。
  // 页面里的 onclick 绑定等用户点了才跑，这里在 <head> 里同步执行即可。
  paint();

  if (mq) {
    const onSys = () => { if (stored() === "auto") paint(); };
    if (mq.addEventListener) mq.addEventListener("change", onSys);
    else if (mq.addListener) mq.addListener(onSys);
  }

  /** 一个三段的主题选择器，塞进任意容器（设置页用） */
  function mountSwitch(host) {
    if (!host) return null;
    const box = document.createElement("div");
    box.className = "themeswitch";
    const labels = { dark: "深色", light: "浅色", auto: "跟随系统" };
    box.innerHTML = Object.keys(labels).map(k =>
      `<button type="button" data-t="${k}">${labels[k]}</button>`).join("");
    const paintOn = () => box.querySelectorAll("button")
      .forEach(b => b.classList.toggle("on", b.dataset.t === stored()));
    box.querySelectorAll("button").forEach(b => { b.onclick = () => set(b.dataset.t); });
    host.appendChild(box);
    paintOn();
    subs.push(paintOn);
    return box;
  }

  window.THEME.mountSwitch = mountSwitch;

  /** 小圆按钮版（各页面右上角用）：点一下在深/浅之间切 */
  function mountToggle(host) {
    if (!host) return null;
    const b = document.createElement("button");
    b.type = "button";
    b.className = "icon-btn themebtn";
    b.title = "切换深浅色";
    b.innerHTML =
      '<svg class="moon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
      'stroke-linecap="round" stroke-linejoin="round">' +
      '<path d="M20.5 14.6A8.6 8.6 0 0 1 9.4 3.5a8.6 8.6 0 1 0 11.1 11.1z"/></svg>' +
      '<svg class="sun" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" ' +
      'stroke-linecap="round" stroke-linejoin="round"><circle cx="12" cy="12" r="4.2"/>' +
      '<path d="M12 2.6v2.2M12 19.2v2.2M2.6 12h2.2M19.2 12h2.2M5.4 5.4 7 7M17 17l1.6 1.6' +
      'M18.6 5.4 17 7M7 17l-1.6 1.6"/></svg>';
    b.onclick = () => set(resolve() === "light" ? "dark" : "light");
    host.appendChild(b);
    return b;
  }

  window.THEME.mountToggle = mountToggle;

  // 任何页面只要放了 id="theme-host" 就自动挂一个小圆按钮
  const auto = () => {
    const host = document.getElementById("theme-host");
    if (host && !host.childElementCount) mountToggle(host);
  };
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", auto);
  else auto();
})();
