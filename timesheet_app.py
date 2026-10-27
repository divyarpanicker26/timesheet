"""Local timesheet site.

Employees sign in, fill the bi-weekly form, and submit it.
Supervisors sign in, review the weekday pattern, and approve with their own signature.
Admins sign in, create user ids, and choose each person's permissions.

Run: python timesheet_app.py
"""

from __future__ import annotations

import hashlib
import json
import re
import secrets
import sys
import tempfile
import threading
from datetime import date, datetime
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from generate_timesheet import (  # noqa: E402
    DAY_NAMES,
    DEFAULT_FINISH,
    DEFAULT_REASON,
    DEFAULT_START,
    DayEntry,
    Timesheet,
    build_timesheet,
    employee_slug,
    digital_signature_html,
    format_date,
    parse_user_date,
    render_html,
    review,
    sunday_on_or_before,
    write_pdf,
)

DATA_PATH = ROOT / "data" / "timesheets.json"
HOST = "127.0.0.1"
PORT = 8766
SESSIONS: dict[str, dict[str, str]] = {}
DB_LOCK = threading.Lock()

EMPLOYEE_PASSWORD = "timesheet"
SUPERVISOR_PASSWORD = "approve"
ADMIN_PASSWORD = "manage"
PERMISSIONS = (
    ("submit", "Submit time sheets"),
    ("approve", "Review and approve"),
    ("manage", "Manage users"),
)
ROLE_PERMISSIONS = {
    "employee": ["submit"],
    "supervisor": ["approve"],
    "admin": ["manage"],
}
USER_ID = re.compile(r"^[a-z][a-z0-9._-]{1,31}$")


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return f"{salt}${digest.hex()}"


def check_password(stored: str, password: str) -> bool:
    salt, digest = stored.split("$", 1)
    test = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
    return secrets.compare_digest(test, digest)


def make_user(user_id: str, name: str, password: str, permissions: list[str]) -> dict:
    return {
        "id": user_id,
        "name": name,
        "role": role_name(permissions),
        "permissions": permissions,
        "password": hash_password(password),
    }


def seed_db() -> dict:
    return {
        "next_id": 1,
        "users": [
            make_user("divya", "Divya Radhakrishnan", EMPLOYEE_PASSWORD, ["submit"]),
            make_user("supervisor", "Supervisor", SUPERVISOR_PASSWORD, ["approve"]),
            make_user("admin", "Admin", ADMIN_PASSWORD, ["manage"]),
        ],
        "sheets": [],
    }


def permissions_of(user: dict) -> list[str]:
    stored = user.get("permissions")
    allowed = {key for key, _label in PERMISSIONS}
    if isinstance(stored, list):
        return [item for item in stored if item in allowed]
    return list(ROLE_PERMISSIONS.get(user.get("role", ""), []))


def can(user: dict, permission: str) -> bool:
    return permission in permissions_of(user)


def role_name(permissions: list[str]) -> str:
    if "manage" in permissions and "submit" not in permissions and "approve" not in permissions:
        return "admin"
    if "approve" in permissions and "submit" not in permissions:
        return "supervisor"
    if "submit" in permissions and "approve" not in permissions and "manage" not in permissions:
        return "employee"
    if "manage" in permissions:
        return "admin"
    if "approve" in permissions:
        return "supervisor"
    return "employee"


def permission_text(user: dict) -> str:
    labels = {"submit": "employee", "approve": "supervisor", "manage": "admin"}
    names = [labels[item] for item in permissions_of(user) if item in labels]
    return ", ".join(names) or "no access"


def landing(user: dict) -> str:
    if can(user, "submit"):
        return "/home"
    if can(user, "approve"):
        return "/queue"
    if can(user, "manage"):
        return "/admin"
    return "/home"


def ensure_accounts(db: dict) -> bool:
    """Fill missing permissions and add the admin account once."""
    changed = False
    for user in db["users"]:
        if not isinstance(user.get("permissions"), list):
            user["permissions"] = list(ROLE_PERMISSIONS.get(user.get("role", "employee"), ["submit"]))
            changed = True
        role = role_name(user["permissions"])
        if user.get("role") != role:
            user["role"] = role
            changed = True
    if not any(can(user, "manage") for user in db["users"]):
        db["users"].append(make_user("admin", "Admin", ADMIN_PASSWORD, ["manage"]))
        changed = True
    return changed


def load_db() -> dict:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DATA_PATH.exists():
        db = seed_db()
        DATA_PATH.write_text(json.dumps(db, indent=2), encoding="utf-8")
        return db
    db = json.loads(DATA_PATH.read_text(encoding="utf-8"))
    if ensure_accounts(db):
        save_db(db)
    return db


def save_db(db: dict) -> None:
    DATA_PATH.write_text(json.dumps(db, indent=2), encoding="utf-8")


def read_db() -> dict:
    with DB_LOCK:
        return load_db()


def mutate_db(mutator) -> dict:
    with DB_LOCK:
        db = load_db()
        mutator(db)
        save_db(db)
        return db


def find_user(db: dict, user_id: str) -> dict | None:
    return next((user for user in db["users"] if user["id"] == user_id), None)


def find_sheet(db: dict, sheet_id: int) -> dict | None:
    return next((sheet for sheet in db["sheets"] if sheet["id"] == sheet_id), None)


def blank_weeks() -> list[list[dict[str, str]]]:
    return [
        [{"day": day, "date": "", "start": "", "finish": ""} for day in DAY_NAMES]
        for _ in range(2)
    ]


def weeks_from_model(sheet: Timesheet) -> list[list[dict[str, str]]]:
    return [
        [
            {
                "day": entry.day_name,
                "date": entry.work_date.isoformat() if entry.work_date else "",
                "start": entry.start,
                "finish": entry.finish,
            }
            for entry in week
        ]
        for week in sheet.weeks
    ]


def new_record(user: dict, form_date: date, period_start: date) -> dict:
    filled = build_timesheet(
        form_date=form_date,
        period_start=period_start,
        employee=user["name"],
        reason=DEFAULT_REASON,
    )
    return {
        "id": None,
        "employee_id": user["id"],
        "employee_name": user["name"],
        "timekeeper": "",
        "kronos_id": "",
        "form_date": form_date.isoformat(),
        "period_start": period_start.isoformat(),
        "reason": DEFAULT_REASON,
        "witness": "",
        "weeks": weeks_from_model(filled),
        "status": "draft",
        "supervisor_name": "",
        "supervisor_date": "",
        "supervisor_signature": "",
        "supervisor_sign_method": "",
        "supervisor_signed_at": "",
        "employee_signature": "",
        "employee_sign_method": "",
        "employee_sign_date": "",
        "employee_signed_at": "",
        "return_note": "",
    }


