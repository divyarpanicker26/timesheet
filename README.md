# Timesheet

Employees sign in, fill the bi-weekly alternate time sheet, draw a signature, and submit it. Supervisors sign in, review the weekday pattern, draw their signature, and approve.

## Run

```powershell
python -m pip install pymupdf
python timesheet_app.py
```

Open http://127.0.0.1:8766/

| Role | Username | Password |
|---|---|---|
| Employee | `divya` | `timesheet` |
| Supervisor | `supervisor` | `approve` |

Weekdays are filled as 9.00AM–5.00PM. Weekends stay blank. The reason defaults to Approved remote work.

`generate_timesheet.py` can also build or review PDF and CSV time sheets from the command line.
