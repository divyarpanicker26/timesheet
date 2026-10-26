"""Local timesheet site.

Employees sign in, fill the bi-weekly form, and submit it.
Supervisors sign in, review the weekday pattern, and approve with their own signature.

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
from datetime import date
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


def hash_password(password: str, salt: str | None = None) -> str:
    salt = salt or secrets.token_hex(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000)
    return f"{salt}${digest.hex()}"


def check_password(stored: str, password: str) -> bool:
    salt, digest = stored.split("$", 1)
    test = hashlib.pbkdf2_hmac("sha256", password.encode(), salt.encode(), 100_000).hex()
    return secrets.compare_digest(test, digest)


def seed_db() -> dict:
    return {
        "next_id": 1,
        "users": [
            {
                "id": "divya",
                "name": "Divya Radhakrishnan",
                "role": "employee",
                "password": hash_password(EMPLOYEE_PASSWORD),
            },
            {
                "id": "supervisor",
                "name": "Supervisor",
                "role": "supervisor",
                "password": hash_password(SUPERVISOR_PASSWORD),
            },
        ],
        "sheets": [],
    }


def load_db() -> dict:
    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    if not DATA_PATH.exists():
        db = seed_db()
        DATA_PATH.write_text(json.dumps(db, indent=2), encoding="utf-8")
        return db
    return json.loads(DATA_PATH.read_text(encoding="utf-8"))


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
        "employee_signature": "",
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
        supervisor_name=record.get("supervisor_name", ""),
        supervisor_date=date.fromisoformat(record["supervisor_date"]) if record.get("supervisor_date") else None,
        supervisor_signature=record.get("supervisor_signature", ""),
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
        "employee_signature": "",
        "return_note": "",
    }
    signature = clean_signature(form.get("employee_signature", ""))
    if form.get("action") == "submit" and not signature:
        errors.append("Draw your signature before submitting.")
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
        "employee_signature": signature or base.get("employee_signature", ""),
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
  .badge.returned { color: #9b1c1c; }
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
  canvas.pad {
    width: min(520px, 100%);
    height: 140px;
    border: 1px solid #222;
    border-radius: 8px;
    background: #fff;
    touch-action: none;
    cursor: crosshair;
  }
  img.signature { height: 52px; vertical-align: middle; }
  .sig-name { margin-left: 8px; }
  .pad-error { color: #9b1c1c; margin: 6px 0 0; }
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
        home = "/queue" if user["role"] == "supervisor" else "/home"
        nav = (
            "<header class='nav'><div><a href='"
            + home
            + "'>NYCHA Time Sheet</a></div><div>"
            + esc(user["name"])
            + " · "
            + esc(user["role"])
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
      <p class="muted">Employees submit hours. Supervisors review and approve.</p>
      <div class="accounts">
        <div><strong>Employee</strong> · username <code>divya</code> · password <code>{EMPLOYEE_PASSWORD}</code></div>
        <div><strong>Supervisor</strong> · username <code>supervisor</code> · password <code>{SUPERVISOR_PASSWORD}</code></div>
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
    label = {"submitted": "Waiting for supervisor", "approved": "Approved", "returned": "Returned"}.get(
        status, status
    )
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
        rows.append(
            "<tr>"
            f"<td>{esc(sheet['employee_name'])}</td>"
            f"<td>{esc(format_date(date.fromisoformat(sheet['form_date'])))}</td>"
            f"<td>{esc(pattern)}</td>"
            f"<td>{status_badge(sheet['status'])}</td>"
            f"<td><a href='/sheet/{sheet['id']}'>Review</a> · <a href='/sheet/{sheet['id']}/pdf'>Download PDF</a></td>"
            "</tr>"
        )
    table = (
        "<table class='list'><thead><tr><th>Employee</th><th>Form date</th><th>Pattern</th><th>Status</th><th></th></tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
        if rows
        else "<p class='muted'>No time sheets have been submitted.</p>"
    )
    body = f"<h1>Supervisor queue</h1><p class='muted'>Open a sheet to review the hours and approve it.</p>{table}"
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


def signature_pad(canvas_id: str, field_name: str, label: str) -> str:
    return f"""
    <div class="no-print">
      <div>{esc(label)}</div>
      <canvas class="pad" id="{canvas_id}" width="520" height="140"></canvas>
      <div><button type="button" class="secondary" id="{canvas_id}-clear">Clear</button></div>
      <p class="pad-error" id="{canvas_id}-error" hidden>Draw your signature before continuing.</p>
      <input type="hidden" name="{field_name}" id="{field_name}">
    </div>
    """


SIGN_SCRIPT = """
<script>
function attachPad(canvasId, inputId, formId) {
  const canvas = document.getElementById(canvasId);
  const input = document.getElementById(inputId);
  const form = document.getElementById(formId);
  if (!canvas || !input || !form) return;
  const ctx = canvas.getContext("2d");
  ctx.lineWidth = 2.4;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  ctx.strokeStyle = "#142033";
  let drawing = false;
  function point(event) {
    const rect = canvas.getBoundingClientRect();
    const source = event.touches ? event.touches[0] : event;
    return {
      x: (source.clientX - rect.left) * canvas.width / rect.width,
      y: (source.clientY - rect.top) * canvas.height / rect.height
    };
  }
  function start(event) {
    drawing = true;
    const p = point(event);
    ctx.beginPath();
    ctx.moveTo(p.x, p.y);
    event.preventDefault();
  }
  function move(event) {
    if (!drawing) return;
    const p = point(event);
    ctx.lineTo(p.x, p.y);
    ctx.stroke();
    event.preventDefault();
  }
  function stop() { drawing = false; }
  canvas.addEventListener("mousedown", start);
  canvas.addEventListener("mousemove", move);
  window.addEventListener("mouseup", stop);
  canvas.addEventListener("touchstart", start, {passive: false});
  canvas.addEventListener("touchmove", move, {passive: false});
  canvas.addEventListener("touchend", stop);
  const clear = document.getElementById(canvasId + "-clear");
  if (clear) {
    clear.addEventListener("click", () => ctx.clearRect(0, 0, canvas.width, canvas.height));
  }
  form.addEventListener("submit", (event) => {
    const submitter = event.submitter;
    if (submitter && submitter.value === "rebuild") return;
    const probe = document.createElement("canvas");
    probe.width = canvas.width;
    probe.height = canvas.height;
    if (canvas.toDataURL() === probe.toDataURL()) {
      event.preventDefault();
      const message = document.getElementById(canvasId + "-error");
      if (message) message.hidden = false;
      return;
    }
    input.value = canvas.toDataURL("image/png");
  });
}
</script>
"""


def paper_block(record: dict, editable: bool) -> str:
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
    employee_signed = record.get("status") in {"submitted", "approved", "returned"}
    employee_image = signature_markup(record.get("employee_signature", ""))
    if editable:
        employee_line = signature_pad("employee-pad", "employee_signature", "Draw your signature")
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
    if supervisor_image:
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
      {SIGN_SCRIPT}
      <script>attachPad("employee-pad", "employee_signature", "sheet-form");</script>
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
    if user["role"] == "supervisor" and record["status"] == "submitted":
        controls += f"""
        <div class="no-print">
          <form id="approve-form" method="post" action="/sheet/{record['id']}/approve">
            <input type="hidden" name="csrf" value="{esc(csrf)}">
            <label for="signature_name">Name to record on the supervisor line</label>
            <input id="signature_name" name="signature_name" type="text" value="{esc(user['name'])}" required>
            {signature_pad("supervisor-pad", "supervisor_signature", "Draw your approval signature")}
            <div class="actions">
              <button class="primary" type="submit">Approve</button>
            </div>
          </form>
          {SIGN_SCRIPT}
          <script>attachPad("supervisor-pad", "supervisor_signature", "approve-form");</script>
          <form method="post" action="/sheet/{record['id']}/return">
            <input type="hidden" name="csrf" value="{esc(csrf)}">
            <label for="note">Note if you are sending it back</label>
            <textarea id="note" name="note" placeholder="What needs to change"></textarea>
            <button class="warn" type="submit">Return to employee</button>
          </form>
        </div>
        """
    elif user["role"] == "employee" and record["status"] == "returned":
        controls = f"<p class='no-print'><a class='button' href='/sheet/{record['id']}/edit'>Edit and submit again</a></p>"
    body = f"""
    <div class="paper">
      <p class="no-print">{status_badge(record['status'])} <a class="button" href="/sheet/{record['id']}/pdf">Download PDF</a></p>
      {note}
      <div class="no-print">{pattern_box(issues)}</div>
      {paper_block(record, False)}
      {controls}
    </div>
    """
    return render_page("Time sheet", body, user)


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
                    self.redirect("/queue" if user["role"] == "supervisor" else "/home")
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
            if path == "/sheet/new" and method == "GET":
                self.new_sheet(user)
                return
            if path == "/sheet" and method == "POST":
                self.save_sheet(user, form, None)
                return
            parts = path.strip("/").split("/")
            if len(parts) >= 2 and parts[0] == "sheet" and parts[1].isdigit():
                sheet_id = int(parts[1])
                action = parts[2] if len(parts) > 2 else ""
                if method == "GET" and action == "":
                    self.view_sheet(user, sheet_id)
                    return
                if method == "GET" and action == "edit":
                    self.edit_sheet(user, sheet_id)
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
        self.redirect("/queue" if user["role"] == "supervisor" else "/home", self.set_session(user["id"]))

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
        if user["role"] == "employee" and sheet["employee_id"] != user["id"]:
            return None
        return sheet

    def home(self, user: dict) -> None:
        if user["role"] != "employee":
            self.redirect("/queue")
            return
        sheets = [sheet for sheet in read_db()["sheets"] if sheet["employee_id"] == user["id"]]
        self.respond(home_page(user, list(reversed(sheets))))

    def queue(self, user: dict) -> None:
        if user["role"] != "supervisor":
            self.redirect("/home")
            return
        order = {"submitted": 0, "returned": 1, "approved": 2}
        sheets = sorted(read_db()["sheets"], key=lambda sheet: (order.get(sheet["status"], 9), -sheet["id"]))
        self.respond(queue_page(user, sheets))

    def new_sheet(self, user: dict) -> None:
        if user["role"] != "employee":
            self.redirect("/queue")
            return
        today = date.today()
        record = new_record(user, today, sunday_on_or_before(today))
        self.respond(editor_page(user, record, [], user["_csrf"]))

    def edit_sheet(self, user: dict, sheet_id: int) -> None:
        sheet = self.visible_sheet(user, sheet_id)
        if not sheet or user["role"] != "employee" or sheet["status"] != "returned":
            self.redirect("/home")
            return
        self.respond(editor_page(user, sheet, [], user["_csrf"]))

    def save_sheet(self, user: dict, form: dict[str, str], sheet_id: int | None) -> None:
        if user["role"] != "employee":
            self.respond(render_page("Not allowed", "<div class='card'>Only an employee can submit a time sheet.</div>", user), 403)
            return
        existing = None
        if sheet_id is not None:
            existing = self.visible_sheet(user, sheet_id)
            if not existing or existing["status"] != "returned":
                self.redirect("/home")
                return
        record, errors = record_from_post(form, user, existing)
        if form.get("action") == "rebuild" or errors:
            if record.get("id") is None and existing:
                record["id"] = existing["id"]
            self.respond(editor_page(user, record, [] if form.get("action") == "rebuild" else errors, user["_csrf"]))
            return
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
        self.redirect(f"/sheet/{record['id']}")

    def download_sheet(self, user: dict, sheet_id: int) -> None:
        sheet = self.visible_sheet(user, sheet_id)
        if not sheet:
            self.redirect("/home" if user["role"] == "employee" else "/queue")
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
            self.redirect("/home" if user["role"] == "employee" else "/queue")
            return
        self.respond(detail_page(user, sheet, user["_csrf"]))

    def approve_sheet(self, user: dict, form: dict[str, str], sheet_id: int) -> None:
        if user["role"] != "supervisor":
            self.respond(render_page("Not allowed", "<div class='card'>Only a supervisor can approve a time sheet.</div>", user), 403)
            return
        signature = form.get("signature_name", "").strip()
        image = clean_signature(form.get("supervisor_signature", ""))
        sheet = find_sheet(read_db(), sheet_id)
        if not sheet or sheet["status"] != "submitted":
            self.redirect("/queue")
            return
        if not signature or not image:
            self.respond(detail_page(user, sheet, user["_csrf"], "Draw your signature and enter your name."))
            return

        def approve(db: dict) -> None:
            stored = find_sheet(db, sheet_id)
            if stored and stored["status"] == "submitted":
                stored["status"] = "approved"
                stored["supervisor_name"] = signature
                stored["supervisor_date"] = date.today().isoformat()
                stored["supervisor_signature"] = image
                stored["supervisor_id"] = user["id"]

        mutate_db(approve)
        self.redirect(f"/sheet/{sheet_id}")

    def return_sheet(self, user: dict, form: dict[str, str], sheet_id: int) -> None:
        if user["role"] != "supervisor":
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
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopped.")


if __name__ == "__main__":
    main()
