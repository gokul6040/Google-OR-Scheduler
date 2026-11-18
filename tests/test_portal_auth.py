"""Account lifecycle and HTTP authorization regression checks on isolated SQLite."""
from datetime import datetime, timedelta
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import json
from threading import Thread

import pytest
from sqlalchemy import create_engine, select

from database.db import Base
from database.models import Appointment, AppointmentStatus, Department, Location, Patient, Therapist, TreatmentRequirement
from services.portal_auth import Accounts, Attempt, LoginSession, User, digest, prepare
from services.portal_access import doctor_schedule
import web_app


@pytest.fixture
def store(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + (tmp_path / 'accounts.sqlite').as_posix())
    Base.metadata.create_all(engine)
    prepare(engine)
    service = Accounts(engine)
    with service.sessions.begin() as s:
        s.add_all([Department(id=1, name='Speech'), Location(id=1, name='Centre', capacity=4)])
        s.add_all([Therapist(id=i, therapist_code=f'D{i}', name=f'Doctor {i}', department_id=1, location_id=1) for i in (1, 2)])
    monkeypatch.setenv('PORTAL_AUTH_ENABLED', 'true')
    monkeypatch.setenv('PORTAL_LOCAL_HTTP', 'true')
    monkeypatch.setenv('MAIN_WORKFLOW_ENABLED', 'false')
    monkeypatch.setattr('services.portal_access.accounts', lambda: service)
    yield service
    engine.dispose()


def account(store, email='admin@example.com', role='admin', clinician=None):
    password = store.create('Test user', email, role, clinician, actor='setup')
    token, user = store.login(email, password, 'setup')
    store.change_password(user, password, 'Permanent#Test1')
    return store.login(email, 'Permanent#Test1', 'setup')


def test_password_replacement_revokes_temporary_sessions_and_hashes_password(store):
    temporary = store.create('Admin', 'ADMIN@example.com', 'admin', actor='bootstrap')
    token, user = store.login('admin@example.com', temporary, 'local')
    assert user['mustChange']
    store.change_password(user, temporary, 'New#Permanent1')
    assert store.authenticate(token) is None
    with pytest.raises(ValueError):
        store.login('admin@example.com', temporary, 'local')
    _, user = store.login('admin@example.com', 'New#Permanent1', 'local')
    assert not user['mustChange']
    with store.sessions() as s:
        assert s.scalar(select(User)).password_hash.startswith('pbkdf2-sha256$600000$')


def test_admin_adds_admin_reset_and_deactivation_revoke_sessions(store):
    _, admin = account(store)
    token, second = account(store, 'second@example.com')
    password = store.update(second['id'], 'reset', admin['email'])
    assert store.authenticate(token) is None
    token, reset_user = store.login(second['email'], password, 'local')
    assert reset_user['mustChange']
    store.update(second['id'], 'deactivate', admin['email'])
    assert store.authenticate(token) is None
    with pytest.raises(ValueError, match='last active admin'):
        store.update(admin['id'], 'deactivate', admin['email'])
    with pytest.raises(ValueError, match='already has an account'):
        store.create('Duplicate', 'ADMIN@example.com', 'admin', actor=admin['email'])
    with pytest.raises(ValueError, match='already exists'):
        store.create('Other', 'other@example.com', 'admin', actor='bootstrap')


def test_lockout_commits_failures_and_password_reset_clears_it(store):
    _, user = account(store)
    for _ in range(5):
        with pytest.raises(ValueError):
            store.login(user['email'], 'wrong', 'local')
    with pytest.raises(ValueError):
        store.login(user['email'], 'Permanent#Test1', 'local')
    with store.sessions() as s:
        assert s.get(Attempt, digest('email:' + user['email'])).failures == 5
    temporary = store.update(user['id'], 'reset', 'another-admin')
    assert store.login(user['email'], temporary, 'local')[1]['mustChange']


def test_doctor_inactivation_and_expiration_invalidate_access(store):
    token, user = account(store, 'doctor@example.com', 'doctor', 1)
    assert store.authenticate(token)
    with store.sessions.begin() as s:
        s.get(Therapist, 1).active = False
    assert store.authenticate(token) is None
    with store.sessions.begin() as s:
        s.get(Therapist, 1).active = True
        s.get(LoginSession, digest(token)).expires = datetime.utcnow() - timedelta(seconds=1)
    assert store.authenticate(token) is None