def to_model(record: dict) -> Timesheet:
    weeks = []
    for week in record["weeks"]:
        rows = []
        for row in week:
            work_date = date.fromisoformat(row["date"]) if row["date"] else None
            rows.append(DayEntry(row["day"], work_date, row["start"], row["finish"]))
        weeks.append(tuple(rows))
    return Timesheet(
        form_date=date.fromisoformat(record["form_date"]),
        period_start=date.fromisoformat(record["period_start"]),
        employee=record["employee_name"],
        timekeeper=record.get("timekeeper", ""),
        kronos_id=record.get("kronos_id", ""),
        reason=record.get("reason", ""),
        start_time=DEFAULT_START,
        finish_time=DEFAULT_FINISH,
        weeks=(weeks[0], weeks[1]),
        employee_signature=record.get("employee_signature", ""),
        employee_sign_method=record.get("employee_sign_method", ""),
        employee_sign_date=date.fromisoformat(record["employee_sign_date"][:10]) if record.get("employee_sign_date") else None,
        employee_signed_at=record.get("employee_signed_at", ""),
        supervisor_name=record.get("supervisor_name", ""),
        supervisor_date=date.fromisoformat(record["supervisor_date"][:10]) if record.get("supervisor_date") else None,
        supervisor_signature=record.get("supervisor_signature", ""),
        supervisor_sign_method=record.get("supervisor_sign_method", ""),
        supervisor_signed_at=record.get("supervisor_signed_at", ""),
        witness=record.get("witness", ""),
    )


def parse_form_date(value: str, label: str) -> tuple[date | None, str | None]:
    text = value.strip()
    if not text:
        return None, f"{label} is required."
    try:
        return parse_user_date(text), None
    except ValueError:
        return None, f"{label} must be a date."


def record_from_post(form: dict[str, str], user: dict, existing: dict | None) -> tuple[dict, list[str]]:
    errors: list[str] = []
    form_date, form_error = parse_form_date(form.get("form_date", ""), "Form date")
    period_start, period_error = parse_form_date(form.get("period_start", ""), "Period start")
    if form_error:
        errors.append(form_error)
    if period_error:
        errors.append(period_error)
    if period_start and period_start.strftime("%A") != "Sunday":
        errors.append("Period start must be a Sunday.")
    if form.get("action") == "submit" and form.get("certify") != "yes":
        errors.append("Check the certification box before submitting.")
    if form.get("action") == "submit" and not (
        existing and existing.get("employee_sign_method") == "digital" and existing.get("employee_signed_at")
    ):
        errors.append("Use the Sign button before submitting.")

    weeks = blank_weeks()
    if form.get("action") == "rebuild":
        if form_date and period_start and period_start.strftime("%A") == "Sunday":
            weeks = new_record(user, form_date, period_start)["weeks"]
    else:
        for week_index in range(2):
            for day_index, day_name in enumerate(DAY_NAMES):
                prefix = f"w{week_index}d{day_index}"
                date_text = form.get(f"{prefix}_date", "").strip()
                work_date = ""
                if date_text:
                    parsed, date_error = parse_form_date(date_text, f"Week {week_index + 1} {day_name}")
                    if date_error:
                        errors.append(date_error)
                    elif parsed:
                        work_date = parsed.isoformat()
                weeks[week_index][day_index] = {
                    "day": day_name,
                    "date": work_date,
                    "start": form.get(f"{prefix}_start", "").strip(),
                    "finish": form.get(f"{prefix}_finish", "").strip(),
                }

    base = existing or {
        "id": None,
        "employee_id": user["id"],
        "employee_name": user["name"],
        "status": "draft",
        "supervisor_name": "",
        "supervisor_date": "",
        "supervisor_signature": "",
        "supervisor_sign_method": "",
        "supervisor_signed_at": "",
        "employee_signature": "",
        "employee_sign_method": "",
        "employee_sign_date": "",
        "employee_signed_at": "",
        "return_note": "",
    }
    record = {
        **base,
        "employee_id": user["id"],
        "employee_name": user["name"],
        "timekeeper": form.get("timekeeper", "").strip(),
        "kronos_id": form.get("kronos_id", "").strip(),
        "form_date": form_date.isoformat() if form_date else form.get("form_date", ""),
        "period_start": period_start.isoformat() if period_start else form.get("period_start", ""),
        "reason": form.get("reason", "").strip(),
        "witness": form.get("witness", "").strip(),
        "employee_signature": base.get("employee_signature", ""),
        "employee_sign_method": base.get("employee_sign_method", ""),
        "employee_sign_date": base.get("employee_sign_date", ""),
        "employee_signed_at": base.get("employee_signed_at", ""),
        "weeks": weeks,
    }
    return record, errors


def clean_signature(value: str) -> str:
    """Keep a drawn PNG signature and drop anything else."""
    if not value.startswith("data:image/png;base64,"):
        return ""
    payload = value.split(",", 1)[1]
    if len(value) > 400_000 or not re.fullmatch(r"[A-Za-z0-9+/=]+", payload or ""):
        return ""
    return value


def signature_markup(data: str) -> str:
    cleaned = clean_signature(data)
    if not cleaned:
        return ""
    return f"<img class='signature' alt='Drawn signature' src='{esc(cleaned)}'>"


