"""Build and review NYCHA special bi-weekly alternate time sheets.

Pattern:
- Weekdays on or before the form date get 9.00AM-5.00PM.
- Saturday, Sunday, and future days stay blank.
- Reason is "Approved remote work".
- The employee name and date are filled.
- Supervisor approval is left blank for the supervisor to sign.

Pass --batch with a CSV to review several sheets and write one report.
Pass --inbox with a shared folder of employee PDFs to read and review them.
Each form and the review report are also saved as PDF.

The sample sheet repeats 10/22/2026 on Friday. Friday of that week is 10/23/2026.
This script uses the calendar date for each row.

Reading employee PDFs requires PyMuPDF. Requires Python 3.9+.
"""

from __future__ import annotations

import argparse
import csv
import html
import os
import re
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from urllib.parse import quote
from datetime import date, datetime, timedelta
from pathlib import Path

DAY_NAMES = (
    "Sunday",
    "Monday",
    "Tuesday",
    "Wednesday",
    "Thursday",
    "Friday",
    "Saturday",
)
WEEKEND = {"Sunday", "Saturday"}
DEFAULT_START = "9.00AM"
DEFAULT_FINISH = "5.00PM"
DEFAULT_REASON = "Approved remote work"
DEFAULT_EMPLOYEE = "Divya Radhakrishnan"
FORM_CODE = "NYCHA 015.210A (Rev. 5/07)"


@dataclass(frozen=True)
class DayEntry:
    day_name: str
    work_date: date | None
    start: str
    finish: str

    @property
    def worked(self) -> bool:
        return self.work_date is not None


@dataclass(frozen=True)
class Timesheet:
    form_date: date
    period_start: date
    employee: str
    timekeeper: str
    kronos_id: str
    reason: str
    start_time: str
    finish_time: str
    weeks: tuple[tuple[DayEntry, ...], tuple[DayEntry, ...]]


def sunday_on_or_before(day: date) -> date:
    """Return the Sunday that opens the calendar week containing day."""
    return day - timedelta(days=(day.weekday() + 1) % 7)


def parse_date(value: str) -> date:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(
            f"use YYYY-MM-DD, got {value!r}"
        ) from exc


def build_timesheet(
    form_date: date,
    period_start: date | None = None,
    employee: str = DEFAULT_EMPLOYEE,
    timekeeper: str = "",
    kronos_id: str = "",
    reason: str = DEFAULT_REASON,
    start_time: str = DEFAULT_START,
    finish_time: str = DEFAULT_FINISH,
) -> Timesheet:
    """Fill the two-week grid through form_date."""
    start = period_start or sunday_on_or_before(form_date)
    if start.strftime("%A") != "Sunday":
        raise ValueError(f"period start must be a Sunday, got {start.isoformat()}")

    weeks: list[tuple[DayEntry, ...]] = []
    for week_index in range(2):
        week_start = start + timedelta(days=7 * week_index)
        rows: list[DayEntry] = []
        for offset, day_name in enumerate(DAY_NAMES):
            current = week_start + timedelta(days=offset)
            if day_name in WEEKEND or current > form_date:
                rows.append(DayEntry(day_name, None, "", ""))
            else:
                rows.append(DayEntry(day_name, current, start_time, finish_time))
        weeks.append(tuple(rows))

    return Timesheet(
        form_date=form_date,
        period_start=start,
        employee=employee.strip(),
        timekeeper=timekeeper.strip(),
        kronos_id=kronos_id.strip(),
        reason=reason.strip(),
        start_time=start_time,
        finish_time=finish_time,
        weeks=(weeks[0], weeks[1]),
    )


def review(sheet: Timesheet) -> list[str]:
    """Return pattern problems. An empty list means the grid matches the pattern."""
    issues: list[str] = []
    if not sheet.employee:
        issues.append("Employee name is blank.")
    if sheet.reason != DEFAULT_REASON:
        issues.append(
            f"Reason is {sheet.reason!r}; the pattern uses {DEFAULT_REASON!r}."
        )

    seen: dict[date, str] = {}
    for week_number, week in enumerate(sheet.weeks, start=1):
        week_start = sheet.period_start + timedelta(days=7 * (week_number - 1))
        for offset, entry in enumerate(week):
            expected_name = DAY_NAMES[offset]
            expected_date = week_start + timedelta(days=offset)
            label = f"Week {week_number} {entry.day_name}"

            if entry.day_name != expected_name:
                issues.append(f"{label} is in the wrong row.")

            if entry.day_name in WEEKEND:
                if entry.worked or entry.start or entry.finish:
                    issues.append(f"{label} is a weekend and must stay blank.")
                continue

            if expected_date > sheet.form_date:
                if entry.worked:
                    issues.append(
                        f"{label} is after the form date and must stay blank."
                    )
                continue

            if not entry.worked:
                issues.append(f"{label} is a past weekday and has no hours.")
                continue

            if entry.work_date != expected_date:
                issues.append(
                    f"{label} date is {format_date(entry.work_date)}, "
                    f"expected {format_date(expected_date)}."
                )
            elif entry.work_date.strftime("%A") != entry.day_name:
                issues.append(f"{label} date does not fall on {entry.day_name}.")

            if entry.start != DEFAULT_START or entry.finish != DEFAULT_FINISH:
                issues.append(
                    f"{label} hours are {entry.start or 'blank'}-"
                    f"{entry.finish or 'blank'}, expected "
                    f"{DEFAULT_START}-{DEFAULT_FINISH}."
                )

            if entry.work_date in seen:
                issues.append(
                    f"{label} repeats {format_date(entry.work_date)}, "
                    f"already used on {seen[entry.work_date]}."
                )
            else:
                seen[entry.work_date] = label

    return issues