def test_doctor_query_is_scoped_to_identity_date_and_minimal_fields(store):
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo('Asia/Kolkata')).date()
    with store.sessions.begin() as s:
        for i in (1, 2):
            s.add(Patient(id=i, patient_code=f'P{i}', name=f'Child {i}'))
            s.add(TreatmentRequirement(id=i, patient_id=i, department_id=1, assigned_therapist_id=i,
                                      sessions_per_week=2, sequence_order=1))
            for day in (today, today + timedelta(days=1), today + timedelta(days=2)):
                s.add(Appointment(patient_id=i, treatment_requirement_id=i, therapist_id=i,
                    department_id=1, location_id=1, date=day, slot_index=0, status=AppointmentStatus.CONFIRMED))
    for day in ('today', 'tomorrow'):
        data = doctor_schedule(store, {'clinicianId': 1}, day)
        assert len(data['sessions']) == 1
        assert data['sessions'][0]['patient'] == 'Child 1'
        assert set(data['sessions'][0]) == {'time', 'patient', 'code', 'therapy', 'location', 'status'}
    with pytest.raises(ValueError):
        doctor_schedule(store, {'clinicianId': 1}, 'nextweek')


def test_doctor_schedule_separates_cancelled_slot_from_its_replacement(store):
    from zoneinfo import ZoneInfo
    today = datetime.now(ZoneInfo('Asia/Kolkata')).date()
    with store.sessions.begin() as session:
        session.add(Patient(id=1, patient_code='P1', name='Child'))
        session.add(TreatmentRequirement(id=1, patient_id=1, department_id=1,
            assigned_therapist_id=1, sessions_per_week=2, sequence_order=1))
        for index, status in enumerate(AppointmentStatus):
            session.add(Appointment(patient_id=1, treatment_requirement_id=1, therapist_id=1,
                department_id=1, location_id=1, date=today,
                slot_index=0 if status in {AppointmentStatus.CONFIRMED, AppointmentStatus.CANCELLED_PATIENT} else index,
                status=status))
    result = doctor_schedule(store, {'clinicianId': 1}, 'today')
    assert {item['status'] for item in result['sessions']} == {'CONFIRMED', 'PENDING_CONFIRMATION'}
    assert {item['status'] for item in result['cancellations']} == {
        'CANCELLED_PATIENT', 'CANCELLED_OTHER', 'NEEDS_RESCHEDULING'}
    confirmed = next(item for item in result['sessions'] if item['status'] == 'CONFIRMED')
    cancelled = next(item for item in result['cancellations'] if item['status'] == 'CANCELLED_PATIENT')
    assert confirmed['time'] == cancelled['time']
    assert doctor_schedule(store, {'clinicianId': 2}, 'today')['sessions'] == []
    assert doctor_schedule(store, {'clinicianId': 1}, 'tomorrow')['cancellations'] == []


