# Elks Attendance — Manager Security & Reporting

**Technical / Developer Reference**

| | |
|---|---|
| **Technical name** | `elksattendance` |
| **Version** | `19.0.3.0` |
| **Odoo series** | 19.0 |
| **Category** | Human Resources |
| **License** | LGPL-3 |
| **Author** | Danny Santiago |
| **Depends on** | `hr_attendance`, `mail`, `portal`, `website`, `elksfrs`, `elkscharity` |
| **Application** | No (it extends HR Attendance) |

---

## 1. What this module does

`elksattendance` layers four lodge-specific capabilities on top of Odoo's
stock **HR Attendance** app:

1. **Team-scoped security** — two new groups let a manager see and edit the
   attendance and employee records of *their own org-tree subtree only*,
   filling the gap between Odoo's all-or-nothing attendance roles.
2. **Worked-hours policy override** — `worked_hours` is the raw
   clock-in → clock-out span with **no** break/lunch deduction.
3. **Tip / gratuity tracking** — a per-employee "Receives Tips" flag, a
   per-shift tip amount, and a kiosk screen that prompts tipped employees
   for a gratuity on clock-out.
4. **Payroll timecard reporting** — a semi-monthly timecard wizard (PDF +
   QuickBooks CSV).
5. **Per-employee email automation** — (a) a delayed timecard email to
   the employee a few minutes after clock-out (so kiosk-entered tips are
   included), (b) a scheduled per-employee timecard email
   as a payroll-processing approval reminder, and (c) an hourly
   "possible missed clock-out" alert to the employee and their Attendance
   approver. All send to each employee's own `work_email` through a
   configurable outgoing mail server.
6. **Portal timecard approval** — a persistent `elks.timecard` record per
   employee per pay period with a two-step sign-off
   (`draft → employee_approved → approved`), signatures stamped with
   name + timestamp, an employee self-service portal at `/my/timecards`,
   an **Invite to Portal** action on the employee, and auto-reset of
   signatures whenever the period's time changes after approval.
7. **Adjustment requests** — `elks.timecard.adjustment`: from a per-line
   portal view the employee suggests corrected check-in/out + reason; the
   request is logged to the timecard chatter with the hour delta, and the
   **approver applies it** (writing the punch, which reopens the period).
   Plus: an approver email when an employee signs off, configurable
   **deadline reminders** before a period closes, a portal chatter on each
   timecard, an **approver portal** (`/my/timecard-approvals`) grouped by
   department/area with inline approve + apply/reject, and the digital
   signatures printed on the payroll PDF.

---

## 2. File / directory map

```
elksattendance/
├── __manifest__.py
├── controllers/
│   └── kiosk.py                       # Kiosk controller override (tips)
├── data/
│   └── timecard_cron.xml              # Daily ir.cron for scheduled email
├── migrations/
│   ├── 19.0.1.1/pre-migrate.py        # Force-recreate stale record rules
│   └── 19.0.1.4/post-migrate.py       # Recompute worked_hours (no breaks)
├── models/
│   ├── hr_attendance.py               # worked_hours override + tip fields
│   ├── hr_employee.py                 # manager-chain cache + tips flag
│   ├── res_config_settings.py         # Timecard-email settings
│   └── timecard_cron.py               # Scheduled-email cron logic
├── report/
│   └── timecard_report.xml            # QWeb timecard (HTML + PDF actions)
├── security/
│   ├── elksattendance_security.xml    # Groups + record rules
│   └── ir.model.access.csv            # Model ACLs
├── static/src/
│   ├── components/kiosk_tip_entry/    # OWL tip-entry component (.js/.xml)
│   └── public_kiosk/                  # Patch of the public kiosk app
├── views/
│   ├── elksattendance_menus.xml
│   ├── hr_attendance_views.xml
│   ├── hr_employee_views.xml
│   └── res_config_settings_views.xml
└── wizard/
    ├── timecard_report_wizard.py
    └── timecard_report_wizard_views.xml
```

---

## 3. Data model

### 3.1 `hr.employee` (extended) — `models/hr_employee.py`

| Field | Type | Stored | Notes |
|---|---|---|---|
| `x_receives_tips` | Boolean | yes | Manager-set flag. `tracking=True`. Drives the kiosk tip prompt and the related flag on attendance. |
| `x_manager_user_ids` | Many2many → `res.users` | yes (computed, `recursive=True`) | Cache of **every** `res.users` above this employee in the org tree. Relation table `hr_employee_manager_user_rel`. |