def format_date(day: date | None) -> str:
    if day is None:
        return ""
    return day.strftime("%m/%d/%Y")


def render_text(sheet: Timesheet, issues: list[str]) -> str:
    lines = [
        "NEW YORK CITY HOUSING AUTHORITY",
        "SPECIAL BI-WEEKLY ALTERNATE TIME SHEET",
        f"DATE: {format_date(sheet.form_date)}",
        f"TO: {sheet.timekeeper or '________'}, Timekeeper",
        f"FROM: {sheet.employee}",
        f"Kronos ID #: {sheet.kronos_id or '________'}",
        f"Period start (Sunday): {format_date(sheet.period_start)}",
        "",
    ]
    for week_number, week in enumerate(sheet.weeks, start=1):
        lines.append(f"WEEK {week_number}")
        lines.append(f"{'Day':<12}{'Date':<14}{'Start':<10}Finish")
        for entry in week:
            lines.append(
                f"{entry.day_name:<12}{format_date(entry.work_date):<14}"
                f"{entry.start:<10}{entry.finish}"
            )
        lines.append("")
    lines.append(f"Reason: {sheet.reason}")
    lines.append(f"Employee signature: {sheet.employee}")
    lines.append(f"Employee date: {format_date(sheet.form_date)}")
    lines.append("Supervisor approval: left blank for the supervisor to sign")
    lines.append("")
    if issues:
        lines.append("Pattern check: needs corrections")
        lines.extend(f"- {issue}" for issue in issues)
    else:
        lines.append("Pattern check: weekday schedule matches.")
        lines.append("Sign the employee line, then send it to your supervisor.")
    return "\n".join(lines) + "\n"