@pytest.fixture
def server(store):
    server = ThreadingHTTPServer(('127.0.0.1', 0), web_app.TherapySchedulerHandler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield server.server_port
    server.shutdown()
    server.server_close()
    thread.join()


def request(port, method, path, token=None, body=None, headers=None):
    client = HTTPConnection('127.0.0.1', port, timeout=10)
    values = {'Content-Type': 'application/json', 'X-Portal-Request': '1', **(headers or {})}
    if token:
        values['Cookie'] = f'therapy_portal={token}'
    client.request(method, path, json.dumps(body) if body is not None else None, values)
    response = client.getresponse()
    result = response.status, response.read(), dict(response.getheaders())
    client.close()
    return result


@pytest.mark.parametrize('path', ['/api/patients', '/api/overview', '/api/users', '/workflow/api/state', '/test/api/state', '/oauth/zoho/start', '/users/existing'])
def test_doctor_cannot_open_any_admin_route(store, server, path):
    token, _ = account(store, 'doctor@example.com', 'doctor', 1)
    assert request(server, 'GET', path, token)[0] == 403


def test_auth_routes_first_password_csrf_and_cookie_flags(store, server, monkeypatch):
    temporary = store.create('Admin', 'admin@example.com', 'admin', actor='bootstrap')
    monkeypatch.setenv('PORTAL_LOCAL_HTTP', 'false')
    status, body, headers = request(server, 'POST', '/api/auth/login', body={'email': 'admin@example.com', 'password': temporary})
    assert status == 200
    assert all(flag in headers['Set-Cookie'] for flag in ('HttpOnly', 'Secure', 'SameSite=Lax'))
    token = headers['Set-Cookie'].split(';')[0].split('=', 1)[1]
    assert request(server, 'GET', '/api/users', token)[0] == 403
    assert request(server, 'POST', '/api/auth/password', token,
                   {'current': temporary, 'password': 'Completely#New1'}, {'Origin': 'https://evil.example'})[0] == 403
    assert request(server, 'POST', '/api/auth/password', token,
                   {'current': temporary, 'password': 'Completely#New1'})[0] == 200
    assert request(server, 'GET', '/api/users', token)[0] == 401


def test_reused_connection_does_not_reuse_admin_identity(store, server):
    token, _ = account(store)
    client = HTTPConnection('127.0.0.1', server, timeout=10)
    client.request('GET', '/api/users', headers={'Cookie': 'therapy_portal=' + token})
    response = client.getresponse()
    assert response.status == 200
    response.read()
    client.request('GET', '/api/users')
    response = client.getresponse()
    assert response.status == 401
    response.read()
    client.close()


def test_doctor_cannot_write_or_choose_another_clinician(store, server):
    token, _ = account(store, 'doctor@example.com', 'doctor', 1)
    assert request(server, 'POST', '/api/users', token, {'role': 'admin'})[0] == 403
    status, body, _ = request(server, 'GET', '/api/doctor/schedule?day=today&clinicianId=2', token)
    assert status == 200
    assert json.loads(body)['sessions'] == []


def test_machine_credential_cannot_manage_users_and_old_shared_key_is_rejected(store, server, monkeypatch):
    monkeypatch.setenv('PORTAL_MACHINE_TOKEN', 'new-machine-credential')
    monkeypatch.setenv('MAIN_WORKFLOW_ADMIN_TOKEN', 'old-shared-key')
    assert request(server, 'GET', '/api/users', headers={'Authorization': 'Bearer new-machine-credential'})[0] == 401
    assert request(server, 'GET', '/api/overview', headers={'Authorization': 'Bearer old-shared-key'})[0] == 401


def test_logout_revokes_and_head_cannot_bypass_login(store, server):
    token, _ = account(store)
    assert request(server, 'HEAD', '/api/patients', token)[0] == 405
    assert request(server, 'POST', '/api/auth/logout', token, {})[0] == 200
    assert store.authenticate(token) is None


def test_logout_then_login_page_on_same_connection(store, server):
    token, _ = account(store)
    client = HTTPConnection('127.0.0.1', server, timeout=10)
    try:
        client.request('POST', '/api/auth/logout', '{}', headers={
            'Cookie': 'therapy_portal=' + token,
            'Content-Type': 'application/json', 'X-Portal-Request': '1'})
        response = client.getresponse()
        assert response.status == 200
        assert 'Max-Age=0' in response.getheader('Set-Cookie')
        response.read()
        connection = client.sock
        client.request('GET', '/login')
        response = client.getresponse()
        assert response.status == 200
        assert b'/portal.js' in response.read()
        assert client.sock is connection
        assert store.authenticate(token) is None
    finally:
        client.close()


def test_repeated_logout_clears_cookie_without_an_active_session(store, server):
    token, _ = account(store)
    for current_token in (token, token, None):
        status, body, headers = request(server, 'POST', '/api/auth/logout', current_token, {})
        assert status == 200
        assert json.loads(body)['ok']
        assert 'Max-Age=0' in headers['Set-Cookie']
    assert store.authenticate(token) is None


def test_rejected_post_closes_connection_before_unread_body_becomes_a_request(store, server):
    client = HTTPConnection('127.0.0.1', server, timeout=10)
    try:
        client.request('POST', '/api/users', '{}', headers={'X-Portal-Request': '1'})
        response = client.getresponse()
        assert response.status == 401
        assert response.getheader('Connection') == 'close'
        response.read()
        client.request('GET', '/login')
        response = client.getresponse()
        assert response.status == 200
        assert b'/portal.js' in response.read()
    finally:
        client.close()


def test_concurrent_admin_deactivations_preserve_an_active_admin(store):
    from concurrent.futures import ThreadPoolExecutor
    _, first = account(store)
    _, second = account(store, 'second@example.com')
    def disable(user):
        try:
            store.update(user['id'], 'deactivate', 'test-admin')
            return 'disabled'
        except ValueError:
            return 'protected'
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(disable, [first, second])) == ['disabled', 'protected']
    with store.sessions() as s:
        assert len(s.scalars(select(User).where(User.active.is_(True), User.role == 'admin')).all()) == 1