**Manager-chain compute**

```python
@api.depends('parent_id', 'parent_id.user_id', 'parent_id.x_manager_user_ids')
def _compute_manager_user_ids(self):
    for emp in self:
        user_ids = set()
        manager = emp.parent_id
        if manager and manager.user_id:
            user_ids.add(manager.user_id.id)
            user_ids |= set(manager.x_manager_user_ids.ids)
        emp.x_manager_user_ids = [(6, 0, list(user_ids))]
```

Because the `@api.depends` includes `parent_id.x_manager_user_ids` and the
field is declared `recursive=True`, changing a manager **high** in the tree
cascades a recompute down to every descendant automatically. This stored
cache is what the attendance/employee record rules filter on, which keeps
the rule domains cheap (a single `in` test instead of a recursive walk at
read time).

### 3.2 `hr.attendance` (extended) — `models/hr_attendance.py`

| Field | Type | Stored | Notes |
|---|---|---|---|
| `x_is_tipped_shift` | Boolean | yes | `related='employee_id.x_receives_tips'`, stored so it can be filtered/grouped/reported. |
| `x_tip_amount` | Float `(10,2)` | yes | Gratuity for the shift. Default `0.0`. |
| `x_post_shift_email_pending` | Boolean | yes | Queue flag; set when a shift closes. A cron sends the email once the delay elapses, then clears this. `copy=False`. |
| `x_post_shift_email_sent` | Boolean | yes | De-dup flag; set once the post-clock-out email has been sent so editing a closed record doesn't re-send. `copy=False`. |
| `x_long_shift_alerted` | Boolean | yes | De-dup flag; set once a missed-clock-out alert has fired for this open shift. `copy=False`. |

**Create / write hooks** — `create()` and `write()` are overridden to
**queue** (not send) the post-shift email the moment a shift is *closed*
(check-out goes falsy → set): they only flip `x_post_shift_email_pending`,
so the kiosk clock-out stays instant and is never exposed to a mail/PDF
failure. `write()` snapshots which records are transitioning so a later
edit to an already-closed record doesn't re-queue. The actual send happens
in `_cron_send_post_shift_emails` after a configurable delay (default 5 min),
which is what lets a tip keyed in seconds after clock-out land on the card.

> **External dependency:** the timecard wizard and cron filter on
> `x_charity_task_id` (`('x_charity_task_id', '=', False)`). That field is
> **not** defined here — it comes from **`elkscharity`**, which is why that
> module is a hard dependency. Charity-tagged hours are excluded from
> payroll timecards (they belong on the charity GL report instead).

**Worked-hours override (lodge policy)**

```python
@api.depends("check_in", "check_out")
def _compute_worked_hours(self):
    for attendance in self:
        if attendance.check_in and attendance.check_out:
            delta = attendance.check_out - attendance.check_in
            attendance.worked_hours = delta.total_seconds() / 3600.0
        else:
            attendance.worked_hours = 0.0
```

Odoo core subtracts unpaid breaks declared in the employee's resource
calendar (e.g. a 12:00–13:00 lunch). The lodge does **not** want that:
shifts are paid for the full clock span, and a real break means clocking
out and back in (two records). This override returns the raw delta and
**narrows the `@api.depends`** to just `check_in`/`check_out`.

### 3.3 `elks.timecard.report.wizard` (TransientModel) — `wizard/timecard_report_wizard.py`

The reporting wizard. Not a stored business object — a transient that
builds the timecard report on demand.

Key fields: `employee_ids` (domain excludes the *Volunteers* department),
`pay_period` (`first_half` / `second_half` / `custom`), `period_month`,
`period_year`, `date_from`, `date_to`, `period_display` (computed),
`no_hours_warning` (text), `csv_file` / `csv_filename` (binary download).

Notable methods:

- `_default_pay_period / _default_date_from / _default_date_to` — default
  to the current half-month based on `today.day <= 15`.
- `_onchange_period` + `action_previous_period` / `action_next_period` —
  period math and the ◀/▶ navigation; `_reopen()` returns an
  `act_window` that re-opens the same transient (`target: new`) so the
  wizard "stays open" as you page through periods.
- `_get_attendance_data()` — the shared query. Domain:
  ```python
  [('check_in', '>=', <date_from 00:00>),
   ('check_in', '<=', <date_to 23:59:59>),
   ('employee_id.department_id.name', '!=', 'Volunteers'),
   ('x_charity_task_id', '=', False)]
  ```
  Returns `{employee: attendance_recordset}`. Raises `UserError` if the
  period is empty; sets `no_hours_warning` when *some* selected employees
  have no hours.
