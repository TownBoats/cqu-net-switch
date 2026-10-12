'use strict';
const $ = id => document.getElementById(id);
let token = '', profiles = [], status = null, stale = true, busy = false, submitting = false;
let refreshing = false, pendingAction = null, pollTimer, refreshTimer, lastOperation = '', profileRenderKey = '';
const clock = date => new Date(date).toLocaleTimeString('zh-CN', {hour12:false});
$('today').textContent = new Date().toLocaleDateString('zh-CN', {month:'long',day:'numeric',weekday:'long'});

async function api(path, body) {
  const response = await fetch(path, {method:body === undefined ? 'GET' : 'POST',
    headers:{'Content-Type':'application/json', 'X-App-Token':token},
    ...(body === undefined ? {} : {body:JSON.stringify(body)}), signal:AbortSignal.timeout(18000)});
  const result = await response.json();
  if (!response.ok) throw new Error(result.error || '请求未完成，请重试。');
  return result;
}
function showError(error) {
  $('error').textContent = error.name === 'TimeoutError' ? '本机服务响应超时，请重试。' : error.message || '无法连接本机服务。';
  $('error').hidden = false;
}
function feedback(message) { $('feedback').textContent = message; $('feedback').hidden = !message; }
function controls() {
  $('refresh').disabled = refreshing || busy || submitting;
  $('refresh').textContent = refreshing ? '↻ 正在查询' : '↻ 刷新状态';
  $('logout').disabled = busy || submitting || stale || !status?.online;
  $('add-account').disabled = busy || submitting;
  $('add-current').disabled = busy || submitting || stale || !status?.account;
  $('save-account').disabled = busy || submitting;
  $('confirm-yes').disabled = busy || submitting;
  document.querySelectorAll('.account-actions button').forEach(button => { button.disabled = busy || submitting; });
}
function paintStatus() {
  const online = !stale && status?.online;
  $('status-badge').className = 'badge ' + (stale ? 'unknown' : online ? 'online' : '');
  $('status-label').textContent = stale ? '状态未知' : online ? '校园网已连接' : '未登录';
  $('current-name').textContent = stale ? '暂时无法确认连接' : online ? status.name || status.account : '校园网尚未登录';
  $('current-account').textContent = stale ? '请检查网络后重新查询' : online ? status.account : '选择下方账号，开始连接';
  $('status-caption').textContent = stale ? '查询失败，不代表账号已下线' : online ? '已由认证门户确认在线' : '已由认证门户确认离线';
  ['ipv4','ipv6','mac'].forEach(key => { $(key).textContent = status?.[key] && status[key] !== '::' ? status[key] : '—'; });
  $('checked-at').textContent = status ? `${stale ? '上次成功' : '更新于'} ${clock(status.checkedAt)}` : '尚未成功查询';
  // Statistics are portal aggregates; never synthesize session counters or rates.
  const usage = !stale && online ? status.bytes : null;
  const gib = usage !== null && usage >= 1024 ** 3;
  $('usage').textContent = usage === null ? '—' : (usage / 1024 ** (gib ? 3 : 2)).toFixed(2);
  $('usage-unit').textContent = usage === null ? '' : gib ? 'GB' : 'MB';
  const minutes = !stale && online ? status.minutes : null;
  $('duration').textContent = minutes === null ? '—' : String(Math.floor(minutes / 60));
  $('duration-unit').textContent = minutes === null ? '' : `小时 ${minutes % 60} 分`;
  paintProfiles(); controls();
}
async function refresh() {
  if (refreshing || busy) return;
  refreshing = true; controls();
  try { const fresh = await api('/api/status'); if (!busy) { status = fresh; stale = false; $('error').hidden = true; } }
  catch(error) { stale = true; showError(error); }
  finally { refreshing = false; if (!busy) paintStatus(); controls(); }
}
function el(tag, className, text) {
  const node = document.createElement(tag); node.className = className; if (text !== undefined) node.textContent = text; return node;
}
function actionButton(text, className, action) {
  const button = el('button', className, text); button.type = 'button'; button.addEventListener('click', action); return button;
}
function paintProfiles() {
  const renderKey = JSON.stringify([profiles, stale, status?.online, status?.account]);
  if (renderKey === profileRenderKey) { controls(); return; }
  profileRenderKey = renderKey;
  const list = $('account-list'); list.replaceChildren();
  $('profile-count').textContent = $('account-counter').textContent = profiles.length;
  $('empty-accounts').hidden = !!profiles.length;
  for (const profile of profiles) {
    const online = !stale && status?.online && status.account === profile.account;
    const row = el('div', 'account-row');
    row.append(el('div', 'account-avatar', Array.from(profile.label)[0] || '网'));
    const details = el('div','account-details'); details.append(el('h3','',profile.label), el('p','',profile.account)); row.append(details);
    if (online) row.append(el('span','account-online','● 当前在线'));
    const actions = el('div','account-actions');
    actions.append(actionButton('编辑','text-button', () => openForm(profile)), actionButton('移除','text-button', () => ask(`移除「${profile.label}」？`, '仅删除本机保存的信息，不会注销当前网络连接。', '确认移除', () => mutate('/api/profiles/delete', {id:profile.id}))));
    if (!online) actions.append(actionButton(status?.online && !stale ? '切换到此账号 ↗' : '连接此账号 ↗','button secondary', () => ask(`连接「${profile.label}」`, `目标账号：${profile.account}。如有账号在线，将先注销；网络会短暂中断。`, '确认连接', () => mutate('/api/switch',{id:profile.id}))));
    row.append(actions); list.append(row);
  }
  controls();
}
function openForm(profile) {
  $('profile-form').reset(); $('form-error').hidden = true;
  $('profile-id').value = profile?.id || '';
  $('profile-label').value = profile?.label || '';
  $('profile-account').value = profile?.account || '';
  $('profile-password').required = !profile?.id;
  $('profile-password').placeholder = profile?.id ? '留空保留原密码' : '请输入密码';
  $('password-hint').textContent = profile?.id ? '密码留空则保持不变；更换账号时需要重新填写密码。' : '密码仅加密保存在本机，不会在列表中显示。';
  $('form-title').textContent = profile?.id ? '编辑账号' : '添加一个校园网账号';
  $('profile-form').hidden = false; $('profile-form').scrollIntoView({block:'nearest'}); $('profile-label').focus();
}
function closeForm() { $('profile-form').hidden = true; $('profile-form').reset(); }
function ask(title, detail, yes, action) {
  pendingAction = action; $('confirm-title').textContent = title; $('confirm-detail').textContent = detail; $('confirm-yes').textContent = yes;
  $('confirmation').hidden = false; $('confirmation').scrollIntoView({block:'center'}); $('confirm-cancel').focus();
}
function cancel() { pendingAction = null; $('confirmation').hidden = true; }
async function mutate(path, body) {
  submitting = true; controls(); $('error').hidden = true; feedback('');
  try { const state = await api(path, body); acceptState(state); return true; }
  catch (error) { showError(error); return false; }
  finally { submitting = false; controls(); }
}
function acceptState(state) {
  profiles = state.profiles; const wasBusy = busy; busy = state.operation.state === 'running';
  const op = state.operation;
  const key = JSON.stringify(op);
  if (key !== lastOperation && op.state !== 'idle') {
    feedback(op.message); lastOperation = key;
    $('feedback').classList.toggle('error', op.state === 'error');
  }
  if (busy) {
    stale = true; paintStatus();
    $('status-label').textContent = '连接变更中';
    $('current-name').textContent = '正在更新校园网连接';
    $('current-account').textContent = '请等待认证门户确认';
    $('status-caption').textContent = '正在处理，请等待结果';
  }
  paintProfiles();
  const list = $('event-list'); list.replaceChildren();
  if (!state.events.length) list.append(el('li','event-empty','还没有操作记录。连接和账号管理操作会显示在这里。'));
  for (const event of state.events) {
    const item = el('li',''); item.dataset.kind = event.kind;
    const timestamp = el('time','',clock(event.at)); timestamp.dateTime = event.at;
    item.append(timestamp,el('span','',event.message)); list.append(item);
  }
  clearTimeout(pollTimer);
  pollTimer = setTimeout(pollState, busy ? 1000 : 5000);
  if (wasBusy && !busy) refresh();
}
async function pollState() {
  try { acceptState(await api('/api/state')); }
  catch (error) { stale = true; paintStatus(); showError(error); pollTimer = setTimeout(pollState,5000); }
}
function scheduleRefresh() {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(async () => {
    if ($('auto-refresh').checked && !document.hidden) await refresh();
    scheduleRefresh();
  },15000);
}
$('refresh').addEventListener('click',refresh);
$('auto-refresh').addEventListener('change',scheduleRefresh);
$('add-account').addEventListener('click',() => openForm());
$('add-current').addEventListener('click',() => openForm({account:status?.account,label:status?.name}));
$('close-form').addEventListener('click',closeForm);
$('logout').addEventListener('click',() => ask('断开当前校园网连接？','注销后，这台设备需要重新登录校园网才能继续访问网络。','确认断开',() => mutate('/api/logout',{})));
$('confirm-cancel').addEventListener('click',cancel);
$('confirm-yes').addEventListener('click',async () => { const action = pendingAction; cancel(); if (action) await action(); });
$('profile-form').addEventListener('submit',async event => {
  event.preventDefault(); if (submitting || busy) return;
  const data = {id:$('profile-id').value, label:$('profile-label').value, account:$('profile-account').value, password:$('profile-password').value};
  const ok = await mutate('/api/profiles/save',data);
  if (ok) { closeForm(); feedback('账号已保存在本机。'); }
  else { $('form-error').textContent = $('error').textContent; $('form-error').hidden = false; }
});
document.querySelectorAll('nav a').forEach(link => link.addEventListener('click',() => {
  document.querySelectorAll('nav a').forEach(item => item.classList.toggle('active',item === link));
}));
document.addEventListener('visibilitychange',() => { if (!document.hidden && $('auto-refresh').checked) refresh(); });
(async () => {
  try { const state = await api('/api/bootstrap'); token = state.token; acceptState(state); await refresh(); scheduleRefresh(); }
  catch(error) { stale = true; paintStatus(); showError(error); }
})();
