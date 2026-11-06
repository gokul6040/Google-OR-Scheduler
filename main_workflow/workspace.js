/* Authentication for opt-in main operations; no test keys or fixture setup. */
const workspaceInfo = window.therapyWorkspace || {apiPrefix:'/workflow', dataSource:'main'};
const marker = document.createElement('div');
marker.className = 'test-workspace-marker';
marker.textContent = workspaceInfo.dataSource === 'test'
  ? `Main application · Existing test database · Messaging ${workspaceInfo.live ? 'enabled' : 'OFF'}`
  : 'Main application · Daily operations';
document.getElementById('mainContent').prepend(marker);
if (workspaceInfo.localNoAuth || workspaceInfo.requiresAccessKey === false) {
  sessionStorage.removeItem('mainWorkflowAdminToken');
} else {
const login = document.createElement('dialog');
login.className = 'test-workspace-login';
login.innerHTML = '<form><h2>Open daily operations</h2><p>Enter the operations access key.</p><label>Access key<input type="password" autocomplete="off" required></label><p role="alert"></p><button class="primary-button" type="submit">Open operations</button></form>';
document.body.append(login);
const showLogin = () => { if (!login.open) login.showModal(); };
document.addEventListener('test-workspace-login', showLogin);
login.addEventListener('cancel', event => event.preventDefault());
login.querySelector('form').onsubmit = async event => {
  event.preventDefault();
  const key = login.querySelector('input').value.trim();
  try {
    const response = await fetch(`${workspaceInfo.apiPrefix}/api/overview`, {headers:{Authorization:`Bearer ${key}`}});
    if (!response.ok) throw new Error('The access key was not accepted, or operations setup needs attention.');
    sessionStorage.setItem('mainWorkflowAdminToken', key);
    location.reload();
  } catch (error) { login.querySelector('[role="alert"]').textContent = error.message; }
};
if (!sessionStorage.getItem('mainWorkflowAdminToken')) showLogin();
}