- `action_print_pdf` / `action_download_pdf` — render the QWeb report
  (HTML preview vs. PDF download).
- `action_export_csv` — writes a QuickBooks-style CSV onto `csv_file`
  (columns: Employee, Date, Check In, Check Out, Hours Worked, Tips,
  Total Period Hours, Total Period Tips) and reopens the wizard so the
  binary field can be downloaded.
- `_format_time / _format_date / _format_hours` — user-tz formatting
  helpers (`HH:MM` for hours, `MM/DD/YYYY`, `hh:MM AM/PM`).

### 3.4 `elksattendance.timecard.cron` (AbstractModel) — `models/timecard_cron.py`

Holds all email automation. An **AbstractModel** so it has no DB table —
it's a home for the methods invoked by `data/timecard_cron.xml` and by
`hr.attendance`. **Everything sends per-employee** to `work_email`.

Period math:

- `_get_send_period(today, frequency)` — the **most recently completed**
  period, or `None` if today isn't a send day (`weekly` → Mondays, prev
  Mon–Sun; `semi_monthly` → 1st covers 16th–EOM prior, 16th covers 1st–15th;
  `monthly` → 1st covers prior month).
- `_get_current_period(ref_date, frequency)` — the **in-progress** period
  containing `ref_date`; used by the post-shift email.

Building blocks:

- `_payroll_domain(date_from, date_to, employee=None)` — shared attendance
  domain (excludes Volunteers dept + charity-tagged hours).
- `_generate_timecard_pdf(date_from, date_to, employee=None)` — `search_count`
  guards against empty periods, then renders `action_report_timecard_pdf`.
  Returns bytes or `None`.
- `_send_employee_email(employee, date_from, date_to, pdf, kind)` — builds a
  `mail.mail` to `employee.work_email`, attaches the PDF, sets
  `mail_server_id` from settings when configured. `kind` ∈
  `{'post_shift', 'scheduled'}` varies the subject/intro; both carry the
  verification notice.
- `_mail_server_id()` — reads the configured `ir.mail_server` id (or False).

Entry points:

- `_cron_send_post_shift_emails()` — every-5-min cron; finds attendances
  with `x_post_shift_email_pending` whose `check_out` is older than the
  delay (`post_shift_delay_minutes`, default 5) and sends them.
- `_send_post_shift_emails(attendances)` — emails each given shift's
  employee their current-period card, then marks every processed record
  `sent=True, pending=False` (even when skipped: Volunteer, no `work_email`,
  or no payroll hours) so it isn't re-scanned. Honors the post-shift toggle.
- `_cron_send_timecard_email()` — daily cron; on a send day, emails **every**
  paid employee with hours in the completed period their own card.
- `_cron_check_long_shifts()` — hourly cron; finds open attendances older
  than the threshold (default 20h) not yet alerted, and calls
  `_send_long_shift_alert`, setting `x_long_shift_alerted`.
- `_send_long_shift_alert(attendance)` — emails the employee **and** their
  `attendance_manager_id` (Attendance approver) that they may have missed a
  clock-out. Approver access is guarded with a `_fields` check.

### 3.5 `res.config.settings` (extended) — `models/res_config_settings.py`

Adds the **Timecard Email** block to *Settings → Attendances*. Values are
persisted in `ir.config_parameter` (prefix `elksattendance.`), not on the
company, since this is a single-lodge system:

| Setting field | `ir.config_parameter` key | Default |
|---|---|---|
| `timecard_mail_server_id` | `elksattendance.mail_server_id` | `''` (system default) |
| `timecard_post_shift_enabled` | `elksattendance.post_shift_enabled` | `True` |
| `timecard_post_shift_delay_minutes` | `elksattendance.post_shift_delay_minutes` | `5` |
| `timecard_email_enabled` | `elksattendance.email_enabled` | `False` |
| `timecard_email_frequency` | `elksattendance.email_frequency` | `semi_monthly` |
| `timecard_long_shift_enabled` | `elksattendance.long_shift_enabled` | `True` |
| `timecard_long_shift_threshold_hours` | `elksattendance.long_shift_threshold_hours` | `20` |

> The old single-recipient `email_recipient` parameter was removed in
> 19.0.2.0 — emails now go to each employee's `work_email`. The mail-server
> id is stored as a stringified int (`''` = use the system default server).