def render_html(sheet: Timesheet) -> str:
    def cell(value: str, shade: bool) -> str:
        css = " class='shade'" if shade else ""
        return f"<td{css}>{html.escape(value)}</td>"

    def week_table(title: str, week: tuple[DayEntry, ...]) -> str:
        body = []
        for entry in week:
            shade = not entry.worked
            body.append(
                "<tr>"
                f"{cell(entry.day_name, shade)}"
                f"{cell(format_date(entry.work_date), shade)}"
                f"{cell(entry.start, shade)}"
                f"{cell(entry.finish, shade)}"
                "</tr>"
            )
        return f"""
        <table class="week">
          <thead>
            <tr><th colspan="4">{html.escape(title)}</th></tr>
            <tr>
              <th>Day</th>
              <th>MM/DD/YY</th>
              <th>Start Time</th>
              <th>Finish Time</th>
            </tr>
          </thead>
          <tbody>
            {''.join(body)}
          </tbody>
        </table>
        """

    timekeeper = html.escape(sheet.timekeeper) or "&nbsp;"
    kronos = html.escape(sheet.kronos_id) or "&nbsp;"
    employee = html.escape(sheet.employee)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Special Bi-Weekly Alternate Time Sheet — {html.escape(format_date(sheet.form_date))}</title>
  <style>
    @page {{ size: letter; margin: 0.55in; }}
    * {{
      box-sizing: border-box;
      -webkit-print-color-adjust: exact;
      print-color-adjust: exact;
    }}
    html {{
      color-scheme: light;
      background: #fff;
    }}
    body {{
      margin: 0 auto;
      max-width: 8in;
      background: #fff;
      color: #1a1a1a;
      font-family: "Times New Roman", Times, serif;
      font-size: 11.5pt;
      line-height: 1.35;
    }}
    h1, h2 {{
      margin: 0;
      text-align: center;
      font-weight: 700;
      letter-spacing: 0.02em;
    }}
    h1 {{ font-size: 14pt; }}
    h2 {{ font-size: 12.5pt; margin-bottom: 10px; }}
    .date-row {{ text-align: right; margin: 8px 0 12px; }}
    .date-row span {{
      display: inline-block;
      min-width: 1.3in;
      border-bottom: 1px solid #222;
      text-align: center;
      font-weight: 700;
    }}
    .meta {{ width: 100%; border-collapse: collapse; margin-bottom: 10px; }}
    .meta td {{ border: none; padding: 3px 0; vertical-align: bottom; }}
    .fill {{
      display: inline-block;
      min-width: 2.4in;
      border-bottom: 1px solid #222;
      padding: 0 6px 1px;
      background: #eef3fb;
    }}
    .id {{ min-width: 1.5in; }}
    ol {{ margin: 6px 0 8px 1.2em; padding: 0; }}
    li {{ margin-bottom: 4px; }}
    .grid {{ width: 100%; border-collapse: collapse; margin-top: 8px; }}
    .grid > tbody > tr > td {{ width: 50%; vertical-align: top; padding: 0; }}
    table.week {{ width: 100%; border-collapse: collapse; }}
    table.week th, table.week td {{
      border: 1px solid #222;
      padding: 3px 6px;
      text-align: center;
      font-size: 10.5pt;
      background: #fff;
      color: #1a1a1a;
    }}
    table.week th {{ background: #d9e2f3; }}
    table.week td:first-child, table.week th:first-child {{ text-align: left; }}
    td.shade {{ background: #eef3fb; height: 22px; }}
    .reason {{
      border: 1px solid #222;
      border-top: none;
      background: #eef3fb;
      padding: 6px 8px 10px;
    }}
    .reason strong {{ display: block; margin-bottom: 4px; }}
    .signatures {{ width: 100%; border-collapse: collapse; margin-top: 14px; }}
    .signatures td {{ border: none; padding: 6px 8px 6px 0; vertical-align: bottom; }}
    .line {{
      display: inline-block;
      min-width: 2.6in;
      border-bottom: 1px solid #222;
      padding: 0 4px;
    }}
    .short {{ min-width: 1.3in; text-align: center; }}
    .fine {{ font-size: 9pt; margin-top: 8px; }}
    .form-code {{ font-size: 8.5pt; margin-top: 12px; }}
    .blank {{ color: transparent; }}
  </style>
</head>
<body>
  <h1>NEW YORK CITY HOUSING AUTHORITY</h1>
  <h2>SPECIAL BI-WEEKLY ALTERNATE TIME SHEET</h2>
  <div class="date-row">DATE: <span>{html.escape(format_date(sheet.form_date))}</span></div>

  <table class="meta">
    <tr>
      <td>TO: <span class="fill">{timekeeper}</span>, Timekeeper</td>
      <td style="text-align:right">Kronos ID # <span class="fill id">{kronos}</span></td>
    </tr>
    <tr>
      <td colspan="2">FROM: <span class="fill">{employee}</span></td>
    </tr>
  </table>

  <p><strong>SUBJECT:</strong> &nbsp; SPECIAL BI-WEEKLY ALTERNATE TIME SHEET</p>
  <p>I understand:</p>
  <ol>
    <li>My ID card serves two purposes. It is the official NYCHA employee identification card as well as a time card. My ID card is to be carried at all times during working hours.</li>
    <li>The original of this form will be kept on file by my location for future reference during timekeeping audits.</li>
  </ol>
  <p>I certify that the time(s) stated below correctly reflect(s) my attendance for the following days.</p>

  <table class="grid">
    <tr>
      <td>{week_table("WEEK 1", sheet.weeks[0])}</td>
      <td>{week_table("WEEK 2", sheet.weeks[1])}</td>
    </tr>
  </table>
  <div class="reason">
    <strong>Reason for not using my Kronos identification card:</strong>
    {html.escape(sheet.reason)}
  </div>

  <table class="signatures">
    <tr>
      <td>Employee's Signature <span class="line">{employee}</span></td>
      <td>Date <span class="line short">{html.escape(format_date(sheet.form_date))}</span></td>
    </tr>
    <tr>
      <td>Witnessed by<br><em>(If other than Supervisor)</em> <span class="line blank">.</span></td>
      <td>Date <span class="line short blank">.</span></td>
    </tr>
    <tr>
      <td>Supervisor's Approval <span class="line blank">.</span></td>
      <td>Date <span class="line short blank">.</span></td>
    </tr>
  </table>
  <p class="fine">(Supervisor's signature indicates approval for the time(s) and location(s) indicated above only. It is <strong>not</strong> approval for the failure to use the Kronos time clock.)</p>
  <p class="form-code">{FORM_CODE}</p>
</body>
</html>
"""


def parse_user_date(value: str) -> date:
    text = value.strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    raise ValueError(f"use YYYY-MM-DD or MM/DD/YYYY, got {value!r}")


def employee_slug(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "employee"


def safe_id(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "-", value.strip()).strip("-.")
    return cleaned or "timesheet"


@dataclass(frozen=True)
class BatchItem:
    sheet_id: str
    sheet: Timesheet | None
    issues: list[str]
    form_href: str = ""

    @property
    def matches(self) -> bool:
        return self.sheet is not None and not self.issues


def _clean_row(row: dict[str, str | None]) -> dict[str, str]:
    return {
        (key or "").strip().lower(): (value or "").strip()
        for key, value in row.items()
    }


def _unique_ids(items: list[BatchItem]) -> list[BatchItem]:
    seen: set[str] = set()
    unique: list[BatchItem] = []
    for item in items:
        base = safe_id(item.sheet_id)
        candidate = base
        number = 2
        while candidate in seen:
            candidate = f"{base}-{number}"
            number += 1
        seen.add(candidate)
        unique.append(BatchItem(candidate, item.sheet, item.issues, item.form_href))
    return unique


def _load_generated_sheets(rows: list[dict[str, str]]) -> list[BatchItem]:
    items: list[BatchItem] = []
    for index, row in enumerate(rows, start=1):
        fallback_id = row.get("sheet_id") or f"row-{index}"
        try:
            form_date = parse_user_date(row["form_date"])
            period_text = row.get("period_start", "")
            period_start = parse_user_date(period_text) if period_text else None
            employee = row.get("employee", "") or DEFAULT_EMPLOYEE
            sheet = build_timesheet(
                form_date=form_date,
                period_start=period_start,
                employee=employee,
                timekeeper=row.get("timekeeper", ""),
                kronos_id=row.get("kronos_id", ""),
                reason=row.get("reason", "") or DEFAULT_REASON,
                start_time=row.get("start", "") or DEFAULT_START,
                finish_time=row.get("finish", "") or DEFAULT_FINISH,
            )
            sheet_id = row.get("sheet_id") or (
                f"{employee_slug(sheet.employee)}_{sheet.form_date.isoformat()}"
            )
            items.append(BatchItem(sheet_id, sheet, review(sheet)))
        except KeyError as exc:
            missing = str(exc).strip("'")
            items.append(BatchItem(fallback_id, None, [f"Missing {missing}."]))
        except ValueError as exc:
            items.append(BatchItem(fallback_id, None, [str(exc)]))
    return items


def _sheet_from_day_rows(rows: list[dict[str, str]]) -> BatchItem:
    first = rows[0]
    fallback_id = first.get("sheet_id") or first.get("employee") or "timesheet"
    issues: list[str] = []
    try:
        form_date = parse_user_date(first["form_date"])
        period_text = first.get("period_start", "")
        period_start = (
            parse_user_date(period_text) if period_text else sunday_on_or_before(form_date)
        )
        if period_start.strftime("%A") != "Sunday":
            raise ValueError(
                f"period start must be a Sunday, got {period_start.isoformat()}"
            )
    except KeyError as exc:
        missing = str(exc).strip("'")
        return BatchItem(fallback_id, None, [f"Missing {missing}."])
    except ValueError as exc:
        return BatchItem(fallback_id, None, [str(exc)])

    mismatched: set[str] = set()
    for row in rows[1:]:
        for field in ("employee", "form_date", "period_start", "reason"):
            if row.get(field, "") != first.get(field, "") and field not in mismatched:
                mismatched.add(field)
                issues.append(f"{field} does not match the other rows in this time sheet.")

    grid: list[list[DayEntry | None]] = [[None] * 7 for _ in range(2)]
    for row in rows:
        day_name = row.get("day", "")
        if day_name not in DAY_NAMES:
            issues.append(f"Unknown day {day_name!r}.")
            continue
        try:
            week_index = int(row.get("week", "")) - 1
        except ValueError:
            issues.append(f"{day_name} is missing a week number.")
            continue
        if week_index not in (0, 1):
            issues.append(f"{day_name} week must be 1 or 2.")
            continue
        slot = DAY_NAMES.index(day_name)
        if grid[week_index][slot] is not None:
            issues.append(f"Week {week_index + 1} {day_name} is listed more than once.")
            continue
        start = row.get("start", "")
        finish = row.get("finish", "")
        date_text = row.get("date", "")
        if not date_text and not start and not finish:
            grid[week_index][slot] = DayEntry(day_name, None, "", "")
            continue
        try:
            work_date = parse_user_date(date_text) if date_text else None
        except ValueError as exc:
            issues.append(f"Week {week_index + 1} {day_name}: {exc}")
            continue
        grid[week_index][slot] = DayEntry(day_name, work_date, start, finish)

    weeks: list[tuple[DayEntry, ...]] = []
    for week in grid:
        weeks.append(
            tuple(
                entry or DayEntry(day_name, None, "", "")
                for entry, day_name in zip(week, DAY_NAMES)
            )
        )

    employee = first.get("employee", "")
    sheet = Timesheet(
        form_date=form_date,
        period_start=period_start,
        employee=employee.strip(),
        timekeeper=first.get("timekeeper", "").strip(),
        kronos_id=first.get("kronos_id", "").strip(),
        reason=first.get("reason", "").strip(),
        start_time=DEFAULT_START,
        finish_time=DEFAULT_FINISH,
        weeks=(weeks[0], weeks[1]),
    )
    sheet_id = first.get("sheet_id") or (
        f"{employee_slug(employee)}_{form_date.isoformat()}"
    )
    issues.extend(review(sheet))
    return BatchItem(sheet_id, sheet, issues)


def _load_day_sheets(rows: list[dict[str, str]]) -> list[BatchItem]:
    grouped: dict[str, list[dict[str, str]]] = {}
    order: list[str] = []
    for index, row in enumerate(rows, start=1):
        if row.get("sheet_id"):
            key = row["sheet_id"]
        elif row.get("employee") and row.get("form_date"):
            key = "|".join(
                (
                    row.get("employee", ""),
                    row.get("form_date", ""),
                    row.get("period_start", ""),
                )
            )
        else:
            key = f"row-{index}"
        if key not in grouped:
            order.append(key)
            grouped[key] = []
        grouped[key].append(row)
    return [_sheet_from_day_rows(grouped[key]) for key in order]


def load_timesheets(path: Path) -> list[BatchItem]:
    """Read a CSV of time sheets.

    Sheet mode has one row per sheet. Day mode, used when a day column is
    present, has one row per worked day and can catch dates that do not match
    the weekday.
    """
    with path.open(newline="", encoding="utf-8-sig") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"{path} has no header row")
        fields = {(name or "").strip().lower() for name in reader.fieldnames}
        rows = [
            _clean_row(row)
            for row in reader
            if any((value or "").strip() for value in row.values())
        ]
    if not rows:
        raise ValueError(f"{path} has no time sheets")
    if "day" in fields:
        items = _load_day_sheets(rows)
    else:
        items = _load_generated_sheets(rows)
    return _unique_ids(items)


def render_batch_text(items: list[BatchItem]) -> str:
    ready = sum(1 for item in items if item.matches)
    lines = [
        f"Reviewed {len(items)} time sheet{'s' if len(items) != 1 else ''}",
        f"Ready for supervisor signature: {ready}",
        f"Needs correction: {len(items) - ready}",
        "",
    ]
    for item in items:
        if item.sheet is None:
            lines.append(f"[needs correction] {item.sheet_id}")
        else:
            status = "ready" if item.matches else "needs correction"
            lines.append(
                f"[{status}] {item.sheet.employee}  "
                f"form {format_date(item.sheet.form_date)}  "
                f"period {format_date(item.sheet.period_start)}"
            )
        lines.extend(f"  - {issue}" for issue in item.issues)
    lines.append("")
    lines.append("The supervisor approval line is left blank on every form.")
    return "\n".join(lines) + "\n"


def render_batch_html(items: list[BatchItem]) -> str:
    ready = sum(1 for item in items if item.matches)
    corrections = len(items) - ready
    sheet_word = "time sheet" if len(items) == 1 else "time sheets"
    match_word = "matches" if ready == 1 else "match"
    ready_word = "is" if ready == 1 else "are"
    correction_word = "needs" if corrections == 1 else "need"
    body: list[str] = []
    for item in items:
        if item.matches:
            status = "<span class='ready'>Ready for supervisor signature</span>"
        else:
            status = "<span class='fix'>Needs correction</span>"
        if item.sheet is None:
            employee = "Not loaded"
            form_date = ""
            period = ""
        else:
            employee = html.escape(item.sheet.employee)
            form_date = html.escape(format_date(item.sheet.form_date))
            period_end = item.sheet.period_start + timedelta(days=13)
            period = html.escape(
                f"{format_date(item.sheet.period_start)} to {format_date(period_end)}"
            )
        if item.form_href:
            href = quote(item.form_href, safe="/")
        elif item.sheet is not None:
            href = f"timesheet_{quote(item.sheet_id)}.pdf"
        else:
            href = ""
        link = f"<a href='{html.escape(href)}'>Open form</a>" if href else ""
        notes = "<br>".join(html.escape(issue) for issue in item.issues) or "Matches the weekday pattern."
        body.append(
            "<tr>"
            f"<td>{employee}</td>"
            f"<td>{form_date}</td>"
            f"<td>{period}</td>"
            f"<td>{status}</td>"
            f"<td>{notes}</td>"
            f"<td>{link}</td>"
            "</tr>"
        )
    rows = "\n".join(body)
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <title>Time sheet review</title>
  <style>
    @page {{ size: letter landscape; margin: 0.5in; }}
    * {{
      -webkit-print-color-adjust: exact;
      print-color-adjust: exact;
    }}
    html {{ color-scheme: light; background: #fff; }}
    body {{
      margin: 32px auto;
      max-width: 1100px;
      background: #fff;
      color: #1a1a1a;
      font-family: "Segoe UI", Calibri, sans-serif;
    }}
    h1 {{ margin-bottom: 0; font-size: 22px; }}
    p {{ color: #333; }}
    table {{ width: 100%; border-collapse: collapse; margin-top: 16px; }}
    th, td {{ border-bottom: 1px solid #d5dbe3; padding: 10px 8px; text-align: left; vertical-align: top; }}
    th {{ background: #eef3fb; }}
    .ready {{ color: #146c2e; font-weight: 700; }}
    .fix {{ color: #9b1c1c; font-weight: 700; }}
    a {{ color: #1a4f8b; }}
  </style>
</head>
<body>
  <h1>Time sheet review</h1>
  <p>Reviewed {len(items)} {sheet_word}. {ready} {match_word} the weekday pattern and {ready_word} ready for a supervisor signature. {corrections} {correction_word} correction.</p>
  <table>
    <thead>
      <tr>
        <th>Employee</th>
        <th>Form date</th>
        <th>Period</th>
        <th>Result</th>
        <th>Review</th>
        <th>Form</th>
      </tr>
    </thead>
    <tbody>
      {rows}
    </tbody>
  </table>
</body>
</html>
"""


def find_browser() -> Path:
    """Return Edge or Chrome, which can print the form to PDF."""
    program_files = os.environ.get("PROGRAMFILES", r"C:\Program Files")
    program_files_x86 = os.environ.get("PROGRAMFILES(X86)", r"C:\Program Files (x86)")
    candidates = [
        Path(program_files_x86) / "Microsoft/Edge/Application/msedge.exe",
        Path(program_files) / "Microsoft/Edge/Application/msedge.exe",
        Path(program_files) / "Google/Chrome/Application/chrome.exe",
        Path(program_files_x86) / "Google/Chrome/Application/chrome.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    raise RuntimeError("Microsoft Edge or Chrome is required to create the PDFs.")


def write_pdf(html_path: Path) -> Path:
    """Print an HTML file to a PDF next to it."""
    pdf_path = html_path.with_suffix(".pdf")
    if pdf_path.exists():
        pdf_path.unlink()
    browser = find_browser()
    with tempfile.TemporaryDirectory(prefix="timesheet-pdf-") as profile:
        command = [
            str(browser),
            "--headless",
            "--disable-gpu",
            "--no-first-run",
            "--no-pdf-header-footer",
            f"--user-data-dir={profile}",
            f"--print-to-pdf={pdf_path.resolve()}",
            html_path.resolve().as_uri(),
        ]
        subprocess.run(command, check=False, capture_output=True, text=True, timeout=90)
    deadline = time.time() + 15
    while time.time() < deadline:
        if pdf_path.is_file() and pdf_path.stat().st_size > 1000:
            return pdf_path
        time.sleep(0.25)
    raise RuntimeError(f"Could not create {pdf_path.name}")


DATE_WORD = re.compile(r"\d{1,2}/\d{1,2}/\d{2,4}")
TIME_WORD = re.compile(r"\d{1,2}[.:]\d{2}\s*[AaPp][Mm]")


def normalize_time(value: str) -> str:
    match = re.fullmatch(r"(\d{1,2})[.:](\d{2})\s*([AaPp][Mm])", value.strip())
    if not match:
        return value.strip()
    hour, minute, ampm = match.groups()
    return f"{int(hour)}.{minute}{ampm.upper()}"


def _row_words(
    words: list[tuple[float, float, str]],
    y: float,
    x_min: float,
    x_max: float,
) -> list[tuple[float, float, str]]:
    return [
        word
        for word in words
        if x_min < word[0] < x_max and abs(word[1] - y) <= 5
    ]


def infer_period_start(
    weeks: tuple[tuple[DayEntry, ...], tuple[DayEntry, ...]],
) -> date | None:
    dated: list[tuple[int, int, DayEntry]] = []
    for week_index, week in enumerate(weeks):
        for offset, entry in enumerate(week):
            if entry.work_date is not None:
                dated.append((week_index, offset, entry))
    for week_index, offset, entry in dated:
        if entry.work_date is not None and entry.work_date.strftime("%A") == entry.day_name:
            return entry.work_date - timedelta(days=offset + 7 * week_index)
    if not dated:
        return None
    week_index, offset, entry = dated[0]
    if entry.work_date is None:
        return None
    return entry.work_date - timedelta(days=offset + 7 * week_index)


def timesheet_from_words(
    words: list[tuple[float, float, str]],
    page_text: str,
    sheet_id: str,
) -> BatchItem:
    """Build a time sheet from the words on a filled PDF page."""
    issues: list[str] = []
    form_date: date | None = None
    employee = ""
    for x_pos, y_pos, text in words:
        if text == "DATE:" and form_date is None:
            for _other_x, _other_y, other in _row_words(words, y_pos, x_pos, x_pos + 220):
                if DATE_WORD.fullmatch(other):
                    form_date = parse_user_date(other)
                    break
        if text == "FROM:":
            employee = " ".join(
                other for _other_x, _other_y, other in _row_words(words, y_pos, x_pos, x_pos + 420)
            ).strip()

    reason_match = re.search(
        r"Reason for not using my Kronos identification card:\s*(.+?)\s*Employee's Signature",
        page_text,
        re.S,
    )
    reason = re.sub(r"\s+", " ", reason_match.group(1)).strip() if reason_match else ""

    day_xs = [x_pos for x_pos, _y_pos, text in words if text in DAY_NAMES]
    if not day_xs:
        return BatchItem(sheet_id, None, ["This PDF has no readable time sheet table."])
    split_at = (
        (min(day_xs) + max(day_xs)) / 2
        if max(day_xs) - min(day_xs) > 80
        else max(day_xs) + 500
    )

    grid: list[list[DayEntry | None]] = [[None] * 7 for _ in range(2)]
    for x_pos, y_pos, text in words:
        if text not in DAY_NAMES:
            continue
        week_index = 0 if x_pos < split_at else 1
        slot = DAY_NAMES.index(text)
        if grid[week_index][slot] is not None:
            continue
        work_date: date | None = None
        times: list[str] = []
        companions = sorted(
            _row_words(words, y_pos, x_pos + 5, x_pos + 230),
            key=lambda item: item[0],
        )
        for _other_x, _other_y, other in companions:
            if other in DAY_NAMES:
                continue
            if DATE_WORD.fullmatch(other) and work_date is None:
                try:
                    work_date = parse_user_date(other)
                except ValueError as exc:
                    issues.append(f"Week {week_index + 1} {text}: {exc}")
            elif TIME_WORD.fullmatch(other):
                times.append(normalize_time(other))
        start = times[0] if times else ""
        finish = times[1] if len(times) > 1 else ""
        grid[week_index][slot] = DayEntry(text, work_date, start, finish)

    if form_date is None:
        return BatchItem(sheet_id, None, ["The PDF has no form date."])

    weeks = tuple(
        tuple(entry or DayEntry(day_name, None, "", "") for entry, day_name in zip(week, DAY_NAMES))
        for week in grid
    )
    period_start = infer_period_start(weeks) or sunday_on_or_before(form_date)
    if period_start.strftime("%A") != "Sunday":
        issues.append(f"Period start {format_date(period_start)} is not a Sunday.")

    sheet = Timesheet(
        form_date=form_date,
        period_start=period_start,
        employee=employee,
        timekeeper="",
        kronos_id="",
        reason=reason,
        start_time=DEFAULT_START,
        finish_time=DEFAULT_FINISH,
        weeks=weeks,
    )
    issues.extend(review(sheet))
    return BatchItem(sheet_id, sheet, issues)


def read_timesheet_pdf(path: Path) -> BatchItem:
    """Read one employee PDF. The file is not changed."""
    try:
        import pymupdf
    except ImportError as exc:
        raise RuntimeError(
            "Install PyMuPDF to read time sheet PDFs: python -m pip install pymupdf"
        ) from exc

    original = path.read_bytes()
    document = pymupdf.open(path)
    try:
        words: list[tuple[float, float, str]] = []
        page_text: list[str] = []
        for page in document:
            page_text.append(page.get_text())
            for word in page.get_text("words"):
                words.append((float(word[0]), float(word[1]), str(word[4])))
        item = timesheet_from_words(words, "\n".join(page_text), safe_id(path.stem))
    finally:
        document.close()
    if path.read_bytes() != original:
        raise RuntimeError(f"{path.name} was modified while it was read.")
    return item


def process_inbox(inbox: Path, output_dir: Path) -> list[BatchItem]:
    """Review every PDF in a shared folder and sort the unchanged files."""
    if not inbox.is_dir():
        raise ValueError(f"Shared folder not found: {inbox}")
    pdfs = sorted(path for path in inbox.glob("*.pdf") if path.is_file())
    if not pdfs:
        raise ValueError(f"No PDF time sheets in {inbox}")

    ready_dir = output_dir / "ready"
    correction_dir = output_dir / "needs-correction"
    processed_dir = inbox / "processed"
    for folder in (ready_dir, correction_dir, processed_dir):
        folder.mkdir(parents=True, exist_ok=True)

    items: list[BatchItem] = []
    for pdf in pdfs:
        item = read_timesheet_pdf(pdf)
        folder = ready_dir if item.matches else correction_dir
        shutil.copy2(pdf, folder / pdf.name)
        stored = BatchItem(
            item.sheet_id,
            item.sheet,
            item.issues,
            f"{folder.name}/{pdf.name}",
        )
        items.append(stored)
        destination = processed_dir / pdf.name
        if destination.exists():
            destination.unlink()
        shutil.move(str(pdf), str(destination))
    return items


def write_batch(items: list[BatchItem], output_dir: Path, rewrite_forms: bool = True) -> Path:
    output_dir.mkdir(parents=True, exist_ok=True)
    if rewrite_forms:
        for item in items:
            if item.sheet is None:
                continue
            stem = output_dir / f"timesheet_{item.sheet_id}"
            stem.with_suffix(".txt").write_text(
                render_text(item.sheet, item.issues), encoding="utf-8"
            )
            html_path = stem.with_suffix(".html")
            html_path.write_text(render_html(item.sheet), encoding="utf-8")
            write_pdf(html_path)
    summary = output_dir / "review.html"
    summary.write_text(render_batch_html(items), encoding="utf-8")
    (output_dir / "review.txt").write_text(render_batch_text(items), encoding="utf-8")
    return write_pdf(summary)


def write_outputs(sheet: Timesheet, output_dir: Path) -> tuple[Path, Path, Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    stem = (
        f"timesheet_{employee_slug(sheet.employee)}_{sheet.form_date.isoformat()}"
    )
    text_path = output_dir / f"{stem}.txt"
    html_path = output_dir / f"{stem}.html"
    issues = review(sheet)
    text_path.write_text(render_text(sheet, issues), encoding="utf-8")
    html_path.write_text(render_html(sheet), encoding="utf-8")
    pdf_path = write_pdf(html_path)
    return text_path, html_path, pdf_path


def self_check() -> None:
    """Check the 10/23/2026 sample and a completed bi-weekly period."""
    sample = build_timesheet(
        form_date=date(2026, 10, 23),
        employee=DEFAULT_EMPLOYEE,
    )
    week1 = {row.day_name: row for row in sample.weeks[0]}
    assert week1["Monday"].work_date == date(2026, 10, 19)
    assert week1["Thursday"].work_date == date(2026, 10, 22)
    assert week1["Friday"].work_date == date(2026, 10, 23)
    assert week1["Friday"].start == "9.00AM"
    assert week1["Friday"].finish == "5.00PM"
    assert week1["Sunday"].work_date is None
    assert week1["Saturday"].work_date is None
    assert all(row.work_date is None for row in sample.weeks[1])
    assert review(sample) == []

    html_doc = render_html(sample)
    approval_row = html_doc.split("Supervisor's Approval", 1)[1].split("</tr>", 1)[0]
    assert DEFAULT_EMPLOYEE not in approval_row
    assert "Digitally signed" not in html_doc

    full = build_timesheet(
        form_date=date(2026, 10, 31),
        period_start=date(2026, 10, 18),
    )
    worked = [
        row.work_date
        for week in full.weeks
        for row in week
        if row.worked
    ]
    assert worked[0] == date(2026, 10, 19)
    assert worked[-1] == date(2026, 10, 30)
    assert len(worked) == 10
    assert review(full) == []

    try:
        build_timesheet(form_date=date(2026, 10, 23), period_start=date(2026, 10, 19))
    except ValueError:
        pass
    else:
        raise AssertionError("a Monday period start should be rejected")

    def day_row(sheet_id: str, form_date: str, period_start: str, week: int, day_name: str, work_date: str) -> dict[str, str]:
        return {
            "sheet_id": sheet_id,
            "employee": DEFAULT_EMPLOYEE,
            "timekeeper": "",
            "kronos_id": "",
            "form_date": form_date,
            "period_start": period_start,
            "reason": DEFAULT_REASON,
            "week": str(week),
            "day": day_name,
            "date": work_date,
            "start": DEFAULT_START,
            "finish": DEFAULT_FINISH,
        }

    good_rows = [
        day_row("good", "2026-10-23", "2026-10-18", 1, "Monday", "2026-10-19"),
        day_row("good", "2026-10-23", "2026-10-18", 1, "Tuesday", "2026-10-20"),
        day_row("good", "2026-10-23", "2026-10-18", 1, "Wednesday", "2026-10-21"),
        day_row("good", "2026-10-23", "2026-10-18", 1, "Thursday", "2026-10-22"),
        day_row("good", "2026-10-23", "2026-10-18", 1, "Friday", "2026-10-23"),
    ]
    bad_rows = [
        day_row("bad", "2026-10-23", "2026-10-18", 1, "Monday", "2026-10-19"),
        day_row("bad", "2026-10-23", "2026-10-18", 1, "Tuesday", "2026-10-20"),
        day_row("bad", "2026-10-23", "2026-10-18", 1, "Wednesday", "2026-10-21"),
        day_row("bad", "2026-10-23", "2026-10-18", 1, "Thursday", "2026-10-22"),
        day_row("bad", "2026-10-23", "2026-10-18", 1, "Friday", "2026-10-22"),
    ]
    items = _unique_ids([
        _sheet_from_day_rows(good_rows),
        _sheet_from_day_rows(bad_rows),
    ])
    assert items[0].matches
    assert not items[1].matches
    assert any("Friday" in issue and "10/22/2026" in issue for issue in items[1].issues)
    report = render_batch_html(items)
    assert "Ready for supervisor signature" in report
    assert "Needs correction" in report
    assert "Digitally signed" not in render_html(items[0].sheet)

    sample_path = Path(__file__).resolve().parent / "sample_timesheets.csv"
    loaded = load_timesheets(sample_path)
    assert [item.matches for item in loaded] == [True, True, False]

    output_dir = Path(__file__).resolve().parent / "output"
    current_pdf = read_timesheet_pdf(output_dir / "timesheet_current-2026-10-23.pdf")
    prior_pdf = read_timesheet_pdf(output_dir / "timesheet_prior-2026-10-10.pdf")
    wrong_pdf = read_timesheet_pdf(output_dir / "timesheet_wrong-friday.pdf")
    assert current_pdf.matches
    assert prior_pdf.matches
    assert prior_pdf.sheet is not None
    assert sum(1 for week in prior_pdf.sheet.weeks for row in week if row.worked) == 10
    assert not wrong_pdf.matches
    assert any("Friday" in issue and "10/22/2026" in issue for issue in wrong_pdf.issues)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Fill and review special bi-weekly alternate time sheets. "
            "Weekdays through the form date are 9.00AM-5.00PM. "
            "The supervisor line is left blank."
        )
    )
    parser.add_argument(
        "--batch",
        type=Path,
        help="CSV of time sheets to review. See sample_timesheets.csv.",
    )
    parser.add_argument(
        "--inbox",
        type=Path,
        help="Shared folder of employee PDF time sheets to read and review.",
    )
    parser.add_argument(
        "--form-date",
        type=parse_date,
        default=date.today(),
        help="Sheet date, YYYY-MM-DD. Defaults to today. Days after this stay blank.",
    )
    parser.add_argument(
        "--period-start",
        type=parse_date,
        default=None,
        help=(
            "Sunday that starts week 1, YYYY-MM-DD. "
            "Defaults to the Sunday of the form date's week."
        ),
    )
    parser.add_argument("--employee", default=DEFAULT_EMPLOYEE)
    parser.add_argument("--timekeeper", default="")
    parser.add_argument("--kronos-id", default="")
    parser.add_argument("--reason", default=DEFAULT_REASON)
    parser.add_argument("--start", default=DEFAULT_START, help="Weekday start, default 9.00AM.")
    parser.add_argument("--finish", default=DEFAULT_FINISH, help="Weekday finish, default 5.00PM.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parent / "output",
    )
    parser.add_argument(
        "--self-check",
        action="store_true",
        help="Run pattern checks for the sample week, then exit.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.self_check:
        self_check()
        print("Self-check passed.")
        return

    if args.inbox:
        items = process_inbox(args.inbox, args.output_dir)
        summary = write_batch(items, args.output_dir, rewrite_forms=False)
        print(render_batch_text(items))
        print(f"Ready for supervisor signature: {args.output_dir / 'ready'}")
        print(f"Needs correction: {args.output_dir / 'needs-correction'}")
        print(f"Review PDF: {summary}")
        return

    if args.batch:
        items = load_timesheets(args.batch)
        summary = write_batch(items, args.output_dir)
        print(render_batch_text(items))
        print(f"Review PDF: {summary}")
        return

    sheet = build_timesheet(
        form_date=args.form_date,
        period_start=args.period_start,
        employee=args.employee,
        timekeeper=args.timekeeper,
        kronos_id=args.kronos_id,
        reason=args.reason,
        start_time=args.start,
        finish_time=args.finish,
    )
    text_path, html_path, pdf_path = write_outputs(sheet, args.output_dir)
    print(render_text(sheet, review(sheet)))
    print(f"Text: {text_path}")
    print(f"Form: {html_path}")
    print(f"PDF: {pdf_path}")


if __name__ == "__main__":
    main()
