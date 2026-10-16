# -*- coding: utf-8 -*-
# =============================================================================
# === HUMAN ===
# The "Payroll Timecards" report tool: pick employees and a pay period (or a
# custom range), step backward/forward through periods, and print a PDF or
# export a QuickBooks-ready CSV. It's also the engine the automatic emails use
# to build each PDF.
#
# === AI AGENT ===
# TransientModel elks.timecard.report.wizard. _get_attendance_data() is the
# shared fetch (reuses the same Volunteers + charity exclusion as the rest of
# the module) and the QWeb report (report/timecard_report.xml) calls it again
# at render time. timecard_cron._generate_timecard_pdf creates this wizard
# programmatically per employee. Pay periods are semi-monthly; CSV format is
# tuned for QuickBooks import.
# =============================================================================
"""Timecard Report Wizard.

Lets the user pick one or more employees and a date range, then
generates either a PDF timecard or a CSV file suitable for QuickBooks
timecard import.

Pay periods are semi-monthly (1st–15th and 16th–end of month).
The wizard lets you navigate forward/backward through periods so you
can easily pull reports for any past (or future) pay period.
"""
import base64
import calendar
import csv
import io
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError


MONTH_CHOICES = [
    ('1', 'January'), ('2', 'February'), ('3', 'March'),
    ('4', 'April'), ('5', 'May'), ('6', 'June'),
    ('7', 'July'), ('8', 'August'), ('9', 'September'),
    ('10', 'October'), ('11', 'November'), ('12', 'December'),
]