Read/written through the standard `get_values()` / `set_values()` pattern.

### 3.6 `elks.timecard` — `models/elks_timecard.py`

A persistent record per **employee per pay period**, inheriting
`portal.mixin` (tokenised `/my/timecard/<id>` URL) and `mail.thread`
(chatter logs every signature/reset).

Key fields: `employee_id`, `period_start`/`period_end`, computed
`attendance_ids` / `shift_count` / `total_hours` / `total_tips` (live
search over `_payroll_domain`, not stored), `state`
(`draft` / `employee_approved` / `approved`), `approver_id` (stored,
computed from `employee_id.attendance_manager_id`), and two signature
triplets (`*_signed_by` / `*_signed_name` / `*_signed_date`) for the
employee and the approver. Uniqueness is enforced with the Odoo 19
`models.Constraint` API on `(employee_id, period_start, period_end)`.

Methods: `_get_or_create(employee, ref_date)` (finds/creates the period
record using the configured frequency); `action_employee_approve` /
`action_approver_approve` (identity-checked sign-off, stamps name +
`now()`, posts to chatter); `action_reset` / `_elks_reset_signatures`;
and `_elks_reset_for_snapshot(snapshot)` — called from the
`hr.attendance` create/write/unlink hooks to reopen any signed timecard
whose period covers a changed shift. `_compute_access_url` +
`_elks_portal_url` build the portal link embedded in the emails.

`hr.employee.action_invite_to_portal` opens Odoo's standard
`portal.wizard` for the employee's `work_contact_id` so an admin grants
the login (the admin confirms inside the wizard — no silent account
creation).

---

## 4. Security model

Defined in `security/elksattendance_security.xml` (groups + record rules)
and `security/ir.model.access.csv` (model ACLs).

### 4.1 Groups

| Group (XML id) | Privilege | Implies | Purpose |
|---|---|---|---|
| `group_hr_attendance_team_manager` ("Manager: Team Attendances") | `hr_attendance.res_groups_privilege_attendances` | `hr_attendance.group_hr_attendance_officer` | View/edit attendance for the manager's subtree. |
| `group_hr_team_manager` ("Manager: Team Employees") | `hr.res_groups_privilege_employees` | `base.group_user` | View/edit employee records for the manager's subtree. |

Two stock groups are **extended** so full-access roles inherit the new
team groups (and therefore the team features):

- `hr_attendance.group_hr_attendance_user` (*Manage all attendances*)
  `implied_ids += group_hr_attendance_team_manager`
- `hr.group_hr_user` (*Manage all employees*)
  `implied_ids += group_hr_team_manager`

### 4.2 Record rules (`noupdate="1"`)

Odoo **AND**s global rules with a group's rules, and **OR**s multiple
rules that apply to the same user. The design exploits both:

| Rule | Model | Domain | Groups |
|---|---|---|---|
| `hr_employee_rule_team_manager` | `hr.employee` | `['|', ('x_manager_user_ids','in',user.id), ('user_id','=',user.id)]` | Team Employees |
| `hr_employee_rule_officer_all` | `hr.employee` | `[(1,'=',1)]` | `hr.group_hr_user` |
| `hr_attendance_rule_team_manager` | `hr.attendance` | `['|', ('employee_id.x_manager_user_ids','in',user.id), ('employee_id.user_id','=',user.id)]` | Team Attendances |
| `hr_attendance_rule_officer_all` | `hr.attendance` | `[(1,'=',1)]` | `hr_attendance.group_hr_attendance_user` |

**Why the "allow all" companion rules exist:** because Officer *implies*
Team Manager, the restrictive team rule would otherwise also apply to
officers and lock them down to their own subtree. The companion
`[(1,'=',1)]` rule is OR'd in for the higher group, restoring full access.
(The `19.0.1.1` migration exists precisely because the first release
shipped without these companions and locked admins out — see §6.)

### 4.3 Model ACLs (`ir.model.access.csv`)

- `elks.timecard.report.wizard` → `base.group_user` (RWCD) so any internal
  user can run the wizard.
- Team Employees group gets read/write/create/unlink on the supporting
  models it must touch to manage subordinates: `hr.employee`,
  `hr.employee.category`, `hr.department`, `hr.job`, `resource.resource`.

---

## 5. Tip tracking & the kiosk patch

### 5.1 Back-end controller — `controllers/kiosk.py`

