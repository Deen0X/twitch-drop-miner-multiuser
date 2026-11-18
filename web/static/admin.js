/* Admin page logic — extracted from admin.html (no Jinja variables). */
let users = [];
let deleteTarget = null;
let currentStatus = {};

async function loadUsers() {
  try {
    const resp = await fetch('/api/admin/users');
    const data = await resp.json();
    users = data.users || [];
    currentStatus = data.status || {};
    renderUsers();
  } catch (e) {
    document.getElementById('users-table').innerHTML = '<tr><td colspan="7" class="text-danger">Failed to load users</td></tr>';
  }
}

function renderUsers() {
  const tbody = document.getElementById('users-table');
  if (users.length === 0) {
    tbody.innerHTML = '<tr><td colspan="7" class="text-center">No users found</td></tr>';
    return;
  }
  let html = '';
  for (const u of users) {
    const ustatus = currentStatus[u.id];
    const running = ustatus && ustatus.running;
    const state = running ? 'running' : 'stopped';
    html += '<tr>';
    html += '<td class="mono">' + u.id + '</td>';
    html += '<td><a href="#" onclick="showUserPanel(' + u.id + ');return false;">' + u.username + '</a>' + (u.is_admin ? ' <span class="badge bg-primary">Admin</span>' : '') + '</td>';
    html += '<td>' + u.display_name + '</td>';
    html += '<td><span class="status-dot ' + state + '"></span> ' + (running ? state : 'stopped') + (u.is_active ? '' : ' (disabled)') + '</td>';
    html += '<td>' + (u.created_at ? u.created_at.split('T')[0] : '-') + '</td>';
    html += '<td>' + (u.last_login ? u.last_login.split('T')[0] : 'Never') + '</td>';
    html += '<td class="actions-cell">';
    html += '<button class="btn btn-sm btn-primary me-1" onclick="showUserPanel(' + u.id + ')">View</button>';
    html += '<button class="btn btn-sm btn-info me-1" onclick="generateCode(' + u.id + ')">Code</button>';
    html += '<button class="btn btn-sm btn-secondary me-1" onclick="showChangePasswordModal(' + u.id + ')">Password</button>';
    if (u.is_admin) {
      html += '<button class="btn btn-sm btn-secondary me-1" disabled>Disable</button>';
      html += '<button class="btn btn-sm btn-warning me-1" onclick="restartUser(' + u.id + ')">Restart</button>';
      html += '<button class="btn btn-sm btn-danger me-1" onclick="stopUser(' + u.id + ')">Stop</button>';
      html += '<button class="btn btn-sm btn-secondary me-1" disabled>Delete</button>';
    } else {
      html += '<button class="btn btn-sm btn-' + (u.is_active ? 'warning' : 'success') + ' me-1" onclick="toggleUser(' + u.id + ')">' + (u.is_active ? 'Disable' : 'Enable') + '</button>';
      if (u.is_active) {
        html += '<button class="btn btn-sm btn-warning me-1" onclick="restartUser(' + u.id + ')">Restart</button>';
        html += '<button class="btn btn-sm btn-danger me-1" onclick="stopUser(' + u.id + ')">Stop</button>';
      }
      html += '<button class="btn btn-sm btn-danger" onclick="showDeleteUserModal(' + u.id + ')">Delete</button>';
    }
    html += '</td></tr>';
  }
  tbody.innerHTML = html;
}

function showCreateUserModal() {
  document.getElementById('new-username').value = '';
  document.getElementById('new-password').value = '';
  document.getElementById('new-display').value = '';
  DNXWebUI.openModal('createUserModal');
}

