"""Database-backed portal accounts. No provider login or email delivery required."""
from datetime import datetime, timedelta
import hashlib
import hmac
import os
import secrets

from sqlalchemy import Boolean, DateTime, Integer, String, Text, select
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class AuthBase(DeclarativeBase):
    pass


class User(AuthBase):
    __tablename__ = "portal_users"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))
    email: Mapped[str] = mapped_column(String(254), unique=True)
    password_hash: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(String(10))
    clinician_id: Mapped[int | None] = mapped_column(Integer, unique=True)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    must_change: Mapped[bool] = mapped_column(Boolean, default=True)


class LoginSession(AuthBase):
    __tablename__ = "portal_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    expires: Mapped[datetime] = mapped_column(DateTime)


class Attempt(AuthBase):
    __tablename__ = "portal_login_limits"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    until: Mapped[datetime] = mapped_column(DateTime)


class Audit(AuthBase):
    __tablename__ = "portal_audit"
    id: Mapped[int] = mapped_column(primary_key=True)
    at: Mapped[datetime] = mapped_column(DateTime, default=datetime.utcnow)
    actor: Mapped[str] = mapped_column(String(254))
    action: Mapped[str] = mapped_column(String(200))


class Control(AuthBase):
    __tablename__ = "portal_auth_control"
    id: Mapped[int] = mapped_column(primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=0)


def enabled() -> bool:
    return os.getenv("PORTAL_AUTH_ENABLED", "false").lower() == "true"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


PASSWORD_RULE = ("Use at least 8 characters with no spaces, including an uppercase letter, a lowercase "
                 "letter and a special character (for example ! @ # $).")


def check_password_rule(password: str) -> None:
    """Rule for passwords people choose (user decision, 13 Nov 2026).

    Applies when a password is set or changed; existing passwords keep working
    until their owner next changes them. Spaces are not allowed (user decision).
    """
    if (not isinstance(password, str) or not 8 <= len(password) <= 128
            or not any(c.isupper() for c in password) or not any(c.islower() for c in password)
            or any(c.isspace() for c in password)
            or not any(not c.isalnum() for c in password)):
        raise ValueError(PASSWORD_RULE)


def password_hash(password: str) -> str:
    # Shape check only: chosen passwords pass check_password_rule first; system
    # temporary passwords are long random strings that must be changed at first sign-in.
    if not isinstance(password, str) or not 8 <= len(password) <= 128:
        raise ValueError(PASSWORD_RULE)
    salt = secrets.token_hex(16)
    value = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 600_000).hex()
    return f"pbkdf2-sha256$600000${salt}${value}"


def password_matches(password: str, stored: str) -> bool:
    if not isinstance(password, str) or len(password) > 128:
        return False
    _, rounds, salt, expected = stored.split("$")
    actual = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), int(rounds)).hex()
    return hmac.compare_digest(actual, expected)


def public_user(user: User) -> dict:
    return {"id": user.id, "name": user.name, "email": user.email, "role": user.role,
            "active": user.active, "clinicianId": user.clinician_id, "mustChange": user.must_change}


