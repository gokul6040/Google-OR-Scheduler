/* Cookie authentication; no passwords or session tokens in browser storage. */
(() => {
  const originalFetch = window.fetch.bind(window);
  window.fetch = (input, options = {}) => {
    const url = new URL(typeof input === 'string' ? input : input.url, location.href);
    const method = (options.method || input.method || 'GET').toUpperCase();
    if (url.origin === location.origin && !['GET', 'HEAD'].includes(method)) {
      const headers = new Headers(options.headers || input.headers);
      headers.set('X-Portal-Request', '1');
      options = {...options, headers};
    }
    return originalFetch(input, options);
  };
  const escape = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
  async function api(path, body) {
    const response = await fetch(path, body === undefined ? {} : {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || 'Could not complete the request.');
    return data;
  }
  async function logout() { await api('/api/auth/logout', {}); location.assign('/login'); }
  function accountMenu() {
    return `<div class="portal-account-menu"><button type="button" class="portal-account-trigger" aria-label="Account options" aria-haspopup="menu" aria-expanded="false" aria-controls="portalAccountMenu"><svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.6"/><circle cx="12" cy="12" r="1.6"/><circle cx="19" cy="12" r="1.6"/></svg></button><div class="portal-account-dropdown" id="portalAccountMenu" role="menu" hidden><button type="button" role="menuitem" id="portalLogout">Sign out</button><p class="portal-menu-error" role="alert" hidden></p></div></div>`;
  }
  function bindAccountMenu() {
    const wrapper = document.querySelector('.portal-account-menu');
    if (!wrapper) {
      const signOut = document.querySelector('#doctorSignOut');
      if (!signOut) return;
      signOut.onclick = async () => {
        const error = document.querySelector('#doctorSignOutError');
        error.hidden = true; signOut.disabled = true;
        try { await logout(); }
        catch (failure) { error.textContent = failure.message; error.hidden = false; signOut.disabled = false; }
      };
      return;
    }
    const trigger = wrapper.querySelector('.portal-account-trigger');
    const menu = wrapper.querySelector('[role="menu"]');
    const signOut = wrapper.querySelector('#portalLogout');
    const close = (restoreFocus = false) => {
      menu.hidden = true; trigger.setAttribute('aria-expanded', 'false');
      if (restoreFocus) trigger.focus();
    };
    const open = () => {
      menu.hidden = false; trigger.setAttribute('aria-expanded', 'true'); signOut.focus();
    };
    trigger.onclick = () => menu.hidden ? open() : close(true);
    wrapper.addEventListener('keydown', event => {
      if (event.key === 'Escape') { event.preventDefault(); close(true); }
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') { event.preventDefault(); open(); }
    });
    wrapper.addEventListener('focusout', event => { if (!wrapper.contains(event.relatedTarget)) close(); });
    document.addEventListener('click', event => { if (!wrapper.contains(event.target)) close(); });
    signOut.onclick = async () => {
      const error = wrapper.querySelector('[role="alert"]');
      error.hidden = true; signOut.disabled = true;
      try { await logout(); }
      catch (failure) { error.textContent = failure.message; error.hidden = false; signOut.disabled = false; }
    };
  }
  function navigation(user) {
    if (user.role === 'doctor') {
      return '<a href="/doctor">My sessions</a><a href="/account">Change password</a><button type="button" class="portal-direct-signout" id="doctorSignOut">Sign out</button><p class="portal-menu-error" id="doctorSignOutError" role="alert" hidden></p>';
    }
    return `${user.role === 'admin' ? '<a href="/users">Users</a><a href="/users/existing">Existing users</a>' : '<a href="/doctor">My sessions</a>'} <a href="/account">Change password</a> ${accountMenu()}`;
  }
  function mountSidebar(user) {
    const nav = document.querySelector('.sidebar .primary-nav');
    const links = [
      ['/users', 'Users', '<svg aria-hidden="true"><use href="#i-patients"/></svg>'],
      ['/users/existing', 'Existing users', '<svg aria-hidden="true"><use href="#i-team"/></svg>'],
      ['/account', 'Change password', '<svg viewBox="0 0 24 24" aria-hidden="true" fill="none" stroke="currentColor" stroke-width="1.7"><rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/></svg>'],
    ];
    nav.insertAdjacentHTML('beforeend', links.map(([href, text, icon]) => `<a class="nav-item${location.pathname === href ? ' active' : ''}" ${location.pathname === href ? 'aria-current="page"' : ''} href="${href}">${icon}<span>${text}</span></a>`).join(''));
    const label = document.querySelector('.user-chip strong'); if (label) label.textContent = user.name;
    const email = document.querySelector('.user-chip small'); if (email) { email.textContent = user.email; email.title = user.email; }
    const avatar = document.querySelector('.user-chip .avatar'); if (avatar) avatar.textContent = user.name.split(/\s+/).map(word => word[0]).slice(0, 2).join('');
    const chip = document.querySelector('.user-chip');
    chip.querySelector(':scope > svg')?.remove();
    chip.insertAdjacentHTML('beforeend', accountMenu());
    bindAccountMenu();
    if (main) document.querySelector('#menuButton')?.addEventListener('click', () => document.querySelector('.sidebar').classList.toggle('open'));
    const breadcrumb = document.querySelector('#portalBreadcrumb');
    if (breadcrumb) breadcrumb.textContent = links.find(([href]) => href === location.pathname)?.[1] || 'Account';
  }
  const main = document.querySelector('#portalMain');
  let signedInUser = null;
  function formAction(form, action) {
    form.addEventListener('submit', async event => {
      event.preventDefault();
      const button = form.querySelector('button[type="submit"]');
      const error = form.querySelector('[role="alert"]');
      error.textContent = ''; button.disabled = true;
      try { await action(Object.fromEntries(new FormData(form))); }
      catch (failure) { error.textContent = failure.message; }
      finally { button.disabled = false; }
    });
  }
  function passwordFields(change, setup = false) {
    return `${change ? `<label>${setup ? 'Temporary password' : 'Current or temporary password'}<input name="current" type="password" autocomplete="current-password" ${setup ? 'placeholder="Enter the password from your admin"' : ''} required maxlength="128"></label>` : '<label>Email<input name="email" type="email" autocomplete="username" required maxlength="254"></label>'}
      <label>${change ? 'New password' : 'Password'}<input name="password" type="password" autocomplete="${change ? 'new-password' : 'current-password'}" ${setup ? 'placeholder="Enter a new password"' : ''} required ${change ? 'minlength="8" aria-describedby="passwordRule"' : ''} maxlength="128">${change ? '<small class="portal-field-hint" id="passwordRule">At least 8 characters, no spaces, with 1 uppercase letter, 1 lowercase letter and 1 special character (e.g. ! @ # $).</small>' : ''}</label>
      ${change ? `<label>Confirm new password<input name="confirm" type="password" autocomplete="new-password" ${setup ? 'placeholder="Re-enter your new password"' : ''} required maxlength="128"></label>` : ''}
      <p class="portal-error" role="alert"></p><button class="primary-button" type="submit">${change ? 'Save password' : 'Sign in'}</button>`;
  }
  function showPasswordChange(setup = false) {
    document.title = `${setup ? 'Choose your password' : 'Change password'} · EIC Therapy Scheduler`;
    if (setup) {
      document.body.classList.add('portal-password-setup');
      document.querySelector('.portal-header')?.remove();
      main.innerHTML = `<section class="portal-password-setup-card" aria-labelledby="passwordSetupTitle">
        <header class="portal-password-setup-heading"><h1 id="passwordSetupTitle">Choose your password</h1><p>Keep your account secure with a password only you know.</p></header>
        <form class="portal-form portal-password-setup-form">${passwordFields(true, true)}</form>
        <p class="portal-password-setup-note">After saving, sign in again with your new password.</p>
      </section>`;
    } else {
      main.innerHTML = `<div class="page-heading"><div><h1>Change password</h1><p class="portal-page-description">Keep your account secure with a password only you know.</p></div></div><section class="panel portal-password-panel"><div class="panel-body"><form class="portal-form">${passwordFields(true)}</form><p class="fine-print">After saving, sign in again with your new password.</p></div></section>`;
    }
    formAction(main.querySelector('form'), async values => {
      if (values.password !== values.confirm) throw new Error('The new passwords do not match.');
      await api('/api/auth/password', values); location.assign('/login');
    });
  }
  function showLogin() {
    document.body.classList.add('portal-login-page');
    document.title = 'User Login · EIC Therapy Scheduler';
    main.classList.add('portal-login-main');
    const emailIcon = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="3" y="5" width="18" height="14" rx="2"/><path d="m3 6 9 7 9-7"/></svg>';
    const lockIcon = '<svg viewBox="0 0 24 24" aria-hidden="true"><rect x="5" y="10" width="14" height="11" rx="2"/><path d="M8 10V7a4 4 0 0 1 8 0v3"/><path d="M12 14v3"/></svg>';
    const eyeIcon = visible => '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12Z"/><circle cx="12" cy="12" r="3"/>' + (visible ? '<path d="m3 3 18 18"/>' : '') + '</svg>';
    main.innerHTML = `<svg class="portal-login-waves" viewBox="0 0 1680 960" preserveAspectRatio="none" aria-hidden="true"><path fill="#bddfc9" opacity=".75" d="M1180 0h500v345c-158-20-260-4-349 33-177 74-243-134-151-378Z"/><path fill="#9ccfab" opacity=".5" d="M1570 0h110v382c-140-14-260 9-323 43-158 84-209-23-180-105C1224 185 1401 70 1570 0Z"/><path fill="#d6ecdf" opacity=".9" d="M0 650c185-45 298 87 476 189 222 127 358 79 532 68 268-17 438-110 672-245v298H0Z"/><path fill="#a9d6b9" opacity=".56" d="M0 834c221-87 390 67 596 103 205 36 291-16 435-16 245 0 429-119 649-184v223H0Z"/></svg><img class="portal-login-tree" src="/login-tree.png" alt="" aria-hidden="true" width="1536" height="1024" decoding="async"><section class="portal-login-card" aria-labelledby="loginTitle"><h1 id="loginTitle">User Login</h1><form class="portal-login-form">
      <label for="loginEmail">Email</label><div class="portal-login-field">${emailIcon}<input id="loginEmail" name="email" type="email" autocomplete="username" placeholder="Enter your email" required maxlength="254"></div>
      <label for="loginPassword">Password</label><div class="portal-login-field">${lockIcon}<input id="loginPassword" name="password" type="password" autocomplete="current-password" placeholder="Enter your password" required maxlength="128"><button type="button" class="portal-password-toggle" aria-label="Show password" aria-pressed="false">${eyeIcon(false)}</button></div>
      <div class="portal-login-forgot"><button type="button" id="forgotPassword" aria-controls="passwordResetHelp" aria-expanded="false">Forgot Password?</button></div><p id="passwordResetHelp" class="portal-reset-help" role="status" hidden>Contact admin to reset the password.</p>
      <p class="portal-error" role="alert"></p><button class="portal-sign-in" type="submit">Sign In</button></form></section>`;
    const tree = main.querySelector('.portal-login-tree');
    const pauseBreeze = () => { tree.style.animationPlayState = document.hidden ? 'paused' : 'running'; };
    document.addEventListener('visibilitychange', pauseBreeze);
    pauseBreeze();
    const password = document.querySelector('#loginPassword');
    const toggle = document.querySelector('.portal-password-toggle');
    toggle.onclick = () => {
      const visible = password.type === 'password';
      password.type = visible ? 'text' : 'password';
      toggle.setAttribute('aria-label', visible ? 'Hide password' : 'Show password');
      toggle.setAttribute('aria-pressed', String(visible));
      toggle.innerHTML = eyeIcon(visible);
    };
    document.querySelector('#forgotPassword').onclick = event => {
      document.querySelector('#passwordResetHelp').hidden = false;
      event.currentTarget.setAttribute('aria-expanded', 'true');
    };
    formAction(main.querySelector('form'), async values => {
      const result = await api('/api/auth/login', values);
      location.assign(result.user.mustChange ? '/account' : result.user.role === 'admin' ? '/' : '/doctor');
    });
  }
  // Searchable clinician picker: type to filter by name, therapy or centre instead of a page-long list.
  function clinicianPicker(clinicians, selectedId = null) {
    const current = clinicians.find(d => d.id === selectedId);
    return `<div class="clinician-picker">
      <input type="hidden" name="clinicianId" value="${current ? current.id : ''}">
      <input type="search" class="clinician-search" role="combobox" aria-expanded="false" aria-autocomplete="list" autocomplete="off" spellcheck="false" placeholder="Search by name, therapy or centre" value="${current ? escape(current.name) : ''}">
      <ul class="clinician-results" role="listbox" hidden></ul>
    </div>`;
  }
  // Centre: a small searchable list like Clinicians; "All centres" (empty value) shows everyone.
  function centreField(clinicians, selectedId = null) {
    const current = clinicians.find(d => d.id === selectedId)?.location || '';
    return `<label class="clinician-centre-label">Centre<div class="centre-picker">
      <input type="hidden" class="clinician-centre" value="${escape(current)}">
      <input type="search" class="centre-search" role="combobox" aria-expanded="false" aria-autocomplete="list" autocomplete="off" spellcheck="false" placeholder="All centres" value="${escape(current)}">
      <ul class="clinician-results" role="listbox" hidden></ul>
    </div></label>`;
  }
  function bindCentrePicker(root, clinicians) {
    const picker = root.querySelector('.centre-picker');
    if (!picker) return;
    const centres = [...new Set(clinicians.map(d => d.location).filter(Boolean))].sort((a, b) => a.localeCompare(b));
    const hidden = picker.querySelector('.clinician-centre');
    const search = picker.querySelector('.centre-search');
    const list = picker.querySelector('.clinician-results');
    const listId = 'centreResults' + Math.random().toString(36).slice(2, 8);
    list.id = listId; search.setAttribute('aria-controls', listId);
    let matches = [], active = -1;
    const close = () => { list.hidden = true; search.setAttribute('aria-expanded', 'false'); search.removeAttribute('aria-activedescendant'); active = -1; };
    const choose = value => {
      const changed = hidden.value !== value;
      hidden.value = value; search.value = value; close();
      if (changed) hidden.dispatchEvent(new Event('change'));
    };
    const render = () => {
      const words = search.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
      matches = ['', ...centres.filter(c => words.every(w => c.toLowerCase().includes(w)))];
      list.innerHTML = matches.map((c, i) => `<li role="option" id="${listId}-${i}" data-index="${i}" aria-selected="${c === hidden.value}"><strong>${c ? escape(c) : 'All centres'}</strong></li>`).join('')
        + (matches.length === 1 && words.length ? '<li class="clinician-empty">No centre matches.</li>' : '');
      list.hidden = false; search.setAttribute('aria-expanded', 'true');
      active = -1;
    };
    const highlight = index => {
      active = index;
      list.querySelectorAll('[role="option"]').forEach((item, i) => item.classList.toggle('is-active', i === index));
      const item = list.querySelector(`#${listId}-${index}`);
      if (item) { search.setAttribute('aria-activedescendant', item.id); item.scrollIntoView({block: 'nearest'}); }
    };
    search.addEventListener('focus', () => { search.select(); render(); });
    search.addEventListener('input', render);
    search.addEventListener('keydown', event => {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        if (list.hidden) render();
        highlight((active + (event.key === 'ArrowDown' ? 1 : matches.length - 1)) % matches.length);
      } else if (event.key === 'Enter' && !list.hidden && active >= 0) {
        event.preventDefault(); choose(matches[active]);
      } else if (event.key === 'Escape' && !list.hidden) {
        event.preventDefault(); event.stopPropagation(); close();
      }
    });
    list.addEventListener('mousedown', event => event.preventDefault());
    list.addEventListener('click', event => {
      const item = event.target.closest('[role="option"]');
      if (item) choose(matches[Number(item.dataset.index)]);
    });
    search.addEventListener('blur', () => { close(); search.value = hidden.value; }); // typed text without a pick is discarded
  }
  function bindClinicianPicker(root, clinicians, taken = new Set()) {
    const picker = root.querySelector('.clinician-picker');
    if (!picker) return;
    const hidden = picker.querySelector('input[name="clinicianId"]');
    const search = picker.querySelector('.clinician-search');
    const list = picker.querySelector('.clinician-results');
    const listId = 'clinicianResults' + Math.random().toString(36).slice(2, 8);
    list.id = listId; search.setAttribute('aria-controls', listId);
    const centre = root.querySelector('.clinician-centre');
    let matches = [], active = -1;
    const close = () => { list.hidden = true; search.setAttribute('aria-expanded', 'false'); search.removeAttribute('aria-activedescendant'); active = -1; };
    const choose = d => {
      if (!d || taken.has(d.id)) return;
      hidden.value = d.id; search.value = d.name; close();
    };
    const highlight = index => {
      active = index;
      list.querySelectorAll('[role="option"]').forEach((item, i) => item.classList.toggle('is-active', i === index));
      const item = list.children[index];
      if (item?.id) { search.setAttribute('aria-activedescendant', item.id); item.scrollIntoView({block: 'nearest'}); }
    };
    const render = () => {
      const words = search.value.trim().toLowerCase().split(/\s+/).filter(Boolean);
      const place = centre ? centre.value : '';
      matches = clinicians.filter(d => (!place || d.location === place) && words.every(w => `${d.name} ${d.department} ${d.location}`.toLowerCase().includes(w))).slice(0, 1000);
      list.innerHTML = matches.length ? matches.map((d, i) => `<li role="option" id="${listId}-${i}" data-index="${i}" aria-disabled="${taken.has(d.id)}" aria-selected="${String(d.id) === hidden.value}" class="${taken.has(d.id) ? 'is-taken' : ''}"><strong>${escape(d.name)}</strong><small>${escape([d.department, d.location].filter(Boolean).join(' · '))}${taken.has(d.id) ? ' · has an account' : ''}</small></li>`).join('')
        : `<li class="clinician-empty">No active clinician matches${centre && centre.value ? ' at this centre' : ''}.</li>`;
      list.hidden = false; search.setAttribute('aria-expanded', 'true');
      active = -1;
    };
    search.addEventListener('focus', render);
    centre?.addEventListener('change', () => {
      // A clinician from another centre is no longer a valid choice.
      const picked = clinicians.find(d => String(d.id) === hidden.value);
      if (picked && centre.value && picked.location !== centre.value) { hidden.value = ''; search.value = ''; }
      if (!list.hidden) render();
    });
    search.addEventListener('input', () => { hidden.value = ''; render(); });
    search.addEventListener('keydown', event => {
      if (event.key === 'ArrowDown' || event.key === 'ArrowUp') {
        event.preventDefault();
        if (list.hidden) render();
        if (!matches.length) return;
        highlight((active + (event.key === 'ArrowDown' ? 1 : matches.length - 1)) % matches.length);
      } else if (event.key === 'Enter' && !list.hidden && active >= 0) {
        event.preventDefault(); choose(matches[active]);
      } else if (event.key === 'Escape' && !list.hidden) {
        event.preventDefault(); event.stopPropagation(); close();
      }
    });
    list.addEventListener('mousedown', event => event.preventDefault()); // keep focus while clicking a result
    list.addEventListener('click', event => {
      const item = event.target.closest('[role="option"]');
      if (item) choose(matches[Number(item.dataset.index)]);
    });
    search.addEventListener('blur', () => {
      close();
      // A typed name that wasn't picked from the list must not keep an old selection.
      const picked = clinicians.find(d => String(d.id) === hidden.value);
      search.value = picked ? picked.name : '';
    });
  }
  const USERS_PAGE_SIZE = 25;
  const userList = {query: '', page: 1};
  async function showUsers(existing = false) {
    const data = await api('/api/users');
    const clinicianName = id => data.clinicians.find(d => d.id === id)?.name || 'Inactive clinician';
    main.innerHTML = `<div class="page-heading"><div><h1>${existing ? 'Existing users' : 'Users'}</h1><p class="portal-page-description">${existing ? 'Manage portal access and reset account passwords.' : 'Add an admin or give a clinician access to their daily sessions.'}</p></div></div>
      <div id="temporary" role="status"></div>${!existing ? `<section class="panel portal-form-panel"><div class="panel-header"><div><h2>Add user</h2><p>Choose the access this person needs.</p></div></div><div class="panel-body"><form class="portal-form portal-form-grid" id="addUser">
      <label>Name<input name="name" autocomplete="name" required maxlength="120"></label>
      <label>Email<input name="email" type="email" autocomplete="email" required maxlength="254"></label>
      <label>Role<select name="role"><option value="doctor">Doctor</option><option value="admin">Admin</option></select></label>
      <span id="centreField" class="portal-field-slot">${centreField(data.clinicians)}</span><label id="clinicianLabel">Clinicians as per Zoho Record${clinicianPicker(data.clinicians)}</label>
      <div class="portal-form-footer"><p class="fine-print">A temporary password will be generated. The user must change it at first sign-in.</p><p class="portal-error" role="alert"></p><button type="submit" class="primary-button">Create account</button></div></form></div></section>` : `
      <p id="userError" class="portal-error" role="alert"></p><div class="portal-users-toolbar"><label class="search-field portal-user-search"><svg aria-hidden="true"><use href="#i-search"/></svg><input id="userSearch" type="search" placeholder="Search by name, email, role or clinician" aria-label="Search users" autocomplete="off"></label><span class="soft-pill" id="userMatchCount" role="status" aria-live="polite"></span></div><div class="portal-table-wrap"><table class="portal-table"><thead><tr><th>User</th><th>Role</th><th>Status</th><th>Actions</th></tr></thead><tbody id="userRows"></tbody></table><div class="table-footer portal-table-footer"><span id="userPageSummary"></span><div><button class="ghost-button" type="button" id="userPrev">Previous</button><button class="ghost-button" type="button" id="userNext">Next</button></div></div></div>`}`;
    const form = document.querySelector('#addUser');
    if (form) form.elements.role.onchange = () => {
      const doctor = form.elements.role.value === 'doctor';
      document.querySelector('#clinicianLabel').hidden = !doctor;
      document.querySelector('#centreField').hidden = !doctor;
      form.elements.clinicianId.disabled = !doctor;
      form.querySelector('.clinician-search').disabled = !doctor;
      form.querySelector('.centre-search').disabled = !doctor;
    };
    if (form) bindCentrePicker(form, data.clinicians);
    if (form) bindClinicianPicker(form, data.clinicians, new Set(data.users.filter(u => u.clinicianId).map(u => u.clinicianId)));
    const showSecret = secret => {
      if (!secret) return;
      const box = document.querySelector('#temporary');
      box.className = 'portal-secret';
      box.innerHTML = `Temporary password — share it privately with the user. It is shown only now.<code>${escape(secret)}</code><button class="secondary-button" id="dismissSecret">Dismiss</button>`;
      document.querySelector('#dismissSecret').onclick = () => { box.textContent = ''; box.className = ''; };
      box.scrollIntoView({block: 'center'});
    };
    if (form) formAction(form, async values => {
      if (values.role === 'doctor' && !values.clinicianId) throw new Error('Choose a clinician from the search list.');
      const result = await api('/api/users', {...values, clinicianId: values.clinicianId ? Number(values.clinicianId) : null});
      await showUsers(); showSecret(result.temporaryPassword);
    });
    // Existing users: A-Z, 25 per page, searchable; the search and page survive Edit/Reset/Deactivate.
    const rowHtml = u => `<tr><td><strong>${escape(u.name)}</strong><small>${escape(u.email)}</small></td><td><span class="soft-pill portal-role">${escape(u.role)}</span>${u.role === 'doctor' ? `<small>${u.clinicianId ? escape(clinicianName(u.clinicianId)) : 'No clinician linked'}</small>` : ''}</td><td><span class="status-pill ${u.active ? 'status-confirmed' : 'status-cancelled_other'}">${u.active ? 'Active' : 'Inactive'}</span>${u.mustChange ? '<small>Password change required</small>' : ''}</td><td class="portal-user-actions"><button class="secondary-button" data-edit="${u.id}">Edit</button><button class="secondary-button" data-id="${u.id}" data-action="reset">Reset password</button><button class="secondary-button" data-id="${u.id}" data-action="${u.active ? 'deactivate' : 'activate'}">${u.active ? 'Deactivate' : 'Activate'}</button></td></tr>`;
    const renderRows = () => {
      const tbody = document.querySelector('#userRows');
      if (!tbody) return;
      const words = userList.query.trim().toLowerCase().split(/\s+/).filter(Boolean);
      const matches = data.users.filter(u => words.every(w => `${u.name} ${u.email} ${u.role} ${u.clinicianId ? clinicianName(u.clinicianId) : ''}`.toLowerCase().includes(w)))
        .sort((a, b) => a.name.localeCompare(b.name, undefined, {sensitivity: 'base'}) || a.email.localeCompare(b.email));
      const pages = Math.max(1, Math.ceil(matches.length / USERS_PAGE_SIZE));
      userList.page = Math.max(1, Math.min(userList.page, pages));
      const start = (userList.page - 1) * USERS_PAGE_SIZE;
      tbody.innerHTML = matches.slice(start, start + USERS_PAGE_SIZE).map(rowHtml).join('')
        || `<tr><td colspan="4">${data.users.length ? 'No users match your search.' : 'No user accounts to display.'}</td></tr>`;
      document.querySelector('#userMatchCount').textContent = `${matches.length} account${matches.length === 1 ? '' : 's'}`;
      document.querySelector('#userPageSummary').textContent = `Showing ${matches.length ? start + 1 : 0}–${Math.min(start + USERS_PAGE_SIZE, matches.length)} of ${matches.length}`;
      document.querySelector('#userPrev').disabled = userList.page === 1;
      document.querySelector('#userNext').disabled = userList.page === pages;
      bindRowActions();
    };
    const bindRowActions = () => {
    // Edit: correct name, email or clinician link (e.g. hand a test account to the real doctor).
    main.querySelectorAll('[data-edit]').forEach(button => button.onclick = () => {
      main.querySelector('.portal-edit-row')?.remove();
      const u = data.users.find(item => String(item.id) === button.dataset.edit);
      const taken = new Set(data.users.filter(other => other.id !== u.id && other.clinicianId).map(other => other.clinicianId));
      const row = document.createElement('tr');
      row.className = 'portal-edit-row';
      row.innerHTML = `<td colspan="4"><form class="portal-form portal-form-grid portal-edit-form" aria-label="Edit ${escape(u.name)}">
        <label>Name<input name="name" value="${escape(u.name)}" autocomplete="off" required maxlength="120"></label>
        <label>Email<input name="email" type="email" value="${escape(u.email)}" autocomplete="off" required maxlength="254"></label>
        ${u.role === 'doctor' ? `${centreField(data.clinicians, u.clinicianId)}<label>Clinicians as per Zoho Record${clinicianPicker(data.clinicians, u.clinicianId)}</label>` : ''}
        <div class="portal-form-footer"><p class="fine-print">Saving signs this account out everywhere. Use Reset password to give the person a new temporary password.</p><p class="portal-error" role="alert"></p><button type="button" class="ghost-button" data-cancel-edit>Cancel</button><button type="submit" class="primary-button">Save changes</button></div></form></td>`;
      button.closest('tr').after(row);
      bindCentrePicker(row, data.clinicians);
      bindClinicianPicker(row, data.clinicians, taken);
      row.querySelector('[data-cancel-edit]').onclick = () => row.remove();
      row.querySelector('input[name="name"]').focus();
      formAction(row.querySelector('form'), async values => {
        if (u.role === 'doctor' && u.active && !values.clinicianId) throw new Error('Choose a clinician from the search list.');
        const result = await api(`/api/users/${u.id}/edit`, {...values, clinicianId: values.clinicianId ? Number(values.clinicianId) : null});
        if (result.reauthenticate) {
          main.innerHTML = '<h1>Account updated</h1><p>Your sessions have ended. <a href="/login">Sign in again</a> with your updated email.</p>';
          return;
        }
        await showUsers(true);
        const note = document.querySelector('#temporary');
        note.className = 'portal-saved'; note.textContent = `Saved changes for ${values.name}.`;
      });
    });
    main.querySelectorAll('[data-action]').forEach(button => button.onclick = async () => {
      if (!confirm(`${button.textContent} for this user? Existing sessions will be signed out.`)) return;
      button.disabled = true;
      try {
        const result = await api(`/api/users/${button.dataset.id}/${button.dataset.action}`, {});
        if (result.reauthenticate) {
          main.innerHTML = '<h1>Account updated</h1><div id="temporary" role="status"></div><p>Your sessions have ended. <a href="/login">Sign in again</a> when ready.</p>';
          showSecret(result.temporaryPassword);
          return;
        }
        await showUsers(true); showSecret(result.temporaryPassword);
      } catch (error) { document.querySelector('#userError').textContent = error.message; button.disabled = false; }
    });
    };
    const search = document.querySelector('#userSearch');
    if (search) {
      search.value = userList.query;
      search.addEventListener('input', () => { userList.query = search.value; userList.page = 1; renderRows(); });
      document.querySelector('#userPrev').onclick = () => { userList.page--; renderRows(); };
      document.querySelector('#userNext').onclick = () => { userList.page++; renderRows(); };
      renderRows();
    }
  }
  async function showDoctor(day = 'today', view = 'schedule') {
    const data = await api('/api/doctor/schedule?day=' + day);
    document.body.classList.add('portal-doctor-page');
    document.querySelector('.portal-header > a')?.remove();
    document.title = 'My sessions · Schedule';
    const cancelled = data.cancellations || [];
    const rows = view === 'cancellations' ? cancelled : data.sessions;
    const dateLabel = new Intl.DateTimeFormat('en-IN', {weekday: 'long', day: 'numeric', month: 'long', year: 'numeric'}).format(new Date(data.date + 'T12:00:00'));
    const statuses = {CONFIRMED: 'Confirmed', PENDING_CONFIRMATION: 'Pending confirmation', CANCELLED_PATIENT: 'Cancelled by patient', CANCELLED_OTHER: 'Cancelled', NEEDS_RESCHEDULING: 'Cancelled'};
    const greeting = `Hi, ${signedInUser?.name?.trim() || 'there'}`;
    const introduction = view === 'cancellations' ? `Here are your cancellations for ${day}.` : `Here's your schedule for ${day}.`;
    main.innerHTML = `<div class="doctor-heading"><div><h1>${escape(greeting)}</h1><p>${escape(introduction)}<span class="doctor-date">${escape(dateLabel)}</span></p></div><div class="doctor-day-switch" role="group" aria-label="Schedule day"><button id="today" aria-pressed="${day === 'today'}">Today</button><button id="tomorrow" aria-pressed="${day === 'tomorrow'}">Tomorrow</button></div></div>
      <section class="doctor-schedule" data-view="${view}" aria-label="Daily sessions"><div class="doctor-view-tabs" role="group" aria-label="Appointment view"><button id="doctorSchedule" aria-pressed="${view === 'schedule'}">Schedule <span>${data.sessions.length}</span></button><button id="doctorCancellations" aria-pressed="${view === 'cancellations'}">Cancellations <span>${cancelled.length}</span></button></div>
      ${rows.length ? `<div class="doctor-table-scroll"><table class="portal-table doctor-table"><caption class="doctor-table-caption">${view === 'cancellations' ? 'Cancelled appointments' : 'Confirmed and pending appointments'} for ${escape(dateLabel)}</caption><thead><tr><th scope="col">Time</th><th scope="col">Patient</th><th scope="col">Therapy</th><th scope="col">Location</th><th scope="col">Status</th></tr></thead><tbody>${rows.map(s => `<tr><td class="doctor-time">${escape(s.time)}</td><td><strong>${escape(s.patient)}</strong><small>${escape(s.code)}</small></td><td>${escape(s.therapy)}</td><td class="doctor-location">${escape(s.location)}</td><td><span class="doctor-status ${s.status === 'CONFIRMED' ? 'is-confirmed' : ''}">${escape(statuses[s.status] || s.status.replaceAll('_', ' ').toLowerCase())}</span></td></tr>`).join('')}</tbody></table></div>` : `<div class="doctor-empty"><h2>${view === 'cancellations' ? 'No cancellations' : 'No sessions scheduled'}</h2><p>${view === 'cancellations' ? 'There are no cancelled appointments for this day.' : 'Confirmed and pending appointments for this day will appear here.'}</p></div>`}</section>`;
    for (const name of ['today', 'tomorrow']) document.getElementById(name).onclick = () => showDoctor(name, view).catch(showError);
    document.querySelector('#doctorSchedule').onclick = () => showDoctor(day, 'schedule').catch(showError);
    document.querySelector('#doctorCancellations').onclick = () => showDoctor(day, 'cancellations').catch(showError);
  }
  function showError(error) { if (main) main.textContent = error.message + ' Refresh to try again.'; }
  async function start() {
    const {user} = await api('/api/auth/me');
    signedInUser = user;
    if (!main) {
      if (!user || user.mustChange || user.role !== 'admin') { location.assign('/login'); return; }
      mountSidebar(user);
      return;
    }
    if (!user) {
      showLogin();
      return;
    }
    if (user.mustChange) {
      showPasswordChange(true);
      return;
    }
    if (document.querySelector('.sidebar')) mountSidebar(user);
    else { document.querySelector('#accountNav').innerHTML = navigation(user); bindAccountMenu(); }
    if (location.pathname === '/account') showPasswordChange();
    else if (user.role === 'doctor') await showDoctor();
    else if (location.pathname === '/users' || location.pathname === '/users/existing') await showUsers(location.pathname === '/users/existing');
    else location.replace('/');
  }
  start().catch(showError);
})();