Subclasses `hr_attendance.controllers.main.HrAttendance` and overrides the
two kiosk routes that return employee info (`scan_barcode`,
`manual_selection`). After the stock logic runs, `_enrich_with_tip_info()`
appends:

```python
response['x_receives_tips'] = employee.x_receives_tips
response['attendance']['id'] = employee.last_attendance_id.id
```

so the front-end knows whether to prompt and which record to write to.

A new public JSON-RPC route persists the entered tip:

```
POST /hr_attendance/save_tip   (type=jsonrpc, auth=public)
    params: token, attendance_id, tip_amount
```

It re-validates the kiosk `token`, confirms the attendance belongs to the
token's company (defense against cross-company writes), then
`attendance.write({'x_tip_amount': float(tip_amount)})`.

### 5.2 Front-end (OWL) — `static/src/`

Loaded into the **public** kiosk bundle:

```python
"assets": {
    "hr_attendance.assets_public_attendance": [
        "elksattendance/static/src/components/**/*",
        "elksattendance/static/src/public_kiosk/**/*",
    ],
},
```

- `components/kiosk_tip_entry/kiosk_tip_entry.{js,xml}` — `KioskTipEntry`,
  an OWL component modeled on `KioskPinCode`: a numeric keypad with
  decimal point, **Skip**, and **OK**. Validates one decimal point, max
  two decimal places, max length 8; supports physical-keyboard entry
  (digits, `.`, Backspace, Delete=clear, Enter=confirm, Escape=skip).
- `public_kiosk/kiosk_tip_patch.js` — `patch`es `kioskAttendanceApp` to add
  a `"tips"` display state and to intercept clock-out. After
  `onManualSelection` / `onBarcodeScanned`, if the app just switched to
  `greet` **and** `employeeData.x_receives_tips` **and** the attendance has
  a `check_out` (i.e. this was a clock-*out*), it routes to the `tips`
  screen. `onTipConfirm(amount)` calls `/hr_attendance/save_tip` (only when
  `amount > 0`) then shows `greet`; `onTipSkip()` just shows `greet`.
- `public_kiosk/kiosk_tip_patch.xml` — `t-inherit` of
  `hr_attendance.public_kiosk_app` that inserts the `KioskTipEntry`
  component after the existing `greet` block.

> **Flow:** clock-out RPC → `x_receives_tips`? → **yes**: show *tips* →
> save → show *greet*; **no**: show *greet* (unchanged).

---

## 6. Reporting

### 6.1 Report actions — `report/timecard_report.xml`

Two `ir.actions.report` against `elks.timecard.report.wizard`, both
rendering the same `report_employee_timecard` QWeb template:

| Action XML id | `report_type` | Used by |
|---|---|---|
| `action_report_timecard` | `qweb-html` | wizard "Preview" |
| `action_report_timecard_pdf` | `qweb-pdf` | wizard "Download PDF" + cron |

The template re-invokes `wizard._get_attendance_data()`, renders one
`<div class="page">` per employee (so each employee is a separate PDF
page), with a lodge-branded header (logos + name from
`elks.lodge.settings`), a per-shift table with a running total, a period
total (HH:MM **and** decimal), a tips column only when
`employee.x_receives_tips`, and a manual signature line.

### 6.2 Menus — `views/elksattendance_menus.xml`

The wizard action is exposed at:

- *Attendances → Reporting → Payroll Timecards*
- *Elks Charity → Reports → Timecard Report* (cross-module convenience
  menu; depends on `elkscharity`).

### 6.3 Scheduled email — `data/timecard_cron.xml`

Three `ir.cron` records, all `state="code"`, all `noupdate="1"`:

- **Attendance: Send Post-Shift Timecard Emails** —
  `model._cron_send_post_shift_emails()`, every 5 minutes. Sends queued
  post-shift emails whose delay window has elapsed.
- **Attendance: Send Timecard Email** — `model._cron_send_timecard_email()`,
  daily at 06:00. Fires every day but only *acts* on the configured send
  day (see §3.4), then emails each paid employee their own card.
- **Attendance: Check for Missed Clock-Outs** —
  `model._cron_check_long_shifts()`, hourly. Flags open shifts past the
  threshold and alerts the employee + Attendance approver, once each.

---

## 7. Migrations

