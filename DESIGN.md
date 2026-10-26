# Timesheet design

Local tool for the NYCHA Special Bi-Weekly Alternate Time Sheet, form NYCHA 015.210A (Rev. 5/07). An employee fills the two-week grid, draws a signature, and submits. A supervisor reviews the weekday pattern, draws their own signature, and approves. A separate command-line path can generate the same form or review PDFs dropped in a shared folder.

The site runs on the computer that starts it. It is a demo of the review and sign-off flow, and it does not connect to Kronos or payroll.

## How the pieces fit

```text
Employee browser                         Supervisor browser
      |                                        |
      |  sign in, fill, draw, submit           |  review, draw, approve or return
      v                                        v
              timesheet_app.py  (127.0.0.1:8766)
              sessions in memory, pages in HTML
                         |
                         |  build_timesheet(), review()
                         v
                 generate_timesheet.py
                 pattern rules and form layout
                    |                |
                    |                +-- Edge or Chrome prints HTML to PDF
                    |
                    +-- data/timesheets.json
                        users, sheets, drawn signatures

Shared folder of PDFs
        |
        |  python generate_timesheet.py --inbox
        v
   read words with PyMuPDF, review, copy the file unchanged
        |
        +-- output/ready
        +-- output/needs-correction
        +-- output/review.pdf
        +-- inbox/processed
```

`timesheet_app.py` is the sign-in site. `generate_timesheet.py` owns the form, the weekday pattern, CSV batch review, PDF printing, and the shared-folder reader. The site imports that module. The folder reader never writes a signature onto an employee PDF.

## Roles

| Role | Sign-in | What they can do |
|---|---|---|
| Employee | `divya` / `timesheet` | Create a sheet, refill the standard weekday pattern, draw their signature, and submit. After a return, edit and submit again. They see only their own sheets. |
| Supervisor | `supervisor` / `approve` | See every sheet. On a submitted sheet, type the name for the approval line, draw a signature, and approve, or send the sheet back with a note. |

Both accounts are created the first time the site starts, when `data/timesheets.json` does not exist yet. The display name on the employee account is Divya Radhakrishnan. The supervisor account’s display name is Supervisor, and the supervisor types the name that is stored on the approval line.

An employee request to approve returns “Not allowed.” A supervisor who opens the employee home is sent to the queue.

## Sheet lifecycle

A new sheet exists only in the browser until the employee submits it. Refill recalculates the grid and stays on the form. Submit stores the sheet.

```text
(unsaved form) --submit--> submitted --approve--> approved
                               |
                               +--return--> returned --submit again--> submitted
```

| Status | Meaning |
|---|---|
| submitted | Waiting for a supervisor. The employee signature is stored. |
| approved | Supervisor name, date, and drawn signature are stored. The sheet is read-only. |
| returned | Supervisor note is stored. Supervisor name, date, and signature are cleared. The employee can edit and submit again. |

Approve and return apply only while the status is submitted. Pattern warnings are shown on the sheet and do not block approval. The supervisor decides.

## What a valid weekday pattern is

`review()` compares a sheet with this pattern. An empty issue list means it matches.

- Week 1 starts on the period start, which must be a Sunday. Week 2 is the next seven days. If no period start is given, week 1 starts on the Sunday of the form date’s week.
- Each weekday on or before the form date has hours `9.00AM`–`5.00PM`. The time uses a period, not a colon, and has no space before AM or PM.
- Saturday, Sunday, and any day after the form date stay blank.
- The reason is exactly `Approved remote work`.
- The employee name is present.
- Each filled date falls on that row’s weekday, and no date is used twice.

The sample photo dated Friday as 10/22/2026, the same date as Thursday. The calendar Friday of that week is 10/23/2026. Review reports both the wrong weekday and the repeated date. `sample_timesheets.csv` includes that sheet as the failing example.

Refill uses the same rule: weekdays through the form date are filled, and weekends and future days are left blank. The supervisor line on a generated PDF stays blank.

## Web application

Python’s built-in `ThreadingHTTPServer` serves HTML on `127.0.0.1:8766`. There is no separate framework and no database. Pages are assembled as HTML strings. Responses send `Cache-Control: no-store`.