class Accounts:
    def __init__(self, engine):
        self.sessions = sessionmaker(bind=engine, expire_on_commit=False)

    def lock(self, session) -> None:
        # Updating one persisted row serializes account changes on PostgreSQL AND SQLite.
        from sqlalchemy import update
        result = session.execute(update(Control).where(Control.id == 1).values(version=Control.version + 1))
        if result.rowcount != 1:
            raise ValueError("Prepare portal account storage before enabling login.")

    def audit(self, actor: str, action: str) -> None:
        with self.sessions.begin() as session:
            session.add(Audit(actor=actor, action=action))

    @staticmethod
    def usable(session, user) -> bool:
        if not user or not user.active:
            return False
        if user.role == "doctor":
            from database.models import Therapist
            if not user.clinician_id:
                return False  # Unlinked (e.g. deactivated, then freed) doctor accounts cannot sign in.
            doctor = session.get(Therapist, user.clinician_id)
            return bool(doctor and doctor.active)
        return user.role == "admin"

    def authenticate(self, token: str) -> dict | None:
        with self.sessions() as session:
            login = session.get(LoginSession, digest(token))
            if not login or login.expires <= datetime.utcnow():
                return None
            user = session.get(User, login.user_id)
            return public_user(user) if self.usable(session, user) else None

    def login(self, email: str, password: str, ip: str) -> tuple[str, dict]:
        email = str(email).strip().lower()[:254]
        now = datetime.utcnow()
        token, identity = None, None
        with self.sessions.begin() as session:
            self.lock(session)
            keys = [(digest("email:" + email), 5), (digest("ip:" + ip), 50)]
            limits = []
            for key, maximum in keys:
                limit = session.get(Attempt, key)
                if limit is None:
                    limit = Attempt(key=key, failures=0, until=now + timedelta(minutes=15))
                    session.add(limit)
                elif limit.until <= now:
                    limit.failures, limit.until = 0, now + timedelta(minutes=15)
                limits.append((limit, maximum))
            user = session.scalar(select(User).where(User.email == email))
            blocked = any(limit.failures >= maximum for limit, maximum in limits)
            # Also run the hash for unknown users to avoid a quick account-existence signal.
            stored = user.password_hash if user else "pbkdf2-sha256$600000$dummy$" + "0" * 64
            matches = password_matches(password, stored) if not blocked else False
            if not blocked and matches and self.usable(session, user):
                limits[0][0].failures = 0
                token = secrets.token_urlsafe(48)
                session.add(LoginSession(token_hash=digest(token), user_id=user.id,
                                         expires=now + timedelta(hours=12)))
                identity = public_user(user)
            else:
                for limit, maximum in limits:
                    limit.failures = min(limit.failures + 1, maximum)
            # Only mention waiting once a limit is actually reached (user feedback, 13 Nov 2026).
            # Unknown emails get the same messages and limits, so neither reveals an account.
            reached = [limit.until for limit, maximum in limits if limit.failures >= maximum]
            wait = max(1, -(-int((max(reached) - now).total_seconds()) // 60)) if reached else 0
            session.add(Audit(actor=email, action="login_success" if token else "login_failed"))
        if not token:
            if wait:
                raise ValueError(f"Too many sign-in attempts. Try again in {wait} minute{'s' if wait != 1 else ''}.")
            raise ValueError("Incorrect email or password.")
        return token, identity

    def revoke(self, session, user_id: int) -> None:
        from sqlalchemy import delete
        session.execute(delete(LoginSession).where(LoginSession.user_id == user_id))

    def logout(self, token: str) -> None:
        from sqlalchemy import delete
        with self.sessions.begin() as session:
            session.execute(delete(LoginSession).where(LoginSession.token_hash == digest(token)))

    def change_password(self, identity: dict, old: str, new: str) -> None:
        check_password_rule(new)
        hashed = password_hash(new)
        with self.sessions.begin() as session:
            self.lock(session)
            user = session.get(User, identity["id"])
            if not self.usable(session, user) or not password_matches(old, user.password_hash):
                raise ValueError("Your current password was not accepted.")
            if password_matches(new, user.password_hash):
                raise ValueError("Choose a different password from your temporary or current password.")
            user.password_hash, user.must_change = hashed, False
            self.revoke(session, user.id)
            session.add(Audit(actor=user.email, action="password_changed"))

    @staticmethod
    def clean_identity(name, email) -> tuple[str, str]:
        email = str(email or "").strip().lower()
        name = str(name or "").strip()
        if not name or len(name) > 120 or len(email) > 254 or email.count("@") != 1 or any(c.isspace() for c in email) or not all(email.split("@")):
            raise ValueError("Enter a name and a valid email address.")
        return name, email

    @staticmethod
    def free_clinician(session, clinician_id, user_id=None) -> int:
        """Return an active clinician's ID that no other account is linked to."""
        from database.models import Therapist
        try:
            doctor = session.get(Therapist, int(clinician_id or 0))
        except (TypeError, ValueError):
            doctor = None
        if not doctor or not doctor.active:
            raise ValueError("Choose an active clinician.")
        taken = select(User.id).where(User.clinician_id == doctor.id)
        if user_id is not None:
            taken = taken.where(User.id != user_id)
        if session.scalar(taken):
            raise ValueError("This clinician already has an account.")
        return doctor.id

    def edit(self, user_id: int, name, email, clinician_id, actor: str) -> None:
        """Correct an account's name, email or clinician link (user decision, 12 Nov 2026).

        Lets an admin hand a test account to the real doctor by changing its email
        and clinician. The role never changes. Every edit signs the account out.
        """
        name, email = self.clean_identity(name, email)
        with self.sessions.begin() as session:
            self.lock(session)
            user = session.get(User, user_id)
            if not user:
                raise ValueError("User not found.")
            if session.scalar(select(User.id).where(User.email == email, User.id != user.id)):
                raise ValueError("This email already has an account.")
            if user.role == "doctor":
                if clinician_id in (None, "", 0, "0"):
                    if user.active:
                        raise ValueError("Choose an active clinician.")
                    link = None  # An inactive doctor account may stay unlinked.
                else:
                    link = self.free_clinician(session, clinician_id, user.id)
            else:
                link = None
            changes = [field for field, old, new in (("name", user.name, name), ("email", user.email, email),
                       ("clinician", user.clinician_id, link)) if old != new]
            if not changes:
                return
            old_email = user.email
            user.name, user.email, user.clinician_id = name, email, link
            self.revoke(session, user.id)
            session.add(Audit(actor=actor, action=f"user_edited:{old_email}:{'+'.join(changes)}"[:200]))

    def create(self, name: str, email: str, role: str, clinician_id=None, *, actor: str,
               permanent_password: str | None = None) -> str:
        name, email = self.clean_identity(name, email)
        if role not in {"admin", "doctor"}:
            raise ValueError("Choose Admin or Doctor.")
        if permanent_password is not None and (actor != "bootstrap" or role != "admin"):
            raise ValueError("Only first-admin setup can choose an initial permanent password.")
        temporary = secrets.token_urlsafe(18)
        if permanent_password is not None:
            check_password_rule(permanent_password)
        hashed = password_hash(permanent_password if permanent_password is not None else temporary)
        with self.sessions.begin() as session:
            self.lock(session)
            if actor == "bootstrap" and session.scalar(select(User.id).where(User.role == "admin")):
                raise ValueError("An admin already exists. Use the Users page.")
            if session.scalar(select(User.id).where(User.email == email)):
                raise ValueError("This email already has an account.")
            if role == "doctor":
                clinician_id = self.free_clinician(session, clinician_id)
            else:
                clinician_id = None
            session.add(User(name=name, email=email, role=role, clinician_id=clinician_id,
                             password_hash=hashed, active=True, must_change=permanent_password is None))
            session.add(Audit(actor=actor, action=f"user_created:{email}:{role}"))
        return temporary

    def update(self, user_id: int, action: str, actor: str) -> str | None:
        from sqlalchemy import delete
        temporary = None
        with self.sessions.begin() as session:
            self.lock(session)
            user = session.get(User, user_id)
            if not user:
                raise ValueError("User not found.")
            if action == "deactivate":
                admins = session.scalars(select(User.id).where(User.active.is_(True), User.role == "admin")).all()
                if user.active and user.role == "admin" and len(admins) <= 1:
                    raise ValueError("The last active admin cannot be deactivated.")
                user.active = False
                if user.role == "doctor":
                    # Free the clinician so a new account can be created for them (user decision).
                    user.clinician_id = None
            elif action == "activate":
                if user.role == "doctor" and not user.clinician_id:
                    raise ValueError("Link this doctor account to a clinician first (Edit), then activate it.")
                user.active = True
            elif action == "reset":
                temporary = secrets.token_urlsafe(18)
                user.password_hash, user.must_change = password_hash(temporary), True
                session.execute(delete(Attempt).where(Attempt.key == digest("email:" + user.email)))
            else:
                raise ValueError("Unknown account action.")
            self.revoke(session, user.id)
            session.add(Audit(actor=actor, action=f"user_{action}:{user.email}"))
        return temporary


def prepare(engine) -> None:
    AuthBase.metadata.create_all(engine)
    with sessionmaker(bind=engine).begin() as session:
        if session.get(Control, 1) is None:
            session.add(Control(id=1, version=0))