async function createUser() {
  const username = document.getElementById('new-username').value.trim();
  const password = document.getElementById('new-password').value;
  const display = document.getElementById('new-display').value.trim() || username;
  if (!username || !password) return;
  try {
    const formData = new URLSearchParams();
    formData.append('username', username);
    formData.append('password', password);
    formData.append('display_name', display);
    const resp = await fetch('/api/admin/users', {
      method: 'POST',
      headers: {'Content-Type': 'application/x-www-form-urlencoded'},
      body: formData
    });
    const data = await resp.json();
    if (resp.ok) {
      DNXWebUI.closeModal('createUserModal');
      loadUsers();
    } else {
      alert('Error: ' + (data.detail || JSON.stringify(data)));
    }
  } catch (e) {
    alert('Error: ' + e);
  }
}

function showDeleteUserModal(userId) {
  deleteTarget = userId;
  const u = users.find(x => x.id === userId);
  document.getElementById('delete-username').textContent = u ? u.username : userId;
  DNXWebUI.openModal('deleteUserModal');
}

function showChangePasswordModal(userId) {
  document.getElementById('change-password-user-id').value = userId;
  document.getElementById('change-password-new').value = '';
  DNXWebUI.openModal('changePasswordModal');
}

async function changePassword() {
  const userId = document.getElementById('change-password-user-id').value;
  const newPass = document.getElementById('change-password-new').value;
  if (!newPass || newPass.length < 4) {
    alert('Password must be at least 4 characters');
    return;
  }
  try {
    const formData = new URLSearchParams();
    formData.append('new_password', newPass);
    const resp = await fetch('/api/admin/users/' + userId + '/change-password', {
      method: 'POST',
      body: formData
    });
    if (resp.ok) {
      DNXWebUI.closeModal('changePasswordModal');
      loadUsers();
    } else {
      const data = await resp.json();
      alert(data.detail || 'Error changing password');
    }
  } catch (e) {
    alert('Error: ' + e);
  }
}

async function confirmDeleteUser() {
  if (!deleteTarget) return;
  try {
    await fetch('/api/admin/users/' + deleteTarget, {method: 'DELETE'});
    DNXWebUI.closeModal('deleteUserModal');
    deleteTarget = null;
    loadUsers();
  } catch (e) {
    alert('Error: ' + e);
  }
}

async function startUser(userId) {
  await fetch('/api/admin/users/' + userId + '/start', {method: 'POST'});
  setTimeout(loadUsers, 500);
}

async function restartUser(userId) {
  await fetch('/api/admin/users/' + userId + '/stop', {method: 'POST'});
  await new Promise(r => setTimeout(r, 1000));
  await fetch('/api/admin/users/' + userId + '/start', {method: 'POST'});
  setTimeout(loadUsers, 1000);
}

async function stopUser(userId) {
  await fetch('/api/admin/users/' + userId + '/stop', {method: 'POST'});
  setTimeout(loadUsers, 500);
}

async function toggleUser(userId) {
  await fetch('/api/admin/users/' + userId + '/toggle', {method: 'POST'});
  setTimeout(loadUsers, 500);
}

async function generateCode(userId) {
  try {
    const resp = await fetch('/api/admin/users/' + userId + '/generate-code', {method: 'POST'});
    const data = await resp.json();
    if (data.success) {
      document.getElementById('code-user').textContent = 'User ID: ' + userId;
      document.getElementById('code-display').textContent = data.user_code;
      document.getElementById('code-link').href = data.verification_uri;
      document.getElementById('code-link').textContent = data.verification_uri;
      DNXWebUI.openModal('codeModal');
    } else {
      alert('Error: ' + (data.error || 'Failed to generate code'));
    }
  } catch (e) {
    alert('Error: ' + e);
  }
}

function shortSha(s) {
  return s ? String(s).slice(0, 8) : '—';
}

function fillAutoHours() {
  const sel = document.getElementById('auto-hour');
  if (!sel) return;
  let html = '';
  for (let h = 0; h < 24; h++) {
    const v = (h < 10 ? '0' : '') + h + ':00';
    html += '<option value="' + h + '">' + v + '</option>';
  }
  sel.innerHTML = html;
}