def test_admin_http_user_creation_reset_and_activity(store, server):
    token, _ = account(store)
    status, body, _ = request(server, 'POST', '/api/users', token,
        {'name': 'New admin', 'email': 'new@example.com', 'role': 'admin'})
    assert status == 201
    temporary = json.loads(body)['temporaryPassword']
    _, new_user = store.login('new@example.com', temporary, 'local')
    assert new_user['mustChange']
    status, body, _ = request(server, 'GET', '/api/users/audit', token)
    assert status == 200
    assert any(a['actor'] == 'admin@example.com' and 'user_created:new@example.com:admin' == a['action']
               for a in json.loads(body)['events'])
    assert request(server, 'POST', '/api/auth/login', body=['invalid'])[0] == 400


def test_portal_admin_reaches_workflow_without_shared_key(store, server, monkeypatch):
    from threading import RLock
    from types import SimpleNamespace
    import main_workflow.mount
    token, _ = account(store)
    monkeypatch.setenv('MAIN_WORKFLOW_ENABLED', 'true')
    monkeypatch.setenv('MAIN_WORKFLOW_REQUIRE_ACCESS_KEY', 'true')
    monkeypatch.setenv('MAIN_WORKFLOW_DATA_SOURCE', 'main')
    flow = SimpleNamespace(sessions=store.sessions, lock=RLock(), live=False, admin_id='old-generic-admin')
    instance = SimpleNamespace(flow=flow, token='internal-secret', previews={})
    monkeypatch.setattr(main_workflow.mount, 'runtime', lambda: instance)
    status, body, _ = request(server, 'GET', '/api/patients', token)
    assert status == 200
    assert json.loads(body)['patients'] == []
    status, body, _ = request(server, 'GET', '/', token)
    assert status == 200 and b'/portal.js' in body
    status, body, _ = request(server, 'GET', '/main-workflow-config.js', token)
    assert status == 200 and b'"requiresAccessKey": false' in body


@pytest.mark.parametrize('path', ['/users', '/users/existing', '/account'])
def test_admin_account_pages_reuse_workspace_shell(store, server, path):
    token, _ = account(store)
    status, body, _ = request(server, 'GET', path, token)
    assert status == 200
    assert b'id="sidebar"' in body and b'brand-mark' in body
    assert b'href="/?view=operations"' in body
    assert body.count(b'src="/portal.js"') == 1
    assert b'src="/app.js"' not in body
    assert b'class="portal-workspace-main"' in body


def _block_address(store, address):
    """Record 50 wrong tries for one address (the per-address limit) without 50 slow hashes."""
    with store.sessions.begin() as s:
        s.add(Attempt(key=digest('ip:' + address), failures=50, until=datetime.utcnow() + timedelta(minutes=15)))


def _login(port, email, forwarded=None):
    headers = {'X-Forwarded-For': forwarded} if forwarded else {}
    return request(port, 'POST', '/api/auth/login', body={'email': email, 'password': 'Permanent#Test1'},
                   headers=headers)[0]


def test_behind_azure_one_blocked_address_does_not_block_other_staff(store, server, monkeypatch):
    """Lockout fix: every request reaches the app from Azure's ingress, so the limit must use its client address."""
    _, user = account(store)
    monkeypatch.setenv('PORTAL_TRUST_FORWARDED_FOR', 'true')
    _block_address(store, '203.0.113.7')
    assert _login(server, user['email'], '203.0.113.7') == 401   # the guesser's own address stays blocked
    assert _login(server, user['email'], '198.51.100.20') == 200  # another staff member signs in normally
    # A visitor cannot escape the block by writing their own entry first: Azure appends the real one last.
    assert _login(server, user['email'], '198.51.100.20, 203.0.113.7') == 401