| Version | Script | What it does |
|---|---|---|
| `19.0.1.1` | `pre-migrate.py` | Deletes the stale `hr_employee_rule_team_manager` and `hr_attendance_rule_team_manager` records (and their `ir_model_data`) so the upgraded data file re-creates them **with** the new officer "allow all" companion rules. Fixes admins being locked out. |
| `19.0.1.4` | `post-migrate.py` | Recomputes `worked_hours` on every closed attendance via raw SQL (`EXTRACT(EPOCH ...)/3600`) to strip any previously-applied resource-calendar break deduction; sets open shifts to `0`. |

Both are idempotent-safe to re-run.

---

## 8. Behavioral notes & gotchas

- **Volunteers are excluded from payroll everywhere** by department name
  string match (`department_id.name != 'Volunteers'`). This is a *string*
  comparison — renaming the department breaks the exclusion. If you ever
  need this to be robust, switch to an XML-id reference or a boolean flag
  on the department.
- **Charity hours** are excluded from payroll timecards via
  `x_charity_task_id` (from `elkscharity`). Keep that dependency.
- **Tip on the just-finished shift** is saved by a *separate* RPC after
  clock-out, so anything that reads the attendance the instant clock-out is
  recorded will not yet see that shift's tip.
- **`worked_hours` depends** are intentionally narrowed to
  `check_in`/`check_out`. If a future requirement needs break-aware hours,
  this override must be revisited rather than extended.
- **Single-lodge assumptions:** timecard-email settings live in
  `ir.config_parameter` (not per-company), and the report header reads a
  single `elks.lodge.settings` row.

---

## 9. Install / upgrade / deploy

Standard custom-addon deploy on the live server
(`lewistonelks896.com`, service `odona-lewistonelks896.com`):

```bash
cd /var/odoo/lewistonelks896.com/extra-addons/elksattendance
sudo git fetch origin main
sudo git reset --hard origin/main
sudo find /var/odoo/lewistonelks896.com -name __pycache__ -type d -exec rm -rf {} +
sudo systemctl restart odona-lewistonelks896.com
```

Then upgrade the module from the **Apps** screen. A restart is required
for Python changes; an Apps-screen upgrade alone only re-reads XML/CSV
data files. Asset (JS/XML) changes in the kiosk bundle additionally
require a browser hard-refresh / asset regeneration.

---

## 10. Quick reference — public API surface

| Symbol | Kind | Location |
|---|---|---|
| `hr.employee.x_receives_tips` | field | `models/hr_employee.py` |
| `hr.employee.x_manager_user_ids` | field (computed, stored) | `models/hr_employee.py` |
| `hr.attendance.x_is_tipped_shift` | field (related, stored) | `models/hr_attendance.py` |
| `hr.attendance.x_tip_amount` | field | `models/hr_attendance.py` |
| `hr.attendance._compute_worked_hours` | override | `models/hr_attendance.py` |
| `elks.timecard.report.wizard` | TransientModel | `wizard/timecard_report_wizard.py` |
| `elksattendance.timecard.cron._cron_send_timecard_email` | cron method | `models/timecard_cron.py` |
| `elksattendance.timecard.cron._cron_check_long_shifts` | cron method | `models/timecard_cron.py` |
| `elksattendance.timecard.cron._send_post_shift_emails` | helper (clock-out) | `models/timecard_cron.py` |
| `hr.attendance.x_post_shift_email_sent` / `x_long_shift_alerted` | fields (dedup flags) | `models/hr_attendance.py` |
| `POST /hr_attendance/save_tip` | controller route | `controllers/kiosk.py` |
| `elks.timecard` | model (portal.mixin + mail.thread) | `models/elks_timecard.py` |
| `elks.timecard.action_employee_approve` / `action_approver_approve` | sign-off methods | `models/elks_timecard.py` |
| `hr.employee.action_invite_to_portal` | portal-grant action | `models/hr_employee.py` |
| `GET /my/timecards` / `/my/timecard/<id>` | portal routes | `controllers/portal.py` |
| `POST /my/timecard/<id>/approve` | portal approve route | `controllers/portal.py` |
| `elks_timecard_rule_user` / `_officer` / `_portal` | record rules | `security/elksattendance_security.xml` |
| `group_hr_attendance_team_manager` | res.groups | `security/elksattendance_security.xml` |
| `group_hr_team_manager` | res.groups | `security/elksattendance_security.xml` |
| `action_report_timecard` / `_pdf` | ir.actions.report | `report/timecard_report.xml` |
| `ir_cron_timecard_email` | ir.cron | `data/timecard_cron.xml` |