async function loadUpdateConfig() {
  try {
    const resp = await fetch('/api/admin/update-config');
    const cfg = await resp.json();
    const chk = document.getElementById('auto-update');
    const hour = document.getElementById('auto-hour');
    if (chk) chk.checked = !!cfg.enabled;
    if (hour && cfg.hour !== undefined) hour.value = String(cfg.hour);
  } catch (e) {
    console.error('update config:', e);
  }
}

function renderUpdate(data) {
  const up = data.upstream || {};
  const our = data.ours || {};
  const cfg = data.config || {};

  document.getElementById('up-sha').textContent = shortSha(up.sha);
  document.getElementById('our-sha').textContent = shortSha(our.sha);

  document.getElementById('up-info').textContent = up.ok
    ? (up.update_available ? 'Update available' : 'Up to date') +
      ' · ' + String(up.date || '').slice(0, 10) + ' · applied ' + shortSha(up.applied_sha)
    : 'Error: ' + (up.error || 'unknown');

  document.getElementById('our-info').textContent = our.ok
    ? (our.update_available ? 'Update available' : 'Up to date') +
      ' · ' + String(our.date || '').slice(0, 10) + ' · applied ' + shortSha(our.applied_sha)
    : 'Error: ' + (our.error || 'unknown');

  const available = !!(up.update_available || our.update_available);
  const badge = document.getElementById('update-badge');
  badge.textContent = available ? 'update available' : 'up to date';
  badge.className = 'badge ' + (available ? 'warn' : 'ok');
  document.getElementById('apply-update-btn').disabled = !available;

  if (document.getElementById('auto-update')) {
    document.getElementById('auto-update').checked = !!cfg.enabled;
  }
  if (document.getElementById('auto-hour') && cfg.hour !== undefined) {
    document.getElementById('auto-hour').value = String(cfg.hour);
  }
}

async function checkUpdate() {
  const badge = document.getElementById('update-badge');
  badge.textContent = 'checking...';
  badge.className = 'badge info';
  try {
    const resp = await fetch('/api/admin/check-update');
    const data = await resp.json();
    renderUpdate(data);
    document.getElementById('update-info').innerHTML =
      '<span class="muted small">Auto-update: ' +
      (data.config && data.config.last_check ? 'last check ' + data.config.last_check + ' · ' + (data.config.last_result || '') : 'no checks yet') +
      '</span>';
  } catch (e) {
    badge.textContent = 'check failed';
    badge.className = 'badge err';
    document.getElementById('update-info').innerHTML = '<div class="alert err">Error: ' + e + '</div>';
  }
}

async function saveAutoUpdate() {
  const enabled = document.getElementById('auto-update').checked;
  const hour = parseInt(document.getElementById('auto-hour').value || '4', 10);
  try {
    await fetch('/api/admin/update-config', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({enabled: enabled, hour: hour})
    });
    DNXWebUI.toast('Auto-update ' + (enabled ? 'enabled' : 'disabled'), 'ok');
  } catch (e) {
    DNXWebUI.toast('Could not save auto-update: ' + e, 'err');
  }
}

function showUpdateProgress(show) {
  const el = document.getElementById('update-progress');
  if (el) el.style.display = show ? 'block' : 'none';
}

async function pollUpdateProgress() {
  try {
    const resp = await fetch('/api/admin/update-progress');
    const p = await resp.json();
    showUpdateProgress(true);
    const pct = Math.max(0, Math.min(100, p.percent || 0));
    document.getElementById('update-progress-bar').style.width = pct + '%';
    document.getElementById('update-stage').textContent = (p.message || p.stage || '—');
    document.getElementById('update-percent').textContent = Math.round(pct) + '%';
    if (p.active) {
      setTimeout(pollUpdateProgress, 700);
      return;
    }
    const btn = document.getElementById('apply-update-btn');
    btn.textContent = 'Update now';
    if (p.success) {
      document.getElementById('update-info').innerHTML =
        '<div class="alert ok">' + (p.message || 'Done') + '. Restarting the web app...</div>';
      setTimeout(function () { location.reload(); }, 9000);
    } else {
      btn.disabled = false;
      document.getElementById('update-info').innerHTML =
        '<div class="alert err">' + (p.message || 'Update failed') +
        ' (stage: ' + (p.stage || '?') + ')</div>' +
        (p.log ? '<pre>' + String(p.log).replace(/</g, '&lt;') + '</pre>' : '');
    }
  } catch (e) {
    setTimeout(pollUpdateProgress, 1500);
  }
}