def test_forwarded_address_is_ignored_unless_enabled_or_malformed(store, server, monkeypatch):
    _, user = account(store)
    _block_address(store, '127.0.0.1')
    # Default (local runs): the header is ignored, so it cannot be used to dodge the limit.
    assert _login(server, user['email'], '198.51.100.20') == 401
    monkeypatch.setenv('PORTAL_TRUST_FORWARDED_FOR', 'true')
    assert _login(server, user['email'], 'not-an-address') == 401  # malformed: falls back to the socket address
    assert _login(server, user['email'], '198.51.100.20') == 200


def test_account_errors_are_clear_answers_not_server_errors(store, server):
    token, user = account(store)
    status, body, _ = request(server, 'POST', '/api/auth/login', body={'email': user['email'], 'password': 'wrong password'})
    assert status == 401 and b'Incorrect email or password.' in body
    status, body, _ = request(server, 'POST', '/api/users', token,
                              {'name': 'Again', 'email': user['email'], 'role': 'admin'})
    assert status == 400 and b'already has an account' in body
    status, body, _ = request(server, 'POST', f"/api/users/{user['id']}/deactivate", token, {})
    assert status == 400 and b'last active admin' in body


def test_test_account_can_be_handed_to_the_real_doctor(store):
    """Edit (user decision): change the email on a test account and reset its password for the real doctor."""
    _, admin = account(store)
    token, test = account(store, 'admin.test@example.com', 'doctor', 1)
    store.edit(test['id'], 'Doctor One', 'doctor.one@example.com', 1, admin['email'])
    assert store.authenticate(token) is None  # editing signs the account out everywhere
    with pytest.raises(ValueError):
        store.login('admin.test@example.com', 'Permanent#Test1', 'local')
    temporary = store.update(test['id'], 'reset', admin['email'])
    _, doctor = store.login('doctor.one@example.com', temporary, 'local')
    assert (doctor['name'], doctor['clinicianId'], doctor['mustChange']) == ('Doctor One', 1, True)


def test_deactivating_a_doctor_account_frees_the_clinician(store):
    _, admin = account(store)
    _, test = account(store, 'test@example.com', 'doctor', 1)
    with pytest.raises(ValueError, match='already has an account'):
        store.create('Real', 'real@example.com', 'doctor', 1, actor=admin['email'])
    store.update(test['id'], 'deactivate', admin['email'])
    store.create('Real', 'real@example.com', 'doctor', 1, actor=admin['email'])
    # The old account cannot come back without a free clinician.
    with pytest.raises(ValueError, match='Link this doctor'):
        store.update(test['id'], 'activate', admin['email'])
    with pytest.raises(ValueError, match='already has an account'):
        store.edit(test['id'], 'Test', 'test@example.com', 1, admin['email'])
    store.edit(test['id'], 'Test', 'test@example.com', 2, admin['email'])
    store.update(test['id'], 'activate', admin['email'])
    with store.sessions() as s:
        assert {u.email: u.clinician_id for u in s.scalars(select(User).where(User.role == 'doctor'))} == {
            'test@example.com': 2, 'real@example.com': 1}


def test_edit_rules_keep_accounts_consistent(store):
    _, admin = account(store)
    _, second = account(store, 'second@example.com')
    _, doctor = account(store, 'doctor@example.com', 'doctor', 1)
    with pytest.raises(ValueError, match='already has an account'):
        store.edit(second['id'], 'Second', 'ADMIN@example.com', None, admin['email'])
    with pytest.raises(ValueError, match='active clinician'):
        store.edit(doctor['id'], 'Doctor', 'doctor@example.com', None, admin['email'])  # active doctors stay linked
    with store.sessions.begin() as s:
        s.get(Therapist, 2).active = False
    with pytest.raises(ValueError, match='active clinician'):
        store.edit(doctor['id'], 'Doctor', 'doctor@example.com', 2, admin['email'])
    store.edit(second['id'], 'Second Admin', 'second@example.com', 1, admin['email'])  # admins never get a link
    with store.sessions() as s:
        assert s.get(User, second['id']).clinician_id is None and s.get(User, second['id']).name == 'Second Admin'
        assert s.get(User, second['id']).role == 'admin'


