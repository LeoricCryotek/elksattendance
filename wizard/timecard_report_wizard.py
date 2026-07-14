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
        ('first_half', '1st – 15th'),
        ('second_half', '16th – End of Month'),
        ('custom', 'Custom Range'),
    ], string="Pay Period", default=lambda self: self._default_pay_period(),
        help="Semi-monthly pay period, or Custom for any date range.",
    )
    period_month = fields.Selection(
        MONTH_CHOICES, string="Month",
        default=lambda self: str(fields.Date.context_today(self).month),
    )
    period_year = fields.Char(
        "Year",
        default=lambda self: str(fields.Date.context_today(self).year),
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

    # ------------------------------------------------------------------
    # Defaults
    # === HUMAN ===
    # Opens the wizard already pointed at the current half-month.
    # === AI AGENT ===
    # day <= 15 -> first half, else second half. Used as field defaults.
    # ------------------------------------------------------------------
    @api.model
    def _default_pay_period(self):
        today = fields.Date.context_today(self)
        return 'first_half' if today.day <= 15 else 'second_half'

    @api.model
    def _default_date_from(self):
        today = fields.Date.context_today(self)
        if today.day <= 15:
            return today.replace(day=1)
        return today.replace(day=16)

    @api.model
    def _default_date_to(self):
        today = fields.Date.context_today(self)
        if today.day <= 15:
            return today.replace(day=15)
        last_day = calendar.monthrange(today.year, today.month)[1]
        return today.replace(day=last_day)

    # ------------------------------------------------------------------
    # Computed
    # === HUMAN ===
    # The human-readable label of the selected period shown on the form.
    # === AI AGENT ===
    # Display-only; doesn't affect the query (date_from/date_to do).
    # ------------------------------------------------------------------
    @api.depends('pay_period', 'period_month', 'period_year', 'date_from', 'date_to')
    def _compute_period_display(self):
        month_map = dict(MONTH_CHOICES)
        for rec in self:
            if rec.pay_period == 'custom':
                if rec.date_from and rec.date_to:
                    rec.period_display = (
                        f"{rec.date_from.strftime('%m/%d/%Y')} – "
                        f"{rec.date_to.strftime('%m/%d/%Y')}"
                    )
                else:
                    rec.period_display = "Custom"
            elif rec.period_month and rec.period_year:
                mname = month_map.get(rec.period_month, '?')
                half = "1st – 15th" if rec.pay_period == 'first_half' else "16th – End"
                rec.period_display = f"{mname} {rec.period_year} ({half})"
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
        month = int(self.period_month)
        year = int(self.period_year)
        if self.pay_period == 'first_half':
            self.date_from = date(year, month, 1)
            self.date_to = date(year, month, 15)
        elif self.pay_period == 'second_half':
            last_day = calendar.monthrange(year, month)[1]
            self.date_from = date(year, month, 16)
            self.date_to = date(year, month, last_day)

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
        domain = [
            ('check_in', '>=', datetime.combine(self.date_from, time.min)),
            ('check_in', '<=', datetime.combine(self.date_to, time.max)),
            # Payroll only: exclude Volunteers department
            ('employee_id.department_id.name', '!=', 'Volunteers'),
            # Exclude hours tagged as charity (those go on the GL report, not payroll)
            ('x_charity_task_id', '=', False),
        ]
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
