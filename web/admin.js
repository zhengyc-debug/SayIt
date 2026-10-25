/* 后台人员管理。
 *
 * 页面本身在服务端已被 require_login 按 role 拦过（非 admin 进不来），
 * 这里的 403 只作为兜底提示 —— 比如管理员的角色刚被别人改掉。
 */
(() => {
  const $ = (id) => document.getElementById(id);
  const esc = (s) => String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));

  let me = '';
  let users = [];

  const toast = (msg, isErr) => {
    const el = $('toast');
    el.textContent = msg;
    el.classList.toggle('err', !!isErr);
    el.classList.add('show');
    clearTimeout(el._t);
    el._t = setTimeout(() => el.classList.remove('show'), 2600);
  };

  async function api(path, opts = {}) {
    const r = await fetch(path, {
      headers: { 'Content-Type': 'application/json' },
      ...opts,
    });
    if (r.status === 403) throw new Error('需要管理员权限');
    if (r.status === 401) { location.replace('/login?next=/admin'); throw new Error('未登录'); }
    let d = {};
    try { d = await r.json(); } catch (e) { /* 非 JSON */ }
    if (!r.ok) throw new Error(d.detail || `请求失败（${r.status}）`);
    return d;
  }

  function fmtDate(ts) {
    if (!ts) return '';
    const d = new Date(ts * 1000);
    const p = (n) => String(n).padStart(2, '0');
    return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}`;
  }

  function render() {
    $('count').textContent = `共 ${users.length} 人`;
    const box = $('list');
    if (!users.length) {
      box.innerHTML = '<div class="empty">还没有账号</div>';
      return;
    }
    box.innerHTML = users.map(u => {
      const isSelf = u.username === me;
      const off = !u.is_active;
      return `
        <div class="admin-row ${off ? 'off' : ''}">
          <div class="who">
            <div class="n">${esc(u.display_name || u.username)}
              ${u.role === 'admin' ? '<span class="badge admin">管理员</span>' : '<span class="badge">用户</span>'}
              ${off ? '<span class="badge off">已禁用</span>' : ''}
              ${isSelf ? '<span class="badge">我</span>' : ''}
            </div>
            <div class="m">账号 ${esc(u.username)} · 建于 ${fmtDate(u.created_at)}</div>
          </div>
          <div class="role-cell">
            <select data-act="role" data-user="${esc(u.username)}" ${isSelf ? 'disabled' : ''}>
              <option value="user" ${u.role === 'user' ? 'selected' : ''}>普通用户</option>
              <option value="admin" ${u.role === 'admin' ? 'selected' : ''}>管理员</option>
            </select>
          </div>
          <div class="admin-actions">
            <button class="btn sm" data-act="passwd" data-user="${esc(u.username)}">改密码</button>
            ${isSelf ? '' : `<button class="btn sm" data-act="toggle" data-user="${esc(u.username)}">
              ${off ? '启用' : '禁用'}</button>`}
          </div>
          <div class="admin-actions">
            ${isSelf ? '' : `<button class="btn sm danger" data-act="delete" data-user="${esc(u.username)}">删除</button>`}
          </div>
        </div>`;
    }).join('');
  }

  async function load() {
    const d = await api('/api/admin/users');
    me = d.me;
    users = d.users || [];
    render();
  }

  // ---- 表格上的操作（事件委托：列表重绘后不用重新绑定）----
  $('list').addEventListener('change', async (e) => {
    const sel = e.target.closest('select[data-act="role"]');
    if (!sel) return;
    try {
      await api(`/api/admin/users/${encodeURIComponent(sel.dataset.user)}`, {
        method: 'PATCH', body: JSON.stringify({ role: sel.value }),
      });
      toast('角色已更新');
      await load();
    } catch (err) {
      toast(err.message, true);
      await load();          // 回滚下拉框到真实状态
    }
  });

  $('list').addEventListener('click', async (e) => {
    const btn = e.target.closest('button[data-act]');
    if (!btn) return;
    const name = btn.dataset.user;
    const act = btn.dataset.act;
    try {
      if (act === 'passwd') {
        const pw = prompt(`给 ${name} 设置新密码（至少 6 位）：`);
        if (!pw) return;
        await api(`/api/admin/users/${encodeURIComponent(name)}`, {
          method: 'PATCH', body: JSON.stringify({ password: pw }),
        });
        toast('密码已更新');
      } else if (act === 'toggle') {
        const target = users.find(u => u.username === name);
        await api(`/api/admin/users/${encodeURIComponent(name)}`, {
          method: 'PATCH', body: JSON.stringify({ is_active: !(target && target.is_active) }),
        });
        toast(target && target.is_active ? '已禁用' : '已启用');
        await load();
      } else if (act === 'delete') {
        if (!confirm(`确定删除账号 ${name}？\nTA 的通话记录会保留，但无法再登录。`)) return;
        await api(`/api/admin/users/${encodeURIComponent(name)}`, { method: 'DELETE' });
        toast('已删除');
        await load();
      }
    } catch (err) {
      toast(err.message, true);
    }
  });

  // ---- 新增 ----
  $('c-gen').onclick = () => {
    const alphabet = 'ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789';
    let s = '';
    const buf = new Uint32Array(12);
    crypto.getRandomValues(buf);
    for (const n of buf) s += alphabet[n % alphabet.length];
    $('c-password').value = s;
    $('c-password').type = 'text';     // 生成的要能看见，否则用户抄不走
    toast('已生成密码，记得记下来');
  };

  $('create-form').addEventListener('submit', async (e) => {
    e.preventDefault();
    const btn = $('c-submit');
    btn.disabled = true;
    try {
      const d = await api('/api/admin/users', {
        method: 'POST',
        body: JSON.stringify({
          username: $('c-username').value.trim(),
          password: $('c-password').value,
          display_name: $('c-display').value.trim(),
          role: $('c-role').value,
        }),
      });
      toast(`已创建 ${d.user.username}`);
      $('create-form').reset();
      $('c-password').type = 'password';
      await load();
    } catch (err) {
      toast(err.message, true);
    } finally {
      btn.disabled = false;
    }
  });

  // 确认身份 → 是管理员才允许停留（页面级拦截已在服务端做过，这里是双保险）
  fetch('/api/auth-state').then(r => r.json()).then(d => {
    if (!d.logged_in) { location.replace('/login?next=/admin'); return; }
    if (!d.user || d.user.role !== 'admin') { location.replace('/'); return; }
    me = d.user.username;
    return load();
  }).catch(err => {
    $('list').innerHTML = `<div class="empty" style="color:var(--err-ink)">${esc(err.message)}</div>`;
  });
})();