# === HUMAN ===
# The report wizard form: which employees, which pay period, and the buttons to
# preview/download/export.
# === AI AGENT ===
# Transient (no table). employee_ids empty = all paid staff with hours. The
# pay_period/month/year fields drive date_from/date_to via onchange; 'custom'
# unlocks free date entry. period navigation re-opens the same transient record.
class ElksTimecardReportWizard(models.TransientModel):
    _name = "elks.timecard.report.wizard"
    _description = "Timecard Report Wizard"

    employee_ids = fields.Many2many(
        "hr.employee", string="Employees",
        domain="[('department_id.name', '!=', 'Volunteers')]",
        help="Leave empty to include all paid employees with attendance in the period.",
    )

    # ------------------------------------------------------------------
    # Pay period selection
    # ------------------------------------------------------------------
    pay_period = fields.Selection([
        ('first_half', 'First half'),
        ('second_half', 'Second half'),
        ('custom', 'Custom Range'),
    ], string="Pay Period", default=lambda self: self._default_pay_period(),
        help="Semi-monthly pay period, or Custom for any date range. "
             "The exact dates follow the lodge's configured split.",
    )
    period_month = fields.Selection(
        MONTH_CHOICES, string="Month",
        default=lambda self: str(self._elks_prev_period()[0].month),
    )
    period_year = fields.Char(
        "Year",
        default=lambda self: str(self._elks_prev_period()[0].year),
    )
    period_display = fields.Char(
        "Period", compute='_compute_period_display',
    )

    date_from = fields.Date(
        "From", required=True,
        default=lambda self: self._default_date_from(),
    )
    date_to = fields.Date(
        "To", required=True,
        default=lambda self: self._default_date_to(),
    )

    # Employees with no hours (populated after report generation)
    no_hours_warning = fields.Text("Missing Hours Warning", readonly=True)

    # CSV output (stored on the transient so the user can download)
    csv_file = fields.Binary("CSV File", readonly=True)
    csv_filename = fields.Char("Filename", readonly=True)
    # Upload slot for Gusto's own import template (downloaded from Gusto's
    # "Import payroll data" screen). We fill its hours/tips and hand it back.
    gusto_template_file = fields.Binary("Gusto Template")
    gusto_template_filename = fields.Char("Gusto Template Filename")

    # ------------------------------------------------------------------
    # Defaults
    # === HUMAN ===
    # Opens the wizard already pointed at the current half-month.
    # === AI AGENT ===
    # day <= 15 -> first half, else second half. Used as field defaults.
    # ------------------------------------------------------------------
    # === HUMAN ===
    # The payroll report opens on the PREVIOUS (just-completed) pay period — that's
    # the one you run payroll for — not the in-progress one.
    # === AI AGENT ===
    # Previous period = the period before the one containing today, from the SAME
    # cut-over-aware math the timecards use (elksattendance.timecard.cron). This
    # keeps the report's dates aligned with how shifts are actually bucketed.
    @api.model
    def _elks_prev_period(self):
        Cron = self.env['elksattendance.timecard.cron']
        freq = self.env['elks.timecard']._frequency()
        today = Cron._lodge_today()
        cur_start, _cur_end = Cron._get_current_period(today, freq)
        return Cron._get_current_period(cur_start - timedelta(days=1), freq)

    @api.model
    def _default_pay_period(self):
        start, _end = self._elks_prev_period()
        # The first period of a month always starts on the 1st.
        return 'first_half' if start.day == 1 else 'second_half'

    @api.model
    def _default_date_from(self):
        return self._elks_prev_period()[0]

    @api.model
    def _default_date_to(self):
        return self._elks_prev_period()[1]

    # ------------------------------------------------------------------
    # Computed
    # === HUMAN ===
    # The human-readable label of the selected period shown on the form.
    # === AI AGENT ===
    # Display-only; doesn't affect the query (date_from/date_to do).
    # ------------------------------------------------------------------
    @api.depends('pay_period', 'period_month', 'period_year', 'date_from', 'date_to')
    def _compute_period_display(self):
        # Always show the ACTUAL date range — unambiguous whatever the split is.
        for rec in self:
            if rec.date_from and rec.date_to:
                rec.period_display = (
                    f"{rec.date_from.strftime('%m/%d/%Y')} – "
                    f"{rec.date_to.strftime('%m/%d/%Y')}"
                )
            else:
                rec.period_display = ""

    # ------------------------------------------------------------------
    # Onchange: recalculate dates when period/month/year changes
    # === HUMAN ===
    # Keeps the From/To dates in sync when you change the period or month/year.
    # === AI AGENT ===
    # No-op for 'custom' (dates entered by hand). Also called explicitly from the
    # navigation buttons after they shift the period.
    # ------------------------------------------------------------------
    @api.onchange('pay_period', 'period_month', 'period_year')
    def _onchange_period(self):
        if not self.pay_period or self.pay_period == 'custom':
            return
        if not self.period_month or not self.period_year:
            return
        # Use the SAME cut-over-aware period math the timecards use, so the
        # report range matches how shifts are bucketed (1st-14th / 15th-EOM,
        # with the legacy split before the cut-over date).
        month = int(self.period_month)
        year = int(self.period_year)
        Cron = self.env['elksattendance.timecard.cron']
        freq = self.env['elks.timecard']._frequency()
        last_day = calendar.monthrange(year, month)[1]
        ref = date(year, month, 1) if self.pay_period == 'first_half' \
            else date(year, month, last_day)
        self.date_from, self.date_to = Cron._get_current_period(ref, freq)

    # ------------------------------------------------------------------
    # Period navigation buttons
    # === HUMAN ===
    # The ◀ / ▶ buttons that jump to the previous/next pay period, plus a date
    # sanity check.
    # === AI AGENT ===
    # _reopen() returns an act_window to the SAME transient record so the wizard
    # stays open as you page. Custom range shifts by its own length. _check_dates
    # is a constraint (from <= to).
    # ------------------------------------------------------------------
    def action_previous_period(self):
        """Jump to the previous pay period."""
        self.ensure_one()
        if self.pay_period == 'custom':
            # Move the entire range backward by its own length
            span = (self.date_to - self.date_from).days + 1
            self.date_to = self.date_from - timedelta(days=1)
            self.date_from = self.date_to - timedelta(days=span - 1)
        elif self.pay_period == 'first_half':
            # Go to previous month's 16th–end
            self.pay_period = 'second_half'
            month = int(self.period_month)
            year = int(self.period_year)
            if month == 1:
                self.period_month = '12'
                self.period_year = str(year - 1)
            else:
                self.period_month = str(month - 1)
        elif self.pay_period == 'second_half':
            # Go to same month's 1st–15th
            self.pay_period = 'first_half'
        self._onchange_period()
        return self._reopen()

    def action_next_period(self):
        """Jump to the next pay period."""
        self.ensure_one()
        if self.pay_period == 'custom':
            span = (self.date_to - self.date_from).days + 1
            self.date_from = self.date_to + timedelta(days=1)
            self.date_to = self.date_from + timedelta(days=span - 1)
        elif self.pay_period == 'first_half':
            # Go to same month's 16th–end
            self.pay_period = 'second_half'
        elif self.pay_period == 'second_half':
            # Go to next month's 1st–15th
            self.pay_period = 'first_half'
            month = int(self.period_month)
            year = int(self.period_year)
            if month == 12:
                self.period_month = '1'
                self.period_year = str(year + 1)
            else:
                self.period_month = str(month + 1)
        self._onchange_period()
        return self._reopen()

    def _reopen(self):
        """Re-open the wizard with updated values."""
        return {
            'type': 'ir.actions.act_window',
            'name': _('Timecard Report'),
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    @api.constrains('date_from', 'date_to')
    def _check_dates(self):
        for rec in self:
            if rec.date_from and rec.date_to and rec.date_from > rec.date_to:
                raise UserError(_("'From' date must be on or before the 'To' date."))

    # ------------------------------------------------------------------
    # Shared: fetch and organize attendance data
    # === HUMAN ===
    # Pulls the period's shifts grouped by employee, warns about anyone with no
    # hours, and provides the date/time/hours formatting helpers.
    # === AI AGENT ===
    # THE data source for both the PDF (QWeb calls it again) and the CSV. Same
    # exclusions as cron._payroll_domain (Volunteers + x_charity_task_id). Raises
    # UserError when the period is empty. _format_* convert to user tz / HH:MM.
    # ------------------------------------------------------------------
    def _get_attendance_data(self):
        """Return attendance records grouped by employee.

        When specific employees are selected, any employees with zero
        attendance in the period are reported in a warning message so
        the user knows who is missing hours.

        Returns:
            dict: {hr.employee recordset: sorted list of hr.attendance records}
        """
        self.ensure_one()
        # Reuse the cron's payroll domain so the period's day bounds are the
        # LODGE-LOCAL days converted to UTC. Naive UTC bounds (the old code here)
        # cut off a last-day evening shift: e.g. Sep 30 5:27 PM Pacific is stored
        # as Oct 1 00:27 UTC, which falls past a naive "Sep 30 23:59" upper bound
        # and so the whole timecard reported zero hours.
        Cron = self.env['elksattendance.timecard.cron']
        domain = Cron._payroll_domain(self.date_from, self.date_to)
        if self.employee_ids:
            domain.append(('employee_id', 'in', self.employee_ids.ids))

        attendances = self.env['hr.attendance'].search(domain, order='employee_id, check_in')

        grouped = defaultdict(lambda: self.env['hr.attendance'])
        for att in attendances:
            grouped[att.employee_id] |= att

        # --- Check for employees with no time entered ---
        missing_names = []
        if self.employee_ids:
            for emp in self.employee_ids:
                if emp not in grouped:
                    missing_names.append(emp.name)

        if not attendances:
            period = f"{self.date_from.strftime('%m/%d/%Y')} – {self.date_to.strftime('%m/%d/%Y')}"
            if missing_names:
                msg = _(
                    "No attendance records found for the period %(period)s.\n\n"
                    "The following employees have zero hours entered:\n"
                    "%(names)s\n\n"
                    "Please verify their time has been entered before running "
                    "this report.",
                    period=period,
                    names='\n'.join(f"  • {n}" for n in missing_names),
                )
            else:
                msg = _(
                    "No attendance records found for the period %(period)s.\n\n"
                    "No employees have time entered for this pay period.",
                    period=period,
                )
            raise UserError(msg)

        # Some employees have data, but others might be missing
        if missing_names:
            self.no_hours_warning = _(
                "Warning: The following employees have NO time entered "
                "for this period and will not appear on the report:\n"
                "%(names)s",
                names='\n'.join(f"  • {n}" for n in missing_names),
            )
        else:
            self.no_hours_warning = False

        return dict(grouped)

    # === HUMAN ===
    # Event call-out pay for this period, grouped by employee. Event work is paid
    # as a 1099 disbursement (event labor + tip-pool share) to WHOEVER worked it —
    # even a W-2 employee, whose event pay is separate from their hourly — so it
    # rides on their timecard as a "1099 Event Pay" section. Custodial call-outs
    # are excluded (not paid this way).
    # === AI AGENT ===
    # Source = elks.event.callout.line (elksevent module; optional — env-guarded
    # so elksattendance still works without it). Matched to the period by the
    # call-out's event date (callout_id.event_id.x_event_date, stored on
    # project.task). Only CERTIFIED call-outs, department != custodial, with an
    # employee_id. Per-line amount = raw_cost (hours x rate) + gratuity_share.
    def _get_event_1099_data(self):
        """Return {hr.employee: callout-line recordset} of event 1099 pay."""
        self.ensure_one()
        if 'elks.event.callout.line' not in self.env:
            return {}
        Line = self.env['elks.event.callout.line'].sudo()
        domain = [
            ('employee_id', '!=', False),
            ('callout_id.state', '=', 'certified'),
            ('callout_id.department', '!=', 'custodial'),
            ('callout_id.event_id.x_event_date', '>=', self.date_from),
            ('callout_id.event_id.x_event_date', '<=', self.date_to),
        ]
        if self.employee_ids:
            domain.append(('employee_id', 'in', self.employee_ids.ids))
        grouped = defaultdict(lambda: Line.browse())
        for line in Line.search(domain, order='id'):
            grouped[line.employee_id] |= line
        return dict(grouped)

    # === HUMAN ===
    # Everyone who belongs on the report: employees with hours PLUS anyone with
    # event 1099 pay this period (so an event-only worker still gets a timecard).
    # === AI AGENT ===
    # Union of _get_attendance_data + _get_event_1099_data keys, sorted by name.
    # _get_attendance_data still raises if the period has zero attendance overall
    # (unchanged), so this doesn't render event-only periods with no clocked time.
    def _get_report_employees(self):
        self.ensure_one()
        emps = set(self._get_attendance_data().keys()) \
            | set(self._get_event_1099_data().keys())
        # Sort by LAST name then first, to match Gusto's ordering on the
        # printed stack of timecards.
        return sorted(emps, key=lambda e: tuple(
            s.lower() for s in self._elks_split_name(e)[::-1]))

    @staticmethod
    def _elks_base_name(employee):
        """The person's plain 'First Last', with any ' - Role' qualifier removed.

        The lodge gives one person several employee records, named
        'First Last - Role' (e.g. 'Maggie Hazelbaker - Lounge'). Gusto has ONE
        record per person ('Maggie Hazelbaker'), so we drop the role suffix
        before matching or displaying the name.
        """
        name = (employee.name or '').strip()
        if ' - ' in name:
            name = name.split(' - ', 1)[0].strip()
        return name

    @classmethod
    def _elks_display_name(cls, employee):
        """'Last, First' (role suffix stripped) — matches how Gusto lists
        employees, so the printed timecards line up with the Gusto screen."""
        name = cls._elks_base_name(employee)
        if ' ' in name:
            first, last = name.split(' ', 1)
            return "%s, %s" % (last, first)
        return name

    # === HUMAN ===
    # The timezone the timecard prints in — the LODGE's local time, so a shift
    # always reads the same whether you run the report yourself or the nightly
    # email does. This is the fix for morning shifts printing as afternoon on
    # the emailed PDF.
    # === AI AGENT ===
    # The email is generated by a cron whose user has NO tz, so the old code
    # (self.env.user.tz or 'UTC') fell back to UTC and printed raw UTC times.
    # Resolve a real tz instead: explicit context tz -> running user's tz ->
    # company partner tz -> config param elksattendance.report_timezone ->
    # America/Los_Angeles (Lewiston is Pacific). Never UTC.
    def _report_tz(self):
        return (self.env.context.get('tz')
                or self.env.user.tz
                or self.env.company.partner_id.tz
                or self.env['ir.config_parameter'].sudo().get_param(
                    'elksattendance.report_timezone')
                or 'America/Los_Angeles')

    def _format_time(self, dt):
        """Time string (e.g. '09:57 AM') in the lodge timezone."""
        if not dt:
            return ''
        # context_timestamp converts the ORM's naive-UTC datetime to the
        # context tz correctly (no fragile astimezone on a naive value).
        local = fields.Datetime.context_timestamp(
            self.with_context(tz=self._report_tz()), dt)
        return local.strftime('%I:%M %p')

    def _format_date(self, dt):
        """Date string (MM/DD/YYYY) in the lodge timezone."""
        if not dt:
            return ''
        local = fields.Datetime.context_timestamp(
            self.with_context(tz=self._report_tz()), dt)
        return local.strftime('%m/%d/%Y')

    def _format_hours(self, hours):
        """Format decimal hours to HH:MM."""
        if not hours:
            return '0:00'
        h = int(hours)
        m = int(round((hours - h) * 60))
        return f'{h}:{m:02d}'

    # ------------------------------------------------------------------
    # PDF Report
    # === HUMAN ===
    # Preview on screen, or download the PDF (one page per employee).
    # === AI AGENT ===
    # Both call _get_attendance_data first (so an empty period errors before the
    # report engine runs), then hand off to the matching ir.actions.report.
    # ------------------------------------------------------------------
    def action_print_pdf(self):
        """Preview the timecard report (HTML)."""
        self.ensure_one()
        self._get_attendance_data()
        return self.env.ref(
            'elksattendance.action_report_timecard'
        ).report_action(self)

    def action_download_pdf(self):
        """Download the timecard report as PDF."""
        self.ensure_one()
        self._get_attendance_data()
        return self.env.ref(
            'elksattendance.action_report_timecard_pdf'
        ).report_action(self)

    # ------------------------------------------------------------------
    # CSV Export
    # === HUMAN ===
    # Builds a QuickBooks-friendly CSV of the period (per shift + per-employee
    # totals) and stashes it on the wizard for download.
    # === AI AGENT ===
    # Writes base64 to csv_file and re-opens the wizard so the binary field's
    # download widget appears. Column layout is tuned for QuickBooks timecard
    # import — changing it affects that import.
    # ------------------------------------------------------------------
    def action_export_csv(self):
        """Generate a QuickBooks-compatible CSV timecard file."""
        self.ensure_one()
        grouped = self._get_attendance_data()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            'Employee', 'Date', 'Check In', 'Check Out',
            'Hours Worked', 'Tips', 'Total Period Hours', 'Total Period Tips',
        ])

        for employee in sorted(grouped.keys(), key=lambda e: e.name):
            records = grouped[employee].sorted(key=lambda a: a.check_in)
            emp_total = 0.0
            emp_tips = 0.0
            is_tipped = employee.x_receives_tips
            rows = []
            for att in records:
                hours = att.worked_hours or 0.0
                tips = (att.x_tip_amount or 0.0) if is_tipped else 0.0
                emp_total += hours
                emp_tips += tips
                rows.append([
                    employee.name,
                    self._format_date(att.check_in),
                    self._format_time(att.check_in),
                    self._format_time(att.check_out) if att.check_out else 'OPEN',
                    round(hours, 2),
                    round(tips, 2) if is_tipped else '',
                    '',
                    '',
                ])
            if rows:
                rows[-1][6] = round(emp_total, 2)
                if is_tipped:
                    rows[-1][7] = round(emp_tips, 2)
            for row in rows:
                writer.writerow(row)
            writer.writerow([])

        csv_content = output.getvalue()
        output.close()

        filename = f"Timecards_{self.date_from}_{self.date_to}.csv"
        return self._elks_stash_csv(csv_content, filename)

    # ------------------------------------------------------------------
    # Gusto exports
    # === HUMAN ===
    # Two downloads built for Gusto payroll so admins stop copy-pasting:
    #   * "Gusto Hours" — a Smart Import file for W-2 staff (regular hours +
    #     paycheck tips + any event-rate pay). Upload it in Gusto's Run Payroll
    #     via "Import payroll data"; Gusto auto-matches the columns.
    #   * "Gusto 1099" — a contractor-payment file for event 1099 workers.
    #     Upload it under Pay Contractors → Upload CSV.
    # === AI AGENT ===
    # No Gusto API/partner approval needed for these — they feed Gusto's
    # spreadsheet Smart Import. Tips are PAYCHECK tips (lodge pays them on the
    # check). No overtime split (lodge never hits 40/wk). Event-rate shifts are
    # detected via hr.attendance._elks_effective_event_rate(): W-2 event pay goes
    # in an "Event pay" earnings column (map once to a Gusto earning type); 1099
    # event pay goes on the contractor file instead. Both reuse _elks_stash_csv.
    # ------------------------------------------------------------------
    def _elks_stash_csv(self, csv_content, filename):
        """Write CSV text to the download field and re-open the wizard."""
        self.ensure_one()
        self.write({
            'csv_file': base64.b64encode(csv_content.encode('utf-8')),
            'csv_filename': filename,
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _('Download Timecard CSV'),
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def _elks_is_1099(self, employee):
        """True when the employee is paid as a 1099 contractor."""
        return ('x_pay_category' in employee._fields
                and employee.x_pay_category == '1099')

    @classmethod
    def _elks_split_name(cls, employee):
        """('First', 'Last') — role suffix stripped; Gusto matches on these."""
        name = cls._elks_base_name(employee)
        if ' ' in name:
            return tuple(name.split(' ', 1))
        return name, ''

    def _elks_paycheck_tips(self, att, is_tipped):
        """Paycheck tips for one shift: approved payable tip + event gratuity +
        coordinator fee (matches the 'Total Tips' on the PDF)."""
        tip = (att.x_tip_payable or 0.0) if is_tipped else 0.0
        if 'x_gratuity_share' in att._fields:
            tip += att.x_gratuity_share or 0.0
        if 'x_coordinator_fee_share' in att._fields:
            tip += att.x_coordinator_fee_share or 0.0
        return tip

    def _elks_period_hours_by_name(self):
        """{(last_lower, first_lower): {regular, event, tips}} for W-2 staff.

        'regular' = non-event hours (primary job); 'event' = hours worked at the
        event call-out rate (go on the employee's event job row); 'tips' = the
        period's paycheck tips.
        """
        grouped = self._get_attendance_data()
        result = {}
        for employee, atts in grouped.items():
            if self._elks_is_1099(employee):
                continue
            is_tipped = employee.x_receives_tips
            reg = ev = tips = 0.0
            for att in atts:
                rate, _pay = att._elks_effective_event_rate()
                if rate > 0:
                    ev += att.worked_hours or 0.0
                else:
                    reg += att.worked_hours or 0.0
                tips += self._elks_paycheck_tips(att, is_tipped)
            first, last = self._elks_split_name(employee)
            key = (last.strip().lower(), first.strip().lower())
            # Accumulate: one person may have several role records (e.g. Lounge
            # + Kitchen), all mapping to the single Gusto employee.
            d = result.setdefault(
                key, {'regular': 0.0, 'event': 0.0, 'tips': 0.0})
            d['regular'] += reg
            d['event'] += ev
            d['tips'] += tips
        return result

    @staticmethod
    def _elks_fmt_hours(val):
        return ('%.2f' % round(val or 0.0, 2))

    def action_export_gusto_hours(self):
        """Fill Gusto's own import template with this period's hours + tips.

        The user downloads the CSV template from Gusto's "Import payroll data"
        screen (it already lists every employee, their exact job titles and
        Gusto IDs) and uploads it here. We match each row by name, put non-event
        hours + paycheck tips on the "(Primary)" job row and event hours on the
        "Event Bartender" row, and hand back the same file ready to re-upload.
        """
        self.ensure_one()
        if not self.gusto_template_file:
            raise UserError(_(
                "Upload the Gusto import template first.\n\n"
                "In Gusto: Run Payroll → Import payroll data → Download the CSV "
                "template, then upload that file in the 'Gusto Template' box here "
                "and click 'Fill Gusto Template' again."))

        raw = base64.b64decode(self.gusto_template_file).decode('utf-8-sig')
        reader = csv.DictReader(io.StringIO(raw))
        fieldnames = reader.fieldnames or []
        rows = list(reader)
        if 'last_name' not in fieldnames or 'regular_hours' not in fieldnames:
            raise UserError(_(
                "That doesn't look like a Gusto payroll template (missing the "
                "'last_name' / 'regular_hours' columns). Download a fresh "
                "template from Gusto's Import payroll data screen."))

        hours = self._elks_period_hours_by_name()

        by_emp = defaultdict(list)
        for r in rows:
            key = ((r.get('last_name') or '').strip().lower(),
                   (r.get('first_name') or '').strip().lower())
            by_emp[key].append(r)

        # We ONLY fill rows that already exist in the template — never add a new
        # row. Gusto matches each employee to one row per existing job, so an
        # invented "Event Bartender" row for someone who has no such job in Gusto
        # collides with their Primary row ("can only be selected for one row").
        matched = set()
        no_event_job = []  # (last, first, event_hours) — event work, no event job
        for key, data in hours.items():
            emp_rows = by_emp.get(key)
            if not emp_rows:
                continue
            matched.add(key)
            primary = next(
                (r for r in emp_rows
                 if '(primary)' in (r.get('title') or '').lower()),
                emp_rows[0])
            primary['regular_hours'] = self._elks_fmt_hours(data['regular'])
            if 'paycheck_tips' in fieldnames:
                primary['paycheck_tips'] = self._elks_fmt_hours(data['tips'])
            if data['event']:
                event_row = next(
                    (r for r in emp_rows
                     if r is not primary
                     and 'event' in (r.get('title') or '').lower()), None)
                if event_row:
                    event_row['regular_hours'] = self._elks_fmt_hours(
                        data['event'])
                else:
                    no_event_job.append((key[0], key[1], data['event']))

        output = io.StringIO()
        writer = csv.DictWriter(output, fieldnames=fieldnames,
                                extrasaction='ignore')
        writer.writeheader()
        for r in rows:
            writer.writerow(r)
        csv_content = output.getvalue()
        output.close()

        # Build a combined warning for anything the user must handle by hand.
        msgs = []
        unmatched = [k for k in hours if k not in matched]
        if unmatched:
            names = ", ".join(sorted("%s, %s" % (ln.title(), fn.title())
                                     for ln, fn in unmatched))
            msgs.append(_(
                "These employees have hours in Odoo but no matching row in the "
                "Gusto template (check the name spelling in Gusto): %s") % names)
        if no_event_job:
            ev = ", ".join(sorted(
                "%s, %s (%s hrs)" % (ln.title(), fn.title(),
                                     self._elks_fmt_hours(h))
                for ln, fn, h in no_event_job))
            msgs.append(_(
                "These employees worked EVENT hours but have no event job row in "
                "the Gusto template, so those hours were NOT added (they'd clash "
                "with the person's primary job). Add an event job for them in "
                "Gusto and re-download the template, or enter those hours "
                "manually: %s") % ev)
        warning = "\n\n".join(msgs) if msgs else False

        filename = f"Gusto_Timesheet_{self.date_from}_{self.date_to}.csv"
        self.write({
            'csv_file': base64.b64encode(csv_content.encode('utf-8')),
            'csv_filename': filename,
            'no_hours_warning': warning,
        })
        return {
            'type': 'ir.actions.act_window',
            'name': _('Download Gusto Timesheet'),
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def _elks_gusto_1099_rows(self):
        """{hr.employee: {'labor','grat','hours'}} of event pay for 1099s.

        Prefers actual clocked event shifts; falls back to the planned certified
        roster for a 1099 who was rostered but never clocked in.
        """
        Cron = self.env['elksattendance.timecard.cron']
        domain = Cron._payroll_domain(self.date_from, self.date_to)
        if self.employee_ids:
            domain.append(('employee_id', 'in', self.employee_ids.ids))
        atts = self.env['hr.attendance'].search(domain)

        rows = defaultdict(lambda: {'labor': 0.0, 'grat': 0.0, 'hours': 0.0})
        clocked = set()
        for att in atts:
            emp = att.employee_id
            if not self._elks_is_1099(emp):
                continue
            rate, pay = att._elks_effective_event_rate()
            if rate <= 0:
                continue
            d = rows[emp]
            d['labor'] += pay
            d['hours'] += att.worked_hours or 0.0
            if 'x_gratuity_share' in att._fields:
                d['grat'] += att.x_gratuity_share or 0.0
            clocked.add(emp)

        # Rostered-but-never-clocked 1099s: use the planned roster lines.
        for emp, lines in self._get_event_1099_data().items():
            if emp in clocked or not self._elks_is_1099(emp):
                continue
            d = rows[emp]
            d['labor'] += sum(lines.mapped('raw_cost'))
            d['grat'] += sum(lines.mapped('gratuity_share'))
            d['hours'] += sum(lines.mapped('hours'))
        return rows

    def action_export_gusto_contractors(self):
        """Gusto contractor-payment CSV for event 1099 workers."""
        self.ensure_one()
        rows = self._elks_gusto_1099_rows()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(['contractor_type', 'first_name', 'last_name',
                         'invoice_number', 'wage', 'bonus', 'hours_worked',
                         'memo'])
        period_tag = f"{self.date_from:%Y%m%d}-{self.date_to:%Y%m%d}"
        wrote = False
        for employee in sorted(rows.keys(), key=lambda e: e.name or ''):
            d = rows[employee]
            if not (d['labor'] or d['grat']):
                continue
            first, last = self._elks_split_name(employee)
            writer.writerow([
                'Individual', first, last,
                f"ELKS-{period_tag}-{employee.id}",
                round(d['labor'], 2) or '',
                round(d['grat'], 2) or '',
                round(d['hours'], 2) or '',
                f"Event pay {self.period_display}",
            ])
            wrote = True

        if not wrote:
            raise UserError(_(
                "No 1099 event pay found for the period %(period)s.",
                period=self.period_display))

        csv_content = output.getvalue()
        output.close()
        filename = f"Gusto_1099_{self.date_from}_{self.date_to}.csv"
        return self._elks_stash_csv(csv_content, filename)