def test_edit_over_http_is_admin_only_and_editing_yourself_signs_you_out(store, server):
    token, admin = account(store)
    doctor_token, doctor = account(store, 'doctor@example.com', 'doctor', 1)
    body = {'name': 'Doctor', 'email': 'doctor.new@example.com', 'clinicianId': 1}
    assert request(server, 'POST', f"/api/users/{doctor['id']}/edit", doctor_token, body)[0] == 403
    status, raw, _ = request(server, 'POST', f"/api/users/{doctor['id']}/edit", token, body)
    assert status == 200 and json.loads(raw) == {'ok': True, 'reauthenticate': False}
    status, raw, _ = request(server, 'POST', f"/api/users/{admin['id']}/edit", token,
                             {'name': 'Admin', 'email': 'admin.new@example.com', 'clinicianId': None})
    assert status == 200 and json.loads(raw)['reauthenticate'] is True
    assert request(server, 'GET', '/api/users', token)[0] == 401
    status, raw, _ = request(server, 'POST', '/api/users/999/edit', account(store, 'third@example.com')[0], body)
    assert status == 400 and b'User not found' in raw


def test_user_page_clinician_list_is_active_only_with_therapy_and_centre(store, server):
    token, _ = account(store)
    with store.sessions.begin() as s:
        s.get(Therapist, 2).active = False
    status, raw, _ = request(server, 'GET', '/api/users', token)
    assert status == 200
    assert json.loads(raw)['clinicians'] == [{'id': 1, 'name': 'Doctor 1', 'department': 'Speech', 'location': 'Centre'}]


def test_wait_message_only_appears_once_the_limit_is_reached(store):
    """User feedback: one wrong password says so; only the 5th wrong try mentions waiting."""
    _, user = account(store)
    for _ in range(4):
        with pytest.raises(ValueError, match=r'^Incorrect email or password\.$'):
            store.login(user['email'], 'wrong password', 'local')
    with pytest.raises(ValueError, match=r'^Too many sign-in attempts\. Try again in 15 minutes\.$'):
        store.login(user['email'], 'wrong password', 'local')
    with pytest.raises(ValueError, match='Too many sign-in attempts'):
        store.login(user['email'], 'Permanent#Test1', 'local')  # even the right password waits
    # An unknown email behaves the same, so the messages never reveal which emails have accounts.
    with pytest.raises(ValueError, match=r'^Incorrect email or password\.$'):
        store.login('nobody@example.com', 'wrong password', 'local')


@pytest.mark.parametrize('password', ['Short!a', 'alllowercase!', 'ALLUPPERCASE!', 'NoSpecialChars1', 'Has Spaces#Here'])
def test_new_passwords_need_8_chars_upper_lower_and_special(store, password):
    temporary = store.create('Admin', 'admin@example.com', 'admin', actor='bootstrap')
    _, user = store.login('admin@example.com', temporary, 'local')
    with pytest.raises(ValueError, match='at least 8 characters'):
        store.change_password(user, temporary, password)
    store.change_password(user, temporary, 'Good#Pass')  # exactly 8, upper, lower, special
    assert store.login('admin@example.com', 'Good#Pass', 'local')[1]['mustChange'] is False


def test_existing_passwords_keep_working_until_changed(store):
    """User decision: people who already have a password keep it; the rule applies on the next change."""
    from services.portal_auth import password_hash
    store.create('Old', 'old@example.com', 'admin', actor='bootstrap')
    with store.sessions.begin() as s:
        old = s.scalar(select(User).where(User.email == 'old@example.com'))
        old.password_hash, old.must_change = password_hash('an old long lowercase password'), False
    _, user = store.login('old@example.com', 'an old long lowercase password', 'local')
    with pytest.raises(ValueError, match='at least 8 characters'):
        store.change_password(user, 'an old long lowercase password', 'another weak one')


def test_spaces_are_not_allowed_in_new_passwords(store):
    temporary = store.create('Admin', 'admin@example.com', 'admin', actor='bootstrap')
    _, user = store.login('admin@example.com', temporary, 'local')
    for password in ('Good#Pass ', ' Good#Pass', 'Good #Pass'):
        with pytest.raises(ValueError, match='no spaces'):
            store.change_password(user, temporary, password)
