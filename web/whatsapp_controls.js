/* Shared drawer/Operations components for test and explicitly enabled main workflows. */
window.therapyWhatsapp = (() => {
  const esc = value => String(value ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#039;'}[c]));
  const preview = data => data.mode.startsWith('PREVIEW');
  const label = value => String(value || 'Not reported').toLowerCase().replaceAll('_', ' ').replace(/\b\w/g, c => c.toUpperCase());
  const action = (title, kind, attrs = '', disabled = false) => `<button type="button" class="secondary-button compact-button" data-wa-action="${kind}" ${attrs} ${disabled ? 'disabled' : ''}>${esc(title)}</button>`;
  const table = (headers, rows, extraClass = '') => `<div class="wa-table ${esc(extraClass)}"><table class="data-table"><thead><tr>${headers.map(h => `<th>${esc(h)}</th>`).join('')}</tr></thead><tbody>${rows.length ? rows.map(row => `<tr>${row.map(cell => `<td>${cell}</td>`).join('')}</tr>`).join('') : `<tr><td colspan="${headers.length}">Nothing yet.</td></tr>`}</tbody></table></div>`;
  const post = (api, path, body) => api(path, {method:'POST', body:JSON.stringify(body)});
  const readiness = (data, patient) => data.messaging_paused ? 'Messaging is OFF — offers remain queued' : preview(data) && data.test_data ? 'Preview — no messages sent' : data.workspace === 'main' && !data.recipients[patient] ? 'Import a verified parent number before publishing' : preview(data) ? 'Preview — no messages sent' : !data.recipients[patient] ? 'Recipient not configured' : data.message_mode === 'session' && !data.windows[patient]?.open ? `${data.recipients[patient]} · offer will wait for a parent message` : `${data.recipients[patient]} · ready to receive`;
  const ready = (data, patient) => (preview(data) && (data.test_data || data.workspace !== 'main')) || Boolean(data.recipients[patient]);

  function offerCard(offer, data, scope) {
    const fieldId = `wa-reply-${scope}-${offer.token}`;
    const reply = {YES:'Yes — visit confirmed', NO:offer.state === 'DECLINED' ? 'Unavailable for the whole day' : 'Cannot attend received — booking still reserved; staff review', VISIT:'Cannot attend this visit', DAY:'Unavailable that day', HELP:'Help requested — see inbox', RESCHEDULE:'Reschedule requested — see inbox'}[offer.last_reply] || (offer.assumed_accepted ? 'Assumed accepted at staff cutoff — no parent reply' : ({UNKNOWN:'Delivery unknown — staff review',UNSUBSCRIBED:'Parent unsubscribed — not sent; contact another way',NEEDS_ATTENTION:'Needs staff follow-up',BLOCKED:'Sending paused — configuration review',RETRY_WAIT:'Temporary rejection — retry scheduled'}[offer.send_status] || (['QUEUED','WAITING_FOR_CONTACT','WAITING_FOR_SYNC','WAITING_FOR_ENABLE','PROCESSING'].includes(offer.send_status) ? 'Offer not sent yet' : 'Awaiting parent reply')));
    const deadline = offer.expires_at ? `Reply deadline: ${new Date(offer.expires_at).toLocaleString('en-IN',{timeZone:'Asia/Kolkata'})} IST` : offer.state === 'PENDING' ? 'The reply window starts when this offer is sent.' : '';
    const simulate = preview(data) && (data.allow_simulation || data.workspace !== 'main') && ['PENDING','CONFIRMED'].includes(offer.state)
      ? `<div class="wa-preview-reply"><div class="wa-reply-field"><label for="${esc(fieldId)}">Preview reply for ${esc(offer.patient)}</label><select id="${esc(fieldId)}" data-wa-reply="${esc(offer.token)}">${[['YES','Yes'],['NO','Cannot attend — whole day'],['HELP','Help'],['DAY','Unavailable that day'],['RESCHEDULE','Request reschedule']].map(([value,title]) => `<option value="${value}">${title}</option>`).join('')}</select></div>${action('Simulate reply','simulate',`data-token="${esc(offer.token)}"`)}</div>` : '';
    return `<article class="wa-offer"><div class="wa-offer-heading"><strong>${esc(offer.patient)}</strong><span class="soft-pill">${esc(label(offer.state))}</span></div><dl class="wa-status-grid"><div><dt>Sending</dt><dd>${esc(label(offer.send_status))}</dd></div><div><dt>Delivery receipt</dt><dd>${esc(preview(data) ? 'Preview only' : label(offer.delivery))}</dd></div><div><dt>Parent reply</dt><dd>${esc(reply)}</dd></div></dl>${offer.send_error ? `<p class="${offer.send_status === 'WAITING_FOR_CONTACT' ? 'fine-print' : 'wa-error'}">${esc(offer.send_error)}</p>` : ''}${deadline ? `<p class="fine-print">${esc(deadline)}</p>` : ''}<small class="wa-reference">Reference: ${esc(offer.token)}</small>${offer.message_preview ? `<details><summary>View offered message</summary><pre>${esc(offer.message_preview)}</pre></details>` : ''}${simulate}</article>`;
  }

  function appointmentActions(item, data) {
    if (item.status === 'PROPOSED') return `${action(preview(data) ? 'Publish preview offer' : 'Publish WhatsApp offer','publish',`data-id="${item.id}"`,!ready(data,item.patient))}<small>${esc(readiness(data,item.patient))}</small>`;
    if (item.status === 'CONFIRMED') return `<div class="wa-actions">${action('Completed','attendance',`data-id="${item.id}" data-status="COMPLETED"`)}${action('No-show','attendance',`data-id="${item.id}" data-status="NO_SHOW"`)}</div>`;
    return `<span>${esc(['COMPLETED','NO_SHOW'].includes(item.status) ? label(item.status) : 'Attendance available after confirmation')}</span>`;
  }

  function controller(body, options, render, active, getDate, notice) {
    let data = null, busy = false, timer = null, signature = '';
    const valid = () => body.isConnected && active();
    function paint(next) {
      const focus = body.contains(document.activeElement) ? document.activeElement : null;
      const filter = focus?.dataset.waFilter;
      const selection = filter && focus.tagName === 'INPUT' ? [focus.selectionStart, focus.selectionEnd] : null;
      const selections = new Map([...body.querySelectorAll('[data-wa-reply]')].map(el => [el.dataset.waReply,el.value]));
      const expanded = new Set([...body.querySelectorAll('details[data-wa-detail][open]')].map(el => el.dataset.waDetail));
      body.innerHTML = render(next);
      body.querySelectorAll('details[data-wa-detail]').forEach(el => { el.open = expanded.has(el.dataset.waDetail); });
      body.querySelectorAll('[data-wa-reply]').forEach(el => { if (selections.has(el.dataset.waReply)) el.value = selections.get(el.dataset.waReply); });
      if (filter) {
        const field = [...body.querySelectorAll('[data-wa-filter]')].find(el => el.dataset.waFilter === filter);
        field?.focus();
        if (selection) field?.setSelectionRange(...selection);
      }
    }
    async function refresh(force = false) {
      if (!valid()) return;
      try {
        const next = await options.api(`/api/state?date=${encodeURIComponent(getDate())}`);
        if (!valid()) return;
        const previousAppointments = data && JSON.stringify(data.appointments);
        data = next;
        const serialized = JSON.stringify(next);
        const editing = body.contains(document.activeElement) && document.activeElement.matches('input,select,textarea');
        if (force || (signature !== serialized && !editing)) {
          paint(next);
          signature = serialized;
        }
        if (!busy && previousAppointments && previousAppointments !== JSON.stringify(next.appointments)) await options.onChanged();
      } catch (error) { if (valid()) notice(error.message, true); }
    }
    async function mutate(button) {
      if (busy || !data) return;
      const kind = button.dataset.waAction;
      busy = true;
      body.querySelectorAll('button').forEach(el => el.disabled = true);
      try {
        let result;
        if (kind === 'publish') result = await post(options.api,'/api/publish',{date:getDate(), ...(button.dataset.id ? {appointment_id:Number(button.dataset.id)} : {})});
        if (kind === 'simulate') {
          const selected = [...body.querySelectorAll('[data-wa-reply]')].find(el => el.dataset.waReply === button.dataset.token);
          result = await post(options.api,'/api/simulate',{token:button.dataset.token,action:selected.value});
        }
        if (kind === 'attendance') result = await post(options.api,'/api/attendance',{id:Number(button.dataset.id),status:button.dataset.status});
        if (kind === 'resume-sending') result = await post(options.api,'/api/resume-sending',{});
        if (kind === 'resolve') result = await post(options.api,'/api/resolve',{id:Number(button.dataset.id)});
        if (kind === 'process-inbound') result = await post(options.api,'/api/process',{});
        if (preview(data)) await post(options.api,'/api/process',{});
        await refresh(true);
        if (valid()) notice(kind === 'resolve' ? 'Request marked as resolved.' : kind === 'publish' ? `${result.offers} visit offer(s) ${preview(data) ? 'published in preview. No messages sent.' : 'queued for WhatsApp.'}` : 'Saved. Replies, attendance and weekly progress are updated.');
        if (kind !== 'resolve') await options.onChanged();
      } catch (error) { if (valid()) notice(error.message, true); }
      finally {
        busy = false;
        if (valid()) { signature = ''; await refresh(true); }
      }
    }
    body.addEventListener('click', event => {
      const button = event.target.closest('[data-wa-action]');
      if (button && body.contains(button)) mutate(button);
    });
    function poll() {
      timer = setTimeout(async () => {
        if (!valid()) return;
        if (!busy) await refresh();
        if (valid()) poll();
      }, 5000);
    }
    refresh(true).then(poll);
    return {refresh:() => !busy && refresh(true), redraw:() => { if (data && !busy) paint(data); }, stop:() => clearTimeout(timer)};
  }

  let drawerController = null, operationsController = null, operationsDate = null;

  function mountAppointment(options) {
    drawerController?.stop();
    const {root,item} = options;
    root.innerHTML = '<h3>WhatsApp & attendance</h3><p class="wa-notice" role="status"></p><div class="wa-body">Loading visit status…</div>';
    const notice = (message,error=false) => { const el = root.querySelector('.wa-notice'); el.textContent = message; el.classList.toggle('wa-error',error); };
    drawerController = controller(root.querySelector('.wa-body'),options,data => {
      const appointment = data.appointments.find(a => a.id === item.id);
      if (!appointment) return '<p>Appointment is no longer on this date. Refresh the calendar.</p>';
      const offers = data.offers.filter(o => o.appointment_ids.includes(item.id));
      return `<p class="wa-mode">${esc(data.mode)}</p><p class="wa-explanation">${esc(readiness(data,appointment.patient))}. Publishing offers the child’s complete continuous visit.</p><div class="wa-drawer-actions">${appointmentActions(appointment,data)}</div>${offers.map(o => offerCard(o,data,'appointment')).join('') || '<p class="fine-print">No WhatsApp offer has been published for this visit.</p>'}`;
    }, () => Boolean(root.closest('.drawer.open')), () => item.date, notice);
  }

  let replyPage = 1;
  let attentionPage = 1, attentionQuery = '', attentionType = 'all', replyQuery = '';
  function operationsBody(data, patients = []) {
    const names = new Map(patients.map(p => [p.code, p.name]));
    const patient = code => `<strong>${esc(names.get(code) || code || 'Unmatched message')}</strong>${names.has(code) ? `<small>${esc(code)}</small>` : ''}`;
    const matchesPatient = (code, query) => `${code || ''} ${names.get(code) || ''}`.toLocaleLowerCase().includes(query.trim().toLocaleLowerCase());
    // Thanal's message has Yes/No buttons only, so typed Help/Reschedule requests sit in "Other requests".
    const kind = item => /whatsapp|deliver|unmatched message/i.test(item.reason) ? 'messages' : 'other';
    const inbox = data.inbox.filter(item => !item.resolved && matchesPatient(item.patient, attentionQuery) && (attentionType === 'all' || kind(item) === attentionType));
    const issues = data.offers.filter(o => matchesPatient(o.patient, attentionQuery) && ['all','messages'].includes(attentionType) && !['CLOSED','DECLINED'].includes(o.state) &&
      (['FAILED','UNKNOWN','NEEDS_ATTENTION','BLOCKED','WAITING_FOR_CONTACT'].includes(o.send_status) || o.delivery === 'failed'));
    const attention = inbox.map(item => `<article class="wa-followup-item"><div>${patient(item.patient)}<p>${esc(item.reason)}</p><details class="wa-detail" data-wa-detail="inbox-${item.id}"><summary>View details</summary><p>${esc(item.text)}</p></details></div>${action('Mark resolved','resolve',`data-id="${item.id}"`)}</article>`);
    attention.push(...issues.map(o => `<article class="wa-followup-item"><div>${patient(o.patient)}<p>${esc(o.send_status === 'WAITING_FOR_CONTACT' ? 'Parent needs to message first' : o.send_status === 'UNKNOWN' ? 'Message delivery needs checking' : 'Message could not be delivered')}</p><details class="wa-detail" data-wa-detail="issue-${esc(o.token)}"><summary>View details</summary>${offerCard(o,data,'followup')}</details></div></article>`));
    const unsubscribed = (data.unsubscribed || []).filter(u => !attentionQuery.trim() || (u.patients || []).some(code => matchesPatient(code, attentionQuery)));
    if (unsubscribed.length && ['all','unsubscribed'].includes(attentionType)) attention.push(`<article class="wa-followup-item"><div><strong>${unsubscribed.length} unsubscribed ${unsubscribed.length === 1 ? 'family' : 'families'}</strong><p>Contact these families another way.</p><details class="wa-detail" data-wa-detail="unsubscribed"><summary>View details</summary>${table(['Family','Phone'],unsubscribed.map(u => [esc((u.patients || []).join(', ') || 'Not matched'),esc(u.phone)]))}</details></div></article>`);
    if (data.sending_paused && !attentionQuery.trim() && ['all','messages'].includes(attentionType)) attention.unshift('<article class="wa-followup-item"><div><strong>WhatsApp sending needs a configuration fix</strong><p>Messages are paused. Check the connection before resuming in View details below.</p></div></article>');
    const replyLabel = o => {
      if (o.last_reply === 'YES') return 'Yes';
      if (['NO','VISIT','DAY'].includes(o.last_reply)) return 'No';
      if (o.last_reply === 'HELP') return 'Help requested';
      if (o.last_reply === 'RESCHEDULE') return 'Reschedule requested';
      if (o.state === 'DECLINED') return 'Declined';
      if (o.assumed_accepted && o.state === 'CONFIRMED') return 'Confirmed without a reply';
      if (o.state === 'CLOSED') return 'Offer withdrawn';
      if (o.state === 'EXPIRED') return 'No reply before deadline';
      if (['FAILED','BLOCKED','NEEDS_ATTENTION','UNSUBSCRIBED'].includes(o.send_status) || o.delivery === 'failed') return 'Not delivered';
      if (o.send_status === 'UNKNOWN') return 'Delivery unknown';
      if (o.send_status !== 'SENT' && !(preview(data) && o.send_status === 'SIMULATED')) return 'Not sent yet';
      return 'Awaiting reply';
    };
    const offers = [...data.offers].reverse().filter(o => matchesPatient(o.patient, replyQuery));
    const pageCount = Math.max(1, Math.ceil(offers.length / 10));
    replyPage = Math.min(replyPage, pageCount);
    const selected = offers.slice((replyPage - 1) * 10, replyPage * 10);
    const replyRows = selected.map(o => {
      const items = data.appointments.filter(a => o.appointment_ids.includes(a.id));
      const times = [...new Set(items.map(a => a.time))].join(', ') || 'Time unavailable';
      const therapies = [...new Set(items.map(a => a.therapy))].join(', ');
      const text = replyLabel(o);
      const tone = ['No','Declined'].includes(text) ? 'wa-reply-no' : text === 'Yes' ? 'wa-reply-yes' : '';
      return [patient(o.patient),`${esc(times)}<small>${esc(therapies)}</small>`,`<span class="${tone}">${esc(text)}</span>`, `<details class="wa-reply-detail" data-wa-detail="reply-${esc(o.token)}"><summary>View details</summary>${offerCard(o,data,'reply')}</details>`];
    });
    const attentionPages = Math.max(1, Math.ceil(attention.length / 5));
    attentionPage = Math.min(attentionPage, attentionPages);
    const filterField = (key, title, value) => `<label>${title}<input type="search" data-wa-filter="${key}" placeholder="Search patient name or ID" value="${esc(value)}" maxlength="254" autocomplete="off"></label>`;
    const pagination = (page, pages, key, count) => pages > 1 ? `<div class="wa-reply-pagination"><span>Page ${page} of ${pages} · ${count} items</span><button type="button" class="secondary-button compact-button" data-wa-${key}-page="${page-1}" ${page === 1 ? 'disabled' : ''}>Previous</button><button type="button" class="secondary-button compact-button" data-wa-${key}-page="${page+1}" ${page === pages ? 'disabled' : ''}>Next</button></div>` : '';
    return `<section class="wa-followup-section" aria-labelledby="waAttentionTitle"><div class="wa-section-heading"><h3 id="waAttentionTitle">Needs attention</h3><span class="soft-pill">${attention.length}</span></div><p class="fine-print">Open requests across all dates, plus delivery issues for the selected day.</p>${data.messaging_paused || preview(data) ? '<p class="wa-followup-mode">WhatsApp is off. Saved offers remain queued.</p>' : ''}<div class="wa-followup-filters">${filterField('attentionQuery','Patient name or ID',attentionQuery)}<label>Request type<select data-wa-filter="attentionType">${[['all','All requests'],['messages','Message issues'],['unsubscribed','Unsubscribed'],['other','Other requests']].map(([value,title]) => `<option value="${value}" ${attentionType === value ? 'selected' : ''}>${title}</option>`).join('')}</select></label></div>${attention.slice((attentionPage-1)*5,attentionPage*5).join('') || `<p class="wa-followup-empty">${attentionQuery.trim() || attentionType !== 'all' ? 'No items match these filters.' : 'Nothing needs your attention.'}</p>`}${pagination(attentionPage,attentionPages,'attention',attention.length)}</section>
      <section class="wa-followup-section" aria-labelledby="waRepliesTitle"><div class="wa-section-heading"><h3 id="waRepliesTitle">Parent replies</h3><span class="soft-pill">${offers.length}</span></div><div class="wa-followup-filters">${filterField('replyQuery','Patient name or ID',replyQuery)}</div>${offers.length ? table(['Patient','Appointment time','Reply',''],replyRows,'wa-replies-table') : `<p class="wa-followup-empty">${replyQuery.trim() ? 'No replies match this patient name or ID.' : 'No parent replies or offers for this date.'}</p>`}${pagination(replyPage,pageCount,'reply',offers.length)}</section>
      <details class="wa-detail wa-followup-details" data-wa-detail="history"><summary>View details</summary><h4>Message history</h4>${table(['Type','Status','Delivery / issue'],data.jobs.map(j => [esc(label(j.kind)),esc(label(j.status)),esc(j.error || j.delivery || 'Not reported')]))}<h4>Resolved requests</h4>${data.inbox.filter(i => i.resolved).map(i => `<p>${esc(i.patient || 'Unmatched message')} · ${esc(i.reason)}</p>`).join('') || '<p class="fine-print">No resolved requests.</p>'}${data.messaging_paused ? action('Process received replies','process-inbound') : ''}${data.sending_paused ? action('Resume after configuration fix','resume-sending') : ''}</details>`;
  }

  function mountOperations(options) {
    operationsController?.stop();
    operationsDate = operationsDate || options.date;
    const panel = document.createElement('section');
    panel.className = 'panel wa-panel';
    panel.innerHTML = '<div class="panel-header"><div><h2 class="wa-title"><svg aria-hidden="true"><use href="#i-whatsapp"/></svg>WhatsApp replies & follow-ups</h2><p>Check parent replies and handle items that need attention.</p></div></div><div class="panel-body"><div class="wa-toolbar"><label>Appointment date<input type="date" class="wa-date"></label><button class="secondary-button compact-button" type="button" data-wa-refresh>Refresh</button></div><p class="wa-notice" role="status"></p><div class="wa-body">Loading replies…</div></div>';
    options.root.querySelector('#operations-panel-whatsapp').append(panel);
    const date = panel.querySelector('.wa-date');
    date.value = operationsDate;
    const notice = (message,error=false) => { const el = panel.querySelector('.wa-notice'); el.textContent = message; el.classList.toggle('wa-error',error); };
    operationsController = controller(panel.querySelector('.wa-body'),options,data => operationsBody(data,options.patients),() => options.root.classList.contains('active'),() => date.value,notice);
    panel.querySelector('.wa-body').addEventListener('click', event => {
      const button = event.target.closest('[data-wa-reply-page], [data-wa-attention-page]');
      if (button && !button.disabled) {
        if (button.hasAttribute('data-wa-attention-page')) attentionPage = Number(button.dataset.waAttentionPage);
        else replyPage = Number(button.dataset.waReplyPage);
        operationsController.redraw();
      }
    });
    const updateFilter = event => {
      if (event.isComposing) return;
      const field = event.target;
      if (field.dataset.waFilter === 'attentionQuery') { attentionQuery = field.value; attentionPage = 1; }
      else if (field.dataset.waFilter === 'attentionType') { attentionType = field.value; attentionPage = 1; }
      else if (field.dataset.waFilter === 'replyQuery') { replyQuery = field.value; replyPage = 1; }
      else return;
      operationsController.redraw();
    };
    panel.querySelector('.wa-body').addEventListener('input', event => { if (event.target.tagName === 'INPUT') updateFilter(event); });
    panel.querySelector('.wa-body').addEventListener('change', event => { if (event.target.tagName === 'SELECT') updateFilter(event); });
    date.onchange = () => { if (date.value) { operationsDate = date.value; replyPage = 1; attentionPage = 1; operationsController.refresh(); } };
    panel.querySelector('[data-wa-refresh]').onclick = () => operationsController.refresh();
  }

  function mountCalendar(options) {
    // Offers and Zoho exports are handled by the scheduled jobs.
    options.root.querySelectorAll('[data-wa-open], [data-zoho-open]').forEach(button => button.remove());
  }

  function openZohoExport(options, opener) {
    if (document.querySelector('.zoho-export-dialog')) return;
    // Capture the selected day. Calendar filters and unsaved previews do not
    // change the saved appointments exported by the server.
    const date = options.date;
    const day = new Date(`${date}T12:00:00+05:30`).toLocaleDateString('en-IN', {day:'numeric',month:'long',year:'numeric',timeZone:'Asia/Kolkata'});
    const dialog = document.createElement('dialog');
    dialog.className = 'test-workspace-login zoho-export-dialog';
    dialog.setAttribute('aria-labelledby','zohoExportTitle');
    dialog.innerHTML = `<h2 id="zohoExportTitle">Push to Zoho</h2><p><strong>${esc(day)}</strong> · Saved appointments for this date, across all calendar filters. Unsaved previews are excluded.</p><div data-zoho-summary aria-live="polite">Loading export preview…</div><p data-zoho-notice role="status"></p><div class="wa-actions"><button type="button" class="secondary-button" data-zoho-close>Cancel</button><button type="button" class="primary-button" data-zoho-confirm disabled>Push to Zoho</button></div>`;
    document.body.append(dialog);
    const summary = dialog.querySelector('[data-zoho-summary]');
    const notice = dialog.querySelector('[data-zoho-notice]');
    const confirm = dialog.querySelector('[data-zoho-confirm]');
    const close = dialog.querySelector('[data-zoho-close]');
    let busy = false;
    const message = (text, error=false) => { notice.textContent = text; notice.classList.toggle('wa-error',error); };
    close.onclick = () => dialog.close();
    dialog.addEventListener('cancel', event => { if (busy) event.preventDefault(); });
    dialog.addEventListener('close', () => { dialog.remove(); opener.focus(); });
    dialog.showModal();

    async function refreshPreview(showEmpty = true) {
      try {
        const result = await post(options.api,'/api/zoho/export/preview',{date});
        if (!dialog.isConnected) return;
        const last = result.lastPush?.completedAtIST;
        const batch = result.openBatch;
        const blocked = result.blockedReasons || [];
        const wrongDate = batch && batch.date !== date;
        summary.innerHTML = `${result.wouldAssumeAcceptedVisits ? `<p><strong>${result.wouldAssumeAcceptedVisits} sent visits</strong> have no reply. Pushing accepts these visits and includes their therapy appointments.</p>` : ''}${result.changes.length ? `<p><strong>${result.changes.length} appointment changes</strong> are ready to send.</p>` : !result.wouldAssumeAcceptedVisits && !batch ? '<p>No new appointment changes are ready.</p>' : ''}<p>Help requests and offers that were not sent stay for staff review. Existing Zoho rows are updated; repeat pushes do not create duplicates.</p>${batch ? `<p>An unfinished push for ${esc(batch.date)} ${wrongDate ? 'must be retried from that calendar date first' : 'will resume first'}.</p>` : ''}<p class="fine-print">Destination: ${esc(result.target)}${last ? `<br>Last successful push: ${esc(new Date(last).toLocaleString('en-IN',{timeZone:'Asia/Kolkata'}))} IST` : '<br>No successful push yet.'}</p>`;
        if (blocked.length) {
          summary.insertAdjacentHTML('beforeend', `<p><strong>${blocked.length} ${blocked.length === 1 ? 'appointment needs' : 'appointments need'} correction before this push.</strong></p><ul>${blocked.slice(0,10).map(row => `<li>${esc(row.patientName || row.patientCode)}: ${esc(row.reason)}</li>`).join('')}</ul>${blocked.length > 10 ? `<p>Showing the first 10 of ${blocked.length} affected appointments.</p>` : ''}`);
        }
        confirm.disabled = Boolean(wrongDate) || blocked.length > 0 || !(result.changes.length || result.wouldAssumeAcceptedVisits || batch);
        if (showEmpty && blocked.length) message('Resolve the listed lookup or status issues, then reopen this preview.',true);
        else if (showEmpty && confirm.disabled && !wrongDate) message('Nothing to push. Save the schedule and resolve pending staff requests first.');
      } catch (error) { if (dialog.isConnected) message(error.message,true); }
    }
    confirm.onclick = async () => {
      if (busy) return;
      busy = true; confirm.disabled = true; close.disabled = true;
      confirm.textContent = 'Pushing…';
      message('Keep this window open while Zoho confirms the records.');
      try {
        const result = await post(options.api,'/api/zoho/export/push',{date});
        message(`Push complete: ${result.created} added, ${result.updated} updated, ${result.reconciled} already matched. ${result.assumedAcceptedVisits} unanswered visit(s) accepted. ${result.awaitingStaff.length} appointment(s) still need staff review.`);
        confirm.hidden = true; close.textContent = 'Done';
        await refreshPreview(false);
      } catch (error) {
        await refreshPreview();
        message(`${error.message} Refresh or retry this date to continue safely.`,true);
        confirm.textContent = 'Retry push';
      } finally {
        busy = false; close.disabled = false;
        options.onChanged().catch(() => {});
      }
    };
    refreshPreview();
  }

  return {mountAppointment,mountOperations,mountCalendar};
})();