async function syncUpdateProgress() {
  try {
    const resp = await fetch('/api/admin/update-progress');
    const p = await resp.json();
    if (p.active) {
      const btn = document.getElementById('apply-update-btn');
      btn.disabled = true;
      btn.textContent = 'Applying...';
      showUpdateProgress(true);
      pollUpdateProgress();
    }
  } catch (e) {}
}

async function applyUpdate() {
  if (!confirm('This will download the latest miner, apply our web patch, verify it in staging and restart the app. Continue?')) return;
  const btn = document.getElementById('apply-update-btn');
  btn.disabled = true;
  btn.textContent = 'Applying...';
  document.getElementById('update-info').innerHTML =
    '<div class="alert info">Starting update...</div>';
  showUpdateProgress(true);
  try {
    const resp = await fetch('/api/admin/apply-update', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({})
    });
    const result = await resp.json();
    if (!result.started) {
      btn.disabled = false;
      btn.textContent = 'Update now';
      document.getElementById('update-info').innerHTML =
        '<div class="alert err">' + (result.message || 'Could not start update') + '</div>';
      return;
    }
    pollUpdateProgress();
  } catch (e) {
    btn.disabled = false;
    btn.textContent = 'Update now';
    document.getElementById('update-info').innerHTML = '<div class="alert err">Error: ' + e + '</div>';
  }
}

function esc(value) {
  return String(value == null ? '' : value).replace(/[&<>"']/g, function (c) {
    return { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c];
  });
}

function panelStat(k, v, s) {
  return '<div class="stat"><div class="k">' + esc(k) + '</div>' +
    '<div class="v" style="font-size:16px;">' + esc(v) + '</div>' +
    '<div class="s">' + esc(s || '') + '</div></div>';
}

function panelChart(chart) {
  if (!chart || !chart.bars) return '';
  var rects = chart.bars.map(function (b) {
    var h = (b.h || 0) * 0.36;
    return '<rect class="chart-bar" x="' + b.x + '" y="' + (40 - h) + '" width="' + b.w +
      '" height="' + h + '" rx="0.6"><title>' + esc(b.label) + ': ' + b.count + '</title></rect>';
  }).join('');
  return '<svg class="chart" viewBox="0 0 100 40" preserveAspectRatio="none">' + rects + '</svg>';
}

function panelChips(list) {
  if (!list || !list.length) return '<span class="muted small">None</span>';
  return list.map(function (x) { return '<span class="badge" style="margin:2px 4px 2px 0;">' + esc(x) + '</span>'; }).join('');
}

async function showUserPanel(userId) {
  DNXWebUI.openModal('userPanelModal');
  document.getElementById('up-title').textContent = 'User overview';
  const body = document.getElementById('user-panel-body');
  body.innerHTML = '<div class="muted small">Loading...</div>';
  try {
    const resp = await fetch('/api/admin/users/' + userId + '/overview');
    const data = await resp.json();
    if (!resp.ok) {
      body.innerHTML = '<div class="alert err">' + esc(data.detail || 'Error') + '</div>';
      return;
    }
    renderUserPanel(data);
  } catch (e) {
    body.innerHTML = '<div class="alert err">Error: ' + esc(e) + '</div>';
  }
}