| Method | Path | Who | Result |
|---|---|---|---|
| GET | `/` | anyone | Sign-in page, or redirect to home or queue |
| POST | `/login` | anyone | Start a session, or show an error |
| POST | `/logout` | signed in | End the session |
| GET | `/home` | employee | That employee’s sheets, newest first |
| GET | `/queue` | supervisor | All sheets: submitted, then returned, then approved |
| GET | `/sheet/new` | employee | Blank form prefilled for today |
| POST | `/sheet` | employee | Submit a new sheet, or refill without saving |
| GET | `/sheet/{id}` | owner or supervisor | Read-only form, pattern result, and actions |
| GET | `/sheet/{id}/edit` | owning employee | Editor, only when the sheet was returned |
| POST | `/sheet/{id}` | owning employee | Resubmit a returned sheet, or refill |
| POST | `/sheet/{id}/approve` | supervisor | Record approval |
| POST | `/sheet/{id}/return` | supervisor | Send the sheet back |

### Submit checks

Submit requires a form date, a Sunday period start, the certification checkbox, and a drawn employee signature. Refill requires the two dates and a Sunday period start, and it does not require the checkbox or a signature. Date fields accept `YYYY-MM-DD`, `MM/DD/YYYY`, and `MM/DD/YY`.

### Signatures

Each person draws on a canvas in the browser. The pad posts a PNG data URL (`data:image/png;base64,...`). The server keeps the value only when it has that prefix, the payload is base64, and the whole value is at most 400,000 characters. A blank pad is rejected in the browser and again on the server.

The employee pad is on the submit form. The supervisor pad is on the approve form, together with the name field. The signed-in user draws their own signature. The site does not generate a “digitally signed by” block.

### Sessions

A successful sign-in stores a random session token in memory and sets an `HttpOnly`, `SameSite=Lax` cookie. Each session has a CSRF token. POST requests from a signed-in user must send that token. Sessions end at logout and whenever the process stops. Restarting the program keeps the JSON file and drops every session.

Passwords are stored as PBKDF2-HMAC-SHA256 hashes with 100,000 iterations (`salt$digest`). The demo passwords are in the source and on the sign-in page so the team can run the site.

## Stored record

`data/timesheets.json` holds `next_id`, `users`, and `sheets`. Writes take a lock, read the file, change it, and write it back.

A sheet stores:

- `id`, `employee_id`, `employee_name`, `status`
- `timekeeper`, `kronos_id`, `form_date`, `period_start`, `reason`, `witness`
- `weeks`: two weeks, Sunday through Saturday, each day with `day`, `date`, `start`, and `finish`
- `employee_signature`
- `supervisor_name`, `supervisor_date`, `supervisor_signature`, `supervisor_id` after approval
- `return_note`

Timekeeper and Kronos ID stay blank unless the employee types them. This file is gitignored because it contains password hashes and signature images.

## Shared-folder review

```text
python generate_timesheet.py --inbox .\inbox
```

PyMuPDF reads each PDF in the folder. The reader locates `FROM:` and `DATE:`, the reason line, and the day rows by word position. If the two week tables sit far apart on the page, it splits them by the day-name x positions. Times such as `9:00 AM` are normalized to `9.00AM` before review.

Each original PDF is copied, unchanged, to `output/ready` or `output/needs-correction`, then moved to `inbox/processed`. The review summary is `output/review.html`, `output/review.txt`, and `output/review.pdf`. This path does not add a supervisor signature. A person signs the copy in `output/ready`.

Other commands:

| Command | Result |
|---|---|
| `python generate_timesheet.py` | One form for today, as text, HTML, and PDF |
| `--form-date` and `--period-start` | Choose the form date and the Sunday that opens week 1 |
| `--batch sample_timesheets.csv` | Review every sheet in the CSV and write the forms plus a summary |
| `--self-check` | Check the sample week, a full period, a rejected Monday start, the bad Friday, the sample CSV, and the three sample PDFs |

PDF printing uses headless Microsoft Edge, or Chrome if Edge is not installed, with `--print-to-pdf`. Forms are letter portrait. The review summary is letter landscape.

A CSV with a `day` column is read as one row per day. A CSV without that column is one row per sheet, and the generator fills the standard pattern.

## Boundaries

- The site listens on localhost. Teammates run it on their own machines. The GitHub repository is the source, not a hosted site.
- Approval is stored only after the signed-in supervisor draws a signature and submits Approve.
- The shared-folder path sorts PDFs and leaves their bytes unchanged.
- Pattern mismatches are visible and do not auto-reject a sheet the supervisor chooses to approve.
- There is one seeded employee and one seeded supervisor. Adding people means extending the user list in a fresh `data/timesheets.json`.
- Signature images are stored in the JSON file. They are drawings, not certificates.