def esc(value: object) -> str:
    return (
        str(value)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


PAGE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>__TITLE__</title>
<style>
  :root { color-scheme: light; }
  * { box-sizing: border-box; }
  body {
    margin: 0;
    background: #e7edf5;
    color: #1a1a1a;
    font-family: "Segoe UI", Calibri, sans-serif;
  }
  a { color: #1a4f8b; }
  .nav {
    display: flex;
    justify-content: space-between;
    align-items: center;
    gap: 16px;
    padding: 12px 24px;
    background: #17375e;
    color: white;
  }
  .nav a, .nav button {
    color: white;
    background: transparent;
    border: 1px solid rgba(255,255,255,.45);
    border-radius: 6px;
    padding: 6px 10px;
    text-decoration: none;
    cursor: pointer;
    font: inherit;
  }
  .nav form { margin: 0; }
  .nav .links { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }
  label.choice { display: inline-flex; align-items: center; gap: 6px; margin: 8px 16px 8px 0; font-weight: 500; }
  .panel { background: white; border-radius: 10px; padding: 20px 22px; margin: 16px 0 24px; }
  .panel h2 { margin: 0 0 4px; font-size: 20px; }
  main { max-width: 1100px; margin: 24px auto 48px; padding: 0 16px; }
  .card, .paper {
    background: white;
    border-radius: 10px;
    box-shadow: 0 8px 24px rgba(23, 55, 94, .08);
  }
  .card { padding: 28px; max-width: 460px; margin: 8vh auto; }
  h1 { margin: 0 0 8px; font-size: 28px; }
  .muted { color: #526074; }
  label { display: block; font-weight: 650; margin: 14px 0 6px; }
  input[type="text"], input[type="password"], input[type="date"], textarea {
    width: 100%;
    border: 1px solid #b7c3d3;
    border-radius: 6px;
    padding: 8px 10px;
    font: inherit;
    background: white;
    color: #1a1a1a;
  }
  textarea { min-height: 64px; resize: vertical; }
  button.primary, a.button {
    display: inline-block;
    margin-top: 16px;
    background: #17375e;
    color: white;
    border: 0;
    border-radius: 6px;
    padding: 10px 14px;
    font: inherit;
    cursor: pointer;
    text-decoration: none;
  }
  button.secondary { background: white; color: #17375e; border: 1px solid #17375e; }
  button.warn { background: #9b1c1c; }
  .error {
    background: #fde8e8;
    color: #9b1c1c;
    border-radius: 6px;
    padding: 10px 12px;
    margin: 12px 0;
  }
  .ok { background: #e5f4ea; color: #146c2e; border-radius: 6px; padding: 10px 12px; }
  .warnbox { background: #fff6e8; color: #8a4b08; border-radius: 6px; padding: 10px 12px; }
  table.list { width: 100%; border-collapse: collapse; background: white; border-radius: 10px; overflow: hidden; }
  table.list th, table.list td { padding: 12px; text-align: left; border-bottom: 1px solid #e1e7ef; vertical-align: top; }
  table.list th { background: #eef3fb; }
  .badge { font-weight: 700; }
  .badge.submitted { color: #1a4f8b; }
  .badge.approved { color: #146c2e; }
  .badge.draft { color: #526074; }
  .badge.returned { color: #9b1c1c; }
  .sign-btn {
    display: inline-block;
    margin: 0 4px 2px;
    padding: 1px 8px;
    border: 0;
    border-radius: 3px;
    background: #c2410c;
    color: white;
    font-family: "Segoe UI", Calibri, sans-serif;
    font-size: 11px;
    font-weight: 700;
    line-height: 1.4;
    cursor: pointer;
    text-decoration: none;
    vertical-align: middle;
  }
  .sign-btn:hover { background: #9a3412; }
  .sig-preview { margin: 18px 0; padding: 16px; background: #fff; border: 1px solid #d5deea; border-radius: 8px; }
  .paper { padding: 28px 32px 36px; font-family: "Times New Roman", Times, serif; }
  .paper h1, .paper h2 { text-align: center; margin: 0; letter-spacing: .02em; }
  .paper h1 { font-size: 22px; }
  .paper h2 { font-size: 18px; margin-bottom: 8px; }
  .date-row { text-align: right; margin: 8px 0 12px; }
  .meta { width: 100%; border-collapse: collapse; margin-bottom: 8px; }
  .meta td { border: none; padding: 4px 0; }
  .grid { width: 100%; border-collapse: collapse; }
  .grid > tbody > tr > td { width: 50%; vertical-align: top; padding: 0; }
  table.week { width: 100%; border-collapse: collapse; }
  table.week th, table.week td {
    border: 1px solid #222;
    padding: 3px 5px;
    font-size: 13px;
    background: white;
  }
  table.week th { background: #d9e2f3; text-align: center; }
  td.shade, th.day { background: #eef3fb; }
  table.week input { border: 0; padding: 2px; font-family: inherit; font-size: 13px; }
  .reason { border: 1px solid #222; border-top: none; background: #eef3fb; padding: 8px; }
  .sign { width: 100%; margin-top: 14px; }
  .sign td { padding: 7px 8px 7px 0; vertical-align: bottom; }
  .line { border-bottom: 1px solid #222; min-width: 220px; display: inline-block; padding: 0 6px; }
  table.sign td:first-child { width: 74%; }
  img.signature { height: 52px; vertical-align: middle; }
  .sig-name { margin-left: 8px; }
  .sig-block { display: inline-flex; align-items: center; gap: 10px; vertical-align: bottom; white-space: nowrap; }
  .sig-script { font-family: "Segoe Script", "Brush Script MT", "Lucida Handwriting", cursive; font-size: 26px; font-weight: 700; line-height: 1; color: #111; white-space: nowrap; }
  .sig-stamp { display: inline-flex; flex-direction: column; font-family: "Segoe UI", Calibri, sans-serif; font-size: 10px; line-height: 1.15; color: #111; }
  .sig-stamp span { white-space: nowrap; }
  .choice { margin-right: 16px; font-weight: 650; }
  .fine { font-size: 12px; }
  .actions { display: flex; gap: 10px; flex-wrap: wrap; align-items: end; }
  .actions form, .actions button { margin: 0; }
  .check { font-weight: 400; }
  .accounts { background: #f4f7fb; border-radius: 8px; padding: 10px 12px; margin: 12px 0 4px; }
  @media print {
    body { background: white; }
    .nav, .no-print { display: none; }
    main { margin: 0; max-width: none; }
    .paper { box-shadow: none; }
  }
</style>
</head>
<body>
__NAV__
<main>
__BODY__
</main>
</body>
</html>
"""


def render_page(title: str, body: str, user: dict | None = None) -> str:
    if user:
        links = ["<a href='" + landing(user) + "'>NYCHA Time Sheet</a>"]
        if can(user, "submit"):
            links.append("<a href='/home'>My sheets</a>")
        if can(user, "approve"):
            links.append("<a href='/queue'>Queue</a>")
        if can(user, "manage"):
            links.append("<a href='/admin'>Users</a>")
        nav = (
            "<header class='nav'><div class='links'>"
            + "".join(links)
            + "</div><div>"
            + esc(user["name"])
            + " · "
            + esc(permission_text(user))
            + " <form method='post' action='/logout' style='display:inline'>"
            + "<input type='hidden' name='csrf' value='"
            + esc(user.get("_csrf", ""))
            + "'><button type='submit'>Sign out</button></form></div></header>"
        )
    else:
        nav = ""
    return PAGE.replace("__TITLE__", esc(title)).replace("__NAV__", nav).replace("__BODY__", body)


def login_page(error: str = "") -> str:
    message = f"<div class='error'>{esc(error)}</div>" if error else ""
    body = f"""
    <section class="card">
      <h1>Time sheet sign in</h1>
      <p class="muted">Employees submit hours. Supervisors review and approve. Admins create user ids and set permissions.</p>
      <div class="accounts">
        <div><strong>Employee</strong> · username <code>divya</code> · password <code>{EMPLOYEE_PASSWORD}</code></div>
        <div><strong>Supervisor</strong> · username <code>supervisor</code> · password <code>{SUPERVISOR_PASSWORD}</code></div>
        <div><strong>Admin</strong> · username <code>admin</code> · password <code>{ADMIN_PASSWORD}</code></div>
      </div>
      {message}
      <form method="post" action="/login">
        <label for="username">Username</label>
        <input id="username" name="username" type="text" autocomplete="username" required>
        <label for="password">Password</label>
        <input id="password" name="password" type="password" autocomplete="current-password" required>
        <button class="primary" type="submit">Sign in</button>
      </form>
    </section>
    """
    return render_page("Sign in", body)


def status_badge(status: str) -> str:
    label = {
        "draft": "Not submitted",
        "submitted": "Waiting for supervisor",
        "approved": "Approved",
        "returned": "Returned",
    }.get(status, status)
    return f"<span class='badge {esc(status)}'>{esc(label)}</span>"


def pattern_box(issues: list[str]) -> str:
    if issues:
        items = "".join(f"<li>{esc(issue)}</li>" for issue in issues)
        return f"<div class='warnbox no-print'><strong>Pattern check needs correction</strong><ul>{items}</ul></div>"
    return "<div class='ok no-print'>Pattern check: weekday hours match 9.00AM–5.00PM.</div>"


def home_page(user: dict, sheets: list[dict]) -> str:
    rows = []
    for sheet in sheets:
        try:
            issues = review(to_model(sheet))
            pattern = "Needs correction" if issues else "Matches"
        except (ValueError, KeyError):
            pattern = "Needs correction"
        rows.append(
            "<tr>"
            f"<td>{esc(format_date(date.fromisoformat(sheet['form_date'])))}</td>"
            f"<td>{esc(format_date(date.fromisoformat(sheet['period_start'])))}</td>"
            f"<td>{esc(pattern)}</td>"
            f"<td>{status_badge(sheet['status'])}</td>"
            f"<td><a href='/sheet/{sheet['id']}'>Open</a> · <a href='/sheet/{sheet['id']}/pdf'>Download PDF</a></td>"
            "</tr>"
        )
    table = (
        "<table class='list'><thead><tr><th>Form date</th><th>Period start</th><th>Pattern</th><th>Status</th><th></th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
        if rows
        else "<p class='muted'>You have not submitted a time sheet yet.</p>"
    )
    body = f"""
    <h1>My time sheets</h1>
    <p><a class="button" href="/sheet/new">New time sheet</a></p>
    {table}
    """
    return render_page("My time sheets", body, user)


def queue_page(user: dict, sheets: list[dict]) -> str:
    rows = []
    for sheet in sheets:
        try:
            issues = review(to_model(sheet))
            pattern = "Needs correction" if issues else "Matches"
        except (ValueError, KeyError):
            pattern = "Needs correction"
        sign = ""
        if sheet["status"] == "submitted" and sheet.get("supervisor_sign_method") != "digital":
            sign = f" · <a class='sign-btn' href='/sheet/{sheet['id']}/sign'>Sign</a>"
        rows.append(
            "<tr>"
            f"<td>{esc(sheet['employee_name'])}</td>"
            f"<td>{esc(format_date(date.fromisoformat(sheet['form_date'])))}</td>"
            f"<td>{esc(pattern)}</td>"
            f"<td>{status_badge(sheet['status'])}</td>"
            f"<td><a href='/sheet/{sheet['id']}'>Review</a>{sign} · <a href='/sheet/{sheet['id']}/pdf'>Download PDF</a></td>"
            "</tr>"
        )
    table = (
        "<table class='list'><thead><tr><th>Employee</th><th>Form date</th><th>Pattern</th><th>Status</th><th></th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
        if rows
        else "<p class='muted'>No time sheets have been submitted.</p>"
    )
    body = f"<h1>Supervisor queue</h1><p class='muted'>Open a sheet to review the hours. Sign opens the digital signature page and approves that sheet.</p>{table}"
    return render_page("Supervisor queue", body, user)


def week_table(weeks: list, week_index: int, editable: bool) -> str:
    rows = []
    for day_index, row in enumerate(weeks[week_index]):
        shade = " class='shade'" if not row["date"] else ""
        if editable:
            date_cell = (
                f"<input type='date' name='w{week_index}d{day_index}_date' value='{esc(row['date'])}'>"
            )
            start_cell = (
                f"<input type='text' name='w{week_index}d{day_index}_start' value='{esc(row['start'])}'>"
            )
            finish_cell = (
                f"<input type='text' name='w{week_index}d{day_index}_finish' value='{esc(row['finish'])}'>"
            )
        else:
            shown = format_date(date.fromisoformat(row["date"])) if row["date"] else ""
            date_cell = esc(shown)
            start_cell = esc(row["start"])
            finish_cell = esc(row["finish"])
        rows.append(
            "<tr>"
            f"<td{shade}>{esc(row['day'])}</td>"
            f"<td{shade}>{date_cell}</td>"
            f"<td{shade}>{start_cell}</td>"
            f"<td{shade}>{finish_cell}</td>"
            "</tr>"
        )
    title = f"WEEK {week_index + 1}"
    return (
        "<table class='week'><thead>"
        f"<tr><th colspan='4'>{title}</th></tr>"
        "<tr><th>Day</th><th>MM/DD/YY</th><th>Start Time</th><th>Finish Time</th></tr>"
        f"</thead><tbody>{''.join(rows)}</tbody></table>"
    )


def sign_stamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def sign_button(label: str = "Sign") -> str:
    return f'<button class="sign-btn no-print" name="action" value="sign" type="submit">{esc(label)}</button>'


def paper_block(record: dict, editable: bool, supervisor_sign_href: str = "") -> str:
    def field(name: str, label: str, value: str, input_type: str = "text") -> str:
        if editable:
            return (
                f"<label for='{name}'>{label}</label>"
                f"<input id='{name}' name='{name}' type='{input_type}' value='{esc(value)}'>"
            )
        shown = value
        if input_type == "date" and value:
            try:
                shown = format_date(date.fromisoformat(value))
            except ValueError:
                shown = value
        return f"<div><strong>{label}</strong> {esc(shown) or '________'}</div>"

    grid = (
        "<table class='grid'><tr>"
        f"<td>{week_table(record['weeks'], 0, editable)}</td>"
        f"<td>{week_table(record['weeks'], 1, editable)}</td>"
        "</tr></table>"
    )
    if editable:
        reason = f"<textarea name='reason'>{esc(record.get('reason', ''))}</textarea>"
        witness = f"<input name='witness' type='text' value='{esc(record.get('witness', ''))}'>"
    else:
        reason = esc(record.get("reason", "")) or "________"
        witness = esc(record.get("witness", "")) or "________"
    employee_signed = record.get("status") in {"submitted", "approved", "returned"} or (
        record.get("employee_sign_method") == "digital" and bool(record.get("employee_signed_at"))
    )
    employee_image = signature_markup(record.get("employee_signature", ""))
    if editable and record.get("employee_sign_method") == "digital" and record.get("employee_signed_at"):
        employee_line = digital_signature_html(
            record["employee_name"],
            record.get("employee_signed_at") or record.get("employee_sign_date", ""),
        )
    elif editable:
        employee_line = sign_button()
    elif record.get("employee_sign_method") == "digital":
        employee_line = digital_signature_html(
            record["employee_name"],
            record.get("employee_signed_at") or record.get("employee_sign_date", ""),
        )
    elif employee_image:
        employee_line = f"{employee_image}<span class='sig-name'>{esc(record['employee_name'])}</span>"
    elif employee_signed:
        employee_line = esc(record["employee_name"])
    else:
        employee_line = "________"
    employee_date = ""
    if employee_signed and record.get("form_date"):
        try:
            employee_date = format_date(date.fromisoformat(record["form_date"]))
        except ValueError:
            employee_date = esc(record["form_date"])
    supervisor_image = signature_markup(record.get("supervisor_signature", ""))
    if record.get("supervisor_sign_method") == "digital" and record.get("supervisor_name"):
        supervisor_line = digital_signature_html(
            record.get("supervisor_name", ""),
            record.get("supervisor_signed_at") or record.get("supervisor_date", ""),
        )
    elif supervisor_sign_href:
        supervisor_line = f'<a class="sign-btn no-print" href="{esc(supervisor_sign_href)}">Sign</a>'
    elif supervisor_image:
        supervisor_line = f"{supervisor_image}<span class='sig-name'>{esc(record.get('supervisor_name', ''))}</span>"
    else:
        supervisor_line = esc(record.get("supervisor_name", "")) or "________"
    supervisor_date = ""
    if record.get("supervisor_date"):
        supervisor_date = format_date(date.fromisoformat(record["supervisor_date"]))
    return f"""
    <h1>NEW YORK CITY HOUSING AUTHORITY</h1>
    <h2>SPECIAL BI-WEEKLY ALTERNATE TIME SHEET</h2>
    <div class="date-row">{field("form_date", "DATE", record.get("form_date", ""), "date")}</div>
    <div>{field("period_start", "Period start (Sunday)", record.get("period_start", ""), "date")}</div>
    <table class="meta">
      <tr>
        <td>{field("timekeeper", "TO, Timekeeper", record.get("timekeeper", ""))}</td>
        <td>{field("kronos_id", "Kronos ID #", record.get("kronos_id", ""))}</td>
      </tr>
      <tr><td colspan="2"><strong>FROM</strong> {esc(record["employee_name"])}</td></tr>
    </table>
    <p><strong>SUBJECT:</strong> SPECIAL BI-WEEKLY ALTERNATE TIME SHEET</p>
    <p>I understand:</p>
    <ol>
      <li>My ID card serves two purposes. It is the official NYCHA employee identification card as well as a time card. My ID card is to be carried at all times during working hours.</li>
      <li>The original of this form will be kept on file by my location for future reference during timekeeping audits.</li>
    </ol>
    <p>I certify that the time(s) stated below correctly reflect(s) my attendance for the following days.</p>
    {grid}
    <div class="reason"><strong>Reason for not using my Kronos identification card:</strong><br>{reason}</div>
    <table class="sign">
      <tr>
        <td>Employee's Signature <span class="line">{employee_line}</span></td>
        <td>Date <span class="line">{employee_date or "________"}</span></td>
      </tr>
      <tr>
        <td>Witnessed by <em>(If other than Supervisor)</em><br>{witness}</td>
        <td></td>
      </tr>
      <tr>
        <td>Supervisor's Approval <span class="line">{supervisor_line}</span></td>
        <td>Date <span class="line">{supervisor_date or "________"}</span></td>
      </tr>
    </table>
    <p class="fine">(Supervisor's signature indicates approval for the time(s) and location(s) indicated above only. It is <strong>not</strong> approval for the failure to use the Kronos time clock.)</p>
    <p class="fine">NYCHA 015.210A (Rev. 5/07)</p>
    """


def editor_page(user: dict, record: dict, errors: list[str], csrf: str) -> str:
    error_html = ""
    if errors:
        error_html = "<div class='error'>" + "".join(f"<div>{esc(error)}</div>" for error in errors) + "</div>"
    action = f"/sheet/{record['id']}" if record.get("id") else "/sheet"
    note = ""
    if record.get("return_note"):
        note = f"<div class='warnbox'>Supervisor note: {esc(record['return_note'])}</div>"
    body = f"""
    <div class="paper">
      {note}
      {error_html}
      <form id="sheet-form" method="post" action="{action}">
        <input type="hidden" name="csrf" value="{esc(csrf)}">
        {paper_block(record, True)}
        <p class="check"><label><input type="checkbox" name="certify" value="yes"> I certify that the hours above are my attendance.</label></p>
        <div class="actions no-print">
          <button class="primary" name="action" value="submit" type="submit">Submit to supervisor</button>
          <button class="secondary" name="action" value="rebuild" type="submit">Refill weekdays 9.00AM–5.00PM</button>
        </div>
      </form>
    </div>
    """
    return render_page("Time sheet", body, user)


def detail_page(user: dict, record: dict, csrf: str, error: str = "") -> str:
    issues: list[str] = []
    try:
        issues = review(to_model(record))
    except (ValueError, KeyError) as exc:
        issues = [str(exc)]
    note = ""
    if record.get("return_note") and record["status"] == "returned":
        note = f"<div class='warnbox'>Returned: {esc(record['return_note'])}</div>"
    controls = f"<div class='error no-print'>{esc(error)}</div>" if error else ""
    sign_href = ""
    if can(user, "approve") and record["status"] == "submitted" and record.get("supervisor_sign_method") != "digital":
        sign_href = f"/sheet/{record['id']}/sign"
    if can(user, "approve") and record["status"] == "submitted":
        controls += f"""
        <div class="no-print">
          <form method="post" action="/sheet/{record['id']}/return">
            <input type="hidden" name="csrf" value="{esc(csrf)}">
            <label for="note">Note if you are sending it back</label>
            <textarea id="note" name="note" placeholder="What needs to change"></textarea>
            <button class="warn" type="submit">Return to employee</button>
          </form>
        </div>
        """
    elif can(user, "submit") and record["employee_id"] == user["id"] and record["status"] == "returned":
        controls = f"<p class='no-print'><a class='button' href='/sheet/{record['id']}/edit'>Edit and submit again</a></p>"
    sign_top = f"<a class='sign-btn' href='{esc(sign_href)}'>Sign</a> " if sign_href else ""
    body = f"""
    <div class="paper">
      <p class="no-print">{status_badge(record['status'])} {sign_top}<a class="button" href="/sheet/{record['id']}/pdf">Download PDF</a></p>
      {note}
      <div class="no-print">{pattern_box(issues)}</div>
      {paper_block(record, False, sign_href)}
      {controls}
    </div>
    """
    return render_page("Time sheet", body, user)


def signature_page(user: dict, record: dict, csrf: str, role: str) -> str:
    stamp = digital_signature_html(user["name"], sign_stamp())
    if role == "supervisor":
        note = "Signing records this on the supervisor line and approves the time sheet."
        button = "Sign and approve"
        back = f"/sheet/{record['id']}"
    else:
        note = "Signing records this on the employee line. Submit the time sheet afterward."
        button = "Sign"
        back = f"/sheet/{record['id']}/edit"
    body = f"""
    <section class="card">
      <h1>Digital signature</h1>
      <p>{esc(note)}</p>
      <div class="sig-preview">{stamp}</div>
      <form method="post" action="/sheet/{record['id']}/sign">
        <input type="hidden" name="csrf" value="{esc(csrf)}">
        <button class="primary" type="submit">{esc(button)}</button>
      </form>
      <p><a href="{back}">Back to the time sheet</a></p>
    </section>
    """
    return render_page("Digital signature", body, user)


def checked(permissions: list[str], key: str) -> str:
    return " checked" if key in permissions else ""


def permission_choices(permissions: list[str]) -> str:
    boxes = []
    for key, label in PERMISSIONS:
        boxes.append(
            f"<label class='choice'><input type='checkbox' name='perm_{key}' value='yes'{checked(permissions, key)}> {esc(label)}</label>"
        )
    return "".join(boxes)


def permissions_from_form(form: dict[str, str]) -> list[str]:
    return [key for key, _label in PERMISSIONS if form.get(f"perm_{key}") == "yes"]


def clean_user_id(value: str) -> str:
    return value.strip().lower()


def admin_page(user: dict, error: str = "", notice: str = "", draft: dict | None = None) -> str:
    draft = draft or {}
    db = read_db()
    message = f"<div class='error'>{esc(error)}</div>" if error else ""
    if notice == "created":
        message = "<div class='ok'>User id created.</div>"
    elif notice == "saved":
        message = "<div class='ok'>Permissions saved.</div>"
    rows = []
    for account in db["users"]:
        shown = draft if draft.get("id") == account["id"] else account
        rows.append(
            "<tr>"
            f"<td><code>{esc(account['id'])}</code></td>"
            "<td>"
            f"<form method='post' action='/admin/users/{esc(account['id'])}'>"
            f"<input type='hidden' name='csrf' value='{esc(user.get('_csrf', ''))}'>"
            f"<label for='name-{esc(account['id'])}'>Name</label>"
            f"<input id='name-{esc(account['id'])}' name='name' type='text' value='{esc(shown.get('name', ''))}' required>"
            "<label>Permissions</label>"
            f"<div>{permission_choices(permissions_of(shown))}</div>"
            f"<label for='password-{esc(account['id'])}'>New password</label>"
            f"<input id='password-{esc(account['id'])}' name='password' type='password' autocomplete='new-password' placeholder='Leave blank to keep the current password'>"
            "<button class='primary' type='submit'>Save</button>"
            "</form>"
            "</td>"
            "</tr>"
        )
    create_permissions = permissions_of(draft) if draft.get("id") == "" else ["submit"]
    body = f"""
    <h1>Users</h1>
    <p class="muted">Create a user id and choose what that person can do. Submit files a time sheet. Review and approve signs off. Manage users opens this page.</p>
    {message}
    <section class="panel">
      <h2>New user</h2>
      <form method="post" action="/admin/users">
        <input type="hidden" name="csrf" value="{esc(user.get('_csrf', ''))}">
        <label for="new-id">User id</label>
        <input id="new-id" name="user_id" type="text" value="{esc(draft.get('user_id', ''))}" autocomplete="off" required>
        <label for="new-name">Name</label>
        <input id="new-name" name="name" type="text" value="{esc(draft.get('name', '') if draft.get('id') == '' else '')}" required>
        <label for="new-password">Password</label>
        <input id="new-password" name="password" type="password" autocomplete="new-password" required>
        <label>Permissions</label>
        <div>{permission_choices(create_permissions)}</div>
        <button class="primary" type="submit">Create user</button>
      </form>
    </section>
    <table class="list">
      <thead><tr><th>User id</th><th>Permissions</th></tr></thead>
      <tbody>{''.join(rows)}</tbody>
    </table>
    """
    return render_page("Users", body, user)


class App(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args) -> None:
        print(f"{self.address_string()} {fmt % args}")

    def do_GET(self) -> None:
        self.dispatch("GET")

    def do_POST(self) -> None:
        self.dispatch("POST")

    def dispatch(self, method: str) -> None:
        path = urlparse(self.path).path
        form = self.read_form() if method == "POST" else {}
        user, session = self.current_user()
        if user and session:
            user = {**user, "_csrf": session["csrf"]}
        try:
            if path == "/favicon.ico":
                self.send_error(404)
                return
            if path == "/" and method == "GET":
                if user:
                    self.redirect(landing(user))
                else:
                    self.respond(login_page())
                return
            if path == "/login" and method == "POST":
                self.login(form)
                return
            if path == "/logout" and method == "POST":
                self.logout(user, form)
                return
            if not user or not session:
                self.redirect("/")
                return
            if not self.csrf_ok(session, form, method):
                self.respond(render_page("Sign in again", "<div class='card'><p>The form expired. Sign in again.</p><p><a href='/'>Sign in</a></p></div>"), 403)
                return
            if path == "/home" and method == "GET":
                self.home(user)
                return
            if path == "/queue" and method == "GET":
                self.queue(user)
                return
            if path == "/admin" and method == "GET":
                self.show_admin(user)
                return
            if path == "/admin/users" and method == "POST":
                self.create_account(user, form)
                return
            if path == "/sheet/new" and method == "GET":
                self.new_sheet(user)
                return
            if path == "/sheet" and method == "POST":
                self.save_sheet(user, form, None)
                return
            parts = path.strip("/").split("/")
            if len(parts) == 3 and parts[0] == "admin" and parts[1] == "users" and method == "POST":
                self.update_account(user, form, parts[2])
                return
            if len(parts) >= 2 and parts[0] == "sheet" and parts[1].isdigit():
                sheet_id = int(parts[1])
                action = parts[2] if len(parts) > 2 else ""
                if method == "GET" and action == "":
                    self.view_sheet(user, sheet_id)
                    return
                if method == "GET" and action == "edit":
                    self.edit_sheet(user, sheet_id)
                    return
                if action == "sign" and method == "GET":
                    self.show_signature(user, sheet_id)
                    return
                if action == "sign" and method == "POST":
                    self.apply_signature(user, sheet_id)
                    return
                if method == "GET" and action == "pdf":
                    self.download_sheet(user, sheet_id)
                    return
                if method == "POST" and action == "":
                    self.save_sheet(user, form, sheet_id)
                    return
                if method == "POST" and action == "approve":
                    self.approve_sheet(user, form, sheet_id)
                    return
                if method == "POST" and action == "return":
                    self.return_sheet(user, form, sheet_id)
                    return
            self.send_error(404)
        except Exception as exc:  # noqa: BLE001
            self.respond(
                render_page("Something went wrong", f"<div class='card'><div class='error'>{esc(exc)}</div></div>", user),
                500,
            )

    def read_form(self) -> dict[str, str]:
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length).decode("utf-8") if length else ""
        parsed = parse_qs(raw, keep_blank_values=True)
        return {key: values[0] for key, values in parsed.items()}

    def current_user(self) -> tuple[dict | None, dict | None]:
        cookie = SimpleCookie(self.headers.get("Cookie"))
        morsel = cookie.get("session")
        if morsel is None:
            return None, None
        session = SESSIONS.get(morsel.value)
        if not session:
            return None, None
        user = find_user(read_db(), session["user_id"])
        return user, session

    def csrf_ok(self, session: dict, form: dict[str, str], method: str) -> bool:
        if method != "POST":
            return True
        return secrets.compare_digest(form.get("csrf", ""), session.get("csrf", ""))

    def respond(self, body: str, status: int = 200, headers: dict[str, str] | None = None) -> None:
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def redirect(self, location: str, headers: dict[str, str] | None = None) -> None:
        self.send_response(303)
        self.send_header("Location", location)
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()

    def set_session(self, user_id: str) -> dict[str, str]:
        token = secrets.token_urlsafe(32)
        session = {"user_id": user_id, "csrf": secrets.token_urlsafe(24)}
        SESSIONS[token] = session
        return {"Set-Cookie": f"session={token}; HttpOnly; SameSite=Lax; Path=/"}

    def login(self, form: dict[str, str]) -> None:
        username = form.get("username", "").strip()
        password = form.get("password", "")
        db = read_db()
        user = find_user(db, username)
        if not user or not check_password(user["password"], password):
            self.respond(login_page("Username or password is incorrect."), 401)
            return
        self.redirect(landing(user), self.set_session(user["id"]))

    def logout(self, user: dict | None, form: dict[str, str]) -> None:
        cookie = SimpleCookie(self.headers.get("Cookie"))
        morsel = cookie.get("session")
        if morsel and morsel.value in SESSIONS:
            session = SESSIONS[morsel.value]
            if secrets.compare_digest(form.get("csrf", ""), session.get("csrf", "")):
                SESSIONS.pop(morsel.value, None)
        self.redirect("/", {"Set-Cookie": "session=; HttpOnly; SameSite=Lax; Path=/; Max-Age=0"})

    def visible_sheet(self, user: dict, sheet_id: int) -> dict | None:
        sheet = find_sheet(read_db(), sheet_id)
        if not sheet:
            return None
        if can(user, "approve"):
            return sheet
        if can(user, "submit") and sheet["employee_id"] == user["id"]:
            return sheet
        return None

    def home(self, user: dict) -> None:
        if not can(user, "submit"):
            target = landing(user)
            if target == "/home":
                self.respond(render_page("No access", "<div class='card'><p>This account cannot submit time sheets.</p></div>", user))
            else:
                self.redirect(target)
            return
        sheets = [sheet for sheet in read_db()["sheets"] if sheet["employee_id"] == user["id"]]
        self.respond(home_page(user, list(reversed(sheets))))

    def queue(self, user: dict) -> None:
        if not can(user, "approve"):
            self.redirect(landing(user))
            return
        order = {"submitted": 0, "returned": 1, "approved": 2}
        sheets = sorted(
            (sheet for sheet in read_db()["sheets"] if sheet["status"] != "draft"),
            key=lambda sheet: (order.get(sheet["status"], 9), -sheet["id"]),
        )
        self.respond(queue_page(user, sheets))

    def show_admin(self, user: dict) -> None:
        if not can(user, "manage"):
            self.redirect(landing(user))
            return
        notice = parse_qs(urlparse(self.path).query).get("notice", [""])[0]
        if notice not in {"created", "saved"}:
            notice = ""
        self.respond(admin_page(user, notice=notice))

    def create_account(self, user: dict, form: dict[str, str]) -> None:
        if not can(user, "manage"):
            self.respond(render_page("Not allowed", "<div class='card'>Only an admin can create users.</div>", user), 403)
            return
        user_id = clean_user_id(form.get("user_id", ""))
        name = form.get("name", "").strip()
        password = form.get("password", "")
        permissions = permissions_from_form(form)
        errors: list[str] = []
        if not USER_ID.fullmatch(user_id):
            errors.append("User id must start with a letter and use only letters, numbers, dots, underscores, or hyphens.")
        if not name or len(name) > 80:
            errors.append("Name is required.")
        if len(password) < 4:
            errors.append("Password must be at least 4 characters.")
        if not permissions:
            errors.append("Choose at least one permission.")
        if user_id and find_user(read_db(), user_id):
            errors.append("That user id already exists.")
        draft = {"id": "", "user_id": user_id, "name": name, "permissions": permissions}
        if errors:
            self.respond(admin_page(user, error=" ".join(errors), draft=draft))
            return

        def add(db: dict) -> None:
            if find_user(db, user_id):
                return
            db["users"].append(make_user(user_id, name, password, permissions))

        mutate_db(add)
        self.redirect("/admin?notice=created")

    def update_account(self, user: dict, form: dict[str, str], user_id: str) -> None:
        if not can(user, "manage"):
            self.respond(render_page("Not allowed", "<div class='card'>Only an admin can change permissions.</div>", user), 403)
            return
        user_id = clean_user_id(user_id)
        if not find_user(read_db(), user_id):
            self.redirect("/admin")
            return
        name = form.get("name", "").strip()
        password = form.get("password", "")
        permissions = permissions_from_form(form)
        errors: list[str] = []
        if not name or len(name) > 80:
            errors.append("Name is required.")
        if password and len(password) < 4:
            errors.append("Password must be at least 4 characters.")
        if not permissions:
            errors.append("Choose at least one permission.")
        draft = {"id": user_id, "name": name, "permissions": permissions}
        if errors:
            self.respond(admin_page(user, error=" ".join(errors), draft=draft))
            return
        blocked = {"error": ""}

        def change(db: dict) -> None:
            account = find_user(db, user_id)
            if not account:
                blocked["error"] = "That user id was not found."
                return
            others = [item for item in db["users"] if item["id"] != user_id and can(item, "manage")]
            if "manage" not in permissions and not others:
                blocked["error"] = "Keep at least one user who can manage users."
                return
            account["name"] = name
            account["permissions"] = permissions
            account["role"] = role_name(permissions)
            if password:
                account["password"] = hash_password(password)

        mutate_db(change)
        if blocked["error"]:
            self.respond(admin_page(user, error=blocked["error"], draft=draft))
            return
        self.redirect("/admin?notice=saved")

    def new_sheet(self, user: dict) -> None:
        if not can(user, "submit"):
            self.redirect(landing(user))
            return
        today = date.today()
        record = new_record(user, today, sunday_on_or_before(today))
        self.respond(editor_page(user, record, [], user["_csrf"]))

    def edit_sheet(self, user: dict, sheet_id: int) -> None:
        sheet = self.visible_sheet(user, sheet_id)
        if not sheet or not can(user, "submit") or sheet["employee_id"] != user["id"] or sheet["status"] not in {"draft", "returned"}:
            self.redirect(landing(user))
            return
        self.respond(editor_page(user, sheet, [], user["_csrf"]))

    def save_sheet(self, user: dict, form: dict[str, str], sheet_id: int | None) -> None:
        if not can(user, "submit"):
            self.respond(render_page("Not allowed", "<div class='card'>Only an employee can submit a time sheet.</div>", user), 403)
            return
        existing = None
        if sheet_id is not None:
            existing = self.visible_sheet(user, sheet_id)
            if not existing or existing["status"] not in {"draft", "returned"}:
                self.redirect("/home")
                return
        record, errors = record_from_post(form, user, existing)
        if form.get("action") == "rebuild" or errors:
            if record.get("id") is None and existing:
                record["id"] = existing["id"]
            self.respond(editor_page(user, record, [] if form.get("action") == "rebuild" else errors, user["_csrf"]))
            return
        if form.get("action") == "sign":
            record["status"] = "returned" if existing and existing["status"] == "returned" else "draft"
        else:
            record["status"] = "submitted"
            record["return_note"] = ""

        def save(db: dict) -> None:
            if existing:
                stored = find_sheet(db, existing["id"])
                if stored:
                    stored.update(record)
                    stored["id"] = existing["id"]
            else:
                record["id"] = db["next_id"]
                db["next_id"] += 1
                db["sheets"].append(record)

        mutate_db(save)
        if form.get("action") == "sign":
            self.redirect(f"/sheet/{record['id']}/sign")
            return
        self.redirect(f"/sheet/{record['id']}")

    def show_signature(self, user: dict, sheet_id: int) -> None:
        sheet = self.visible_sheet(user, sheet_id)
        if not sheet:
            self.redirect(landing(user))
            return
        if can(user, "submit") and sheet["employee_id"] == user["id"] and sheet["status"] in {"draft", "returned"}:
            self.respond(signature_page(user, sheet, user["_csrf"], "employee"))
            return
        if can(user, "approve") and sheet["status"] == "submitted":
            self.respond(signature_page(user, sheet, user["_csrf"], "supervisor"))
            return
        self.redirect(f"/sheet/{sheet_id}")

    def apply_signature(self, user: dict, sheet_id: int) -> None:
        sheet = find_sheet(read_db(), sheet_id)
        if not sheet:
            self.redirect(landing(user))
            return
        if can(user, "submit") and sheet["employee_id"] == user["id"] and sheet["status"] in {"draft", "returned"}:
            signed_at = sign_stamp()

            def sign_employee(db: dict) -> None:
                stored = find_sheet(db, sheet_id)
                if stored and stored["employee_id"] == user["id"] and stored["status"] in {"draft", "returned"}:
                    stored["employee_signature"] = ""
                    stored["employee_sign_method"] = "digital"
                    stored["employee_sign_date"] = date.today().isoformat()
                    stored["employee_signed_at"] = signed_at

            mutate_db(sign_employee)
            self.redirect(f"/sheet/{sheet_id}/edit")
            return
        if can(user, "approve") and sheet["status"] == "submitted":
            self.approve_sheet(user, {"signature_name": user["name"]}, sheet_id)
            return
        self.redirect(f"/sheet/{sheet_id}")

    def download_sheet(self, user: dict, sheet_id: int) -> None:
        sheet = self.visible_sheet(user, sheet_id)
        if not sheet:
            self.redirect(landing(user))
            return
        try:
            pdf, filename = sheet_pdf(sheet)
        except (OSError, RuntimeError, ValueError, KeyError) as exc:
            self.respond(
                render_page("PDF", f"<div class='card'><div class='error'>{esc(exc)}</div></div>", user),
                500,
            )
            return
        self.send_response(200)
        self.send_header("Content-Type", "application/pdf")
        self.send_header("Content-Disposition", f'attachment; filename="{filename}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(pdf)))
        self.end_headers()
        self.wfile.write(pdf)

    def view_sheet(self, user: dict, sheet_id: int) -> None:
        sheet = self.visible_sheet(user, sheet_id)
        if not sheet:
            self.redirect(landing(user))
            return
        if sheet["status"] == "draft" and sheet["employee_id"] == user["id"] and can(user, "submit"):
            self.redirect(f"/sheet/{sheet_id}/edit")
            return
        self.respond(detail_page(user, sheet, user["_csrf"]))

    def approve_sheet(self, user: dict, form: dict[str, str], sheet_id: int) -> None:
        if not can(user, "approve"):
            self.respond(render_page("Not allowed", "<div class='card'>Only a supervisor can approve a time sheet.</div>", user), 403)
            return
        signature = user["name"].strip()
        sheet = find_sheet(read_db(), sheet_id)
        if not sheet or sheet["status"] != "submitted":
            self.redirect("/queue")
            return
        if not signature:
            self.respond(detail_page(user, sheet, user["_csrf"], "This account needs a name before it can approve."))
            return

        def approve(db: dict) -> None:
            stored = find_sheet(db, sheet_id)
            if stored and stored["status"] == "submitted":
                stored["status"] = "approved"
                stored["supervisor_name"] = signature
                stored["supervisor_date"] = date.today().isoformat()
                stored["supervisor_signature"] = ""
                stored["supervisor_sign_method"] = "digital"
                stored["supervisor_signed_at"] = sign_stamp()
                stored["supervisor_id"] = user["id"]

        mutate_db(approve)
        self.redirect(f"/sheet/{sheet_id}")

    def return_sheet(self, user: dict, form: dict[str, str], sheet_id: int) -> None:
        if not can(user, "approve"):
            self.respond(render_page("Not allowed", "<div class='card'>Only a supervisor can return a time sheet.</div>", user), 403)
            return
        note = form.get("note", "").strip() or "Please correct this time sheet."

        def send_back(db: dict) -> None:
            stored = find_sheet(db, sheet_id)
            if stored and stored["status"] == "submitted":
                stored["status"] = "returned"
                stored["return_note"] = note
                stored["supervisor_name"] = ""
                stored["supervisor_date"] = ""
                stored["supervisor_signature"] = ""
                stored["supervisor_sign_method"] = ""
                stored["supervisor_signed_at"] = ""

        mutate_db(send_back)
        self.redirect(f"/sheet/{sheet_id}")


def sheet_pdf(record: dict) -> tuple[bytes, str]:
    """Print one saved time sheet to PDF bytes."""
    sheet = to_model(record)
    with tempfile.TemporaryDirectory(prefix="timesheet-download-") as folder:
        html_path = Path(folder) / "sheet.html"
        html_path.write_text(render_html(sheet), encoding="utf-8")
        pdf_path = write_pdf(html_path)
        data = pdf_path.read_bytes()
    filename = f"timesheet_{employee_slug(sheet.employee)}_{sheet.form_date.isoformat()}.pdf"
    return data, filename


def main() -> None:
    load_db()
    server = ThreadingHTTPServer((HOST, PORT), App)
    print(f"Time sheet site: http://{HOST}:{PORT}")
    print(f"Employee login: divya / {EMPLOYEE_PASSWORD}")
    print(f"Supervisor login: supervisor / {SUPERVISOR_PASSWORD}")
    print(f"Admin login: admin / {ADMIN_PASSWORD}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