function renderUserPanel(data) {
  const u = data.user || {};
  const s = data.snapshot || {};
  const pr = data.progress || {};
  const inv = data.inventory || {};
  const st = data.settings || {};
  const prog = pr.current_campaign || null;

  document.getElementById('up-title').textContent =
    (u.display_name || u.username || 'User') + ' (' + (u.username || '') + ')';

  let pct = 0;
  if (prog && prog.required_minutes) {
    pct = Math.min(100, Math.round(((prog.current_minutes || 0) / prog.required_minutes) * 100));
  }

  const history = (data.history || []).slice(0, 10).map(function (h) {
    return '<div class="drop-item">' +
      (h.image ? '<img class="benefit-img" src="' + esc(h.image) + '" onerror="this.style.display=\'none\'">' : '') +
      '<div style="flex:1;min-width:0;"><div style="font-size:.85rem;">' + esc(h.drop) + '</div>' +
      '<div class="campaign-time">' + esc(h.campaign) + '</div></div>' +
      '<div class="campaign-time mono" style="white-space:nowrap;">' +
      esc(String(h.detected_at || '').slice(0, 16).replace('T', ' ')) + '</div></div>';
  }).join('') || '<div class="muted small">No claimed drops recorded.</div>';

  const progressHtml = prog
    ? '<div class="d-flex justify-content-between gap-2"><span>' + esc(prog.name || '') + '</span>' +
      '<span class="muted small">' + esc(prog.game || '') + '</span></div>' +
      '<div class="progress-bar-custom" style="margin-top:6px;"><div class="progress-bar-fill" style="width:' + pct + '%"></div></div>' +
      '<div class="muted small" style="margin-top:4px;">' + pct + '% · ' +
      esc(prog.current_minutes || 0) + ' / ' + esc(prog.required_minutes || 0) + ' min</div>'
    : '<div class="muted small">No current campaign.</div>';

  document.getElementById('user-panel-body').innerHTML =
    '<div class="grid g4" style="margin-bottom:12px;">' +
      panelStat('Status', s.running ? 'Running' : 'Stopped', s.state || '') +
      panelStat('Campaigns', s.campaigns_count || 0, s.campaign_name || '') +
      panelStat('Claimed', data.history_total || 0, 'drops') +
      panelStat('Current game', (prog && prog.game) ? prog.game : '—', '') +
    '</div>' +
    '<div class="card" style="margin-bottom:12px;"><div class="card-h"><h2>Progress</h2></div>' + progressHtml + '</div>' +
    '<div class="grid g2" style="margin-bottom:12px;">' +
      '<div class="card"><div class="card-h"><h2>Claimed drops</h2><div class="grow"></div>' +
        '<span class="badge info">last 14 days: ' + ((data.chart && data.chart.total) || 0) + '</span></div>' +
        panelChart(data.chart) + '</div>' +
      '<div class="card"><div class="card-h"><h2>Inventory</h2></div>' +
        '<div class="muted small">Campaigns: ' + esc(inv.campaigns || 0) + ' · Active: ' + esc(inv.active || 0) +
        ' · Upcoming: ' + esc(inv.upcoming || 0) + '</div>' +
        '<div class="muted small">Drops claimed: ' + esc(inv.claimed_drops || 0) + ' / ' + esc(inv.total_drops || 0) + '</div>' +
      '</div>' +
    '</div>' +
    '<div class="card" style="margin-bottom:12px;"><div class="card-h"><h2>Recent history</h2><div class="grow"></div>' +
      '<span class="badge">' + esc(data.history_total || 0) + ' total</span></div>' + history + '</div>' +
    '<div class="grid g2">' +
      '<div class="card"><div class="card-h"><h2>Priority</h2></div>' + panelChips(st.priority) + '</div>' +
      '<div class="card"><div class="card-h"><h2>Excluded</h2></div>' + panelChips(st.exclude) + '</div>' +
    '</div>';
}

document.addEventListener('dnx:refresh', function () { loadUsers(); checkUpdate(); });
setInterval(loadUsers, 10000);
fillAutoHours();
loadUpdateConfig();
checkUpdate();
syncUpdateProgress();
loadUsers();
