# -*- coding: utf-8 -*-
# =============================================================================
# === HUMAN ===
# All the automatic timecard emails and the period math behind them: the email a
# few minutes after clock-out, the scheduled "payroll is processing" reminder,
# the "you might have forgotten to clock out" alert, and the "approve before the
# deadline" nudge. Also builds the per-employee PDF.
#
# === AI AGENT ===
# AbstractModel elksattendance.timecard.cron (NO table). Invoked by the ir.cron
# records in data/timecard_cron.xml and by hr_attendance.py / elks_timecard.py.
# _payroll_domain and _get_current_period / _get_send_period are the SHARED
# definitions of "payroll-eligible attendance" and "pay period" — reused across
# the whole module; change them here and everything follows. Reads settings from
# ir.config_parameter (PARAM_PREFIX). Mail uses the configured server or default.
# =============================================================================
"""Timecard email automation.

Three behaviors live here, all sending **per employee** to the
employee's own ``work_email`` (and, for the long-shift alert, their
Attendance approver):

1. Post-shift email (instant)
   When an employee clocks out, they are emailed their current-period
   timecard PDF with a verification notice.  Triggered from
   ``hr.attendance`` create/write (see ``hr_attendance.py``).

2. Scheduled email (cron)
   On the configured frequency (weekly / semi-monthly / monthly) every
   paid employee with hours in the most-recently-completed period is
   emailed their own timecard as a payroll-processing approval reminder.

3. Long-shift alert (cron)
   Hourly, any still-open attendance older than the configured threshold
   (default 20h) triggers a "may have forgotten to clock out" email to
   the employee and their Attendance approver.

All outgoing mail uses the outgoing mail server selected in
Settings → Attendances → Timecard Email (blank = system default).
"""
import base64
import calendar
import logging
from datetime import datetime, time, timedelta

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)

PARAM_PREFIX = 'elksattendance.'

VERIFY_NOTICE = (
    "You will be paid based on this timecard if no additional time is "
    "logged prior to payroll processing. If your timecard needs adjusting, "
    "please message the updates to your supervisor immediately. Corrections "
    "are processed on the following payroll if not completed prior to "
    "processing."
)
VERIFY_RESPONSIBILITY = (
    "It is the employee's responsibility to verify their timecard prior to "
    "payroll processing."
)


class TimecardCron(models.AbstractModel):
    """Holds the cron + helper methods for timecard emails.

    AbstractModel = no database table; methods are invoked by the
    ir.cron records in data/timecard_cron.xml and by hr.attendance.
    """
    _name = 'elksattendance.timecard.cron'
    _description = 'Timecard Email Cron Helper'

    # ==================================================================
    # Shared config / helpers
    # === HUMAN ===
    # Small helpers: read settings, pick the mail server, send a plain email,
    # the lodge's display name, and THE definition of which shifts count for
    # payroll.
    # === AI AGENT ===
    # _payroll_domain is the canonical "payroll attendance" filter (excludes
    # Volunteers dept + charity-tagged hours via x_charity_task_id from
    # elkscharity). _elks_send_mail is the no-attachment sender (used by alerts/
    # reminders); _send_employee_email below is the with-PDF sender.
    # ==================================================================
    @api.model
    def _icp(self):
        return self.env['ir.config_parameter'].sudo()

    @api.model
    def _mail_server_id(self):
        """Return the configured outgoing mail server id, or False."""
        raw = self._icp().get_param(PARAM_PREFIX + 'mail_server_id', '')
        try:
            return int(raw) if raw else False
        except (TypeError, ValueError):
            return False

    @api.model
    def _post_shift_delay_minutes(self):
        """Minutes to wait after clock-out before sending (so tips land)."""
        try:
            return max(0, int(float(
                self._icp().get_param(
                    PARAM_PREFIX + 'post_shift_delay_minutes', '5') or 5)))
        except (TypeError, ValueError):
            return 5

    @api.model
    def _elks_send_mail(self, recipients, subject, body_html):
        """Send a simple HTML email (no attachment) to recipient addresses."""
        recipients = sorted({r for r in recipients if r})
        if not recipients:
            return
        vals = {
            'subject': subject,
            'body_html': body_html,
            'email_from': self.env.company.email or self.env.user.email,
            'email_to': ','.join(recipients),
            'auto_delete': True,
        }
        ms_id = self._mail_server_id()
        if ms_id:
            vals['mail_server_id'] = ms_id
        self.env['mail.mail'].sudo().create(vals).send()

    @api.model
    def _lodge_name(self):
        name = "Elks Lodge"
        try:
            settings = self.env['elks.lodge.settings'].sudo().search([], limit=1)
            if settings and settings.name:
                name = settings.name
                if settings.lodge_number:
                    name += f" #{settings.lodge_number}"
        except Exception:
            pass
        return name

    @api.model
    def _payroll_domain(self, date_from, date_to, employee=None):
        """Domain for payroll-eligible attendance in a period.

        Excludes the Volunteers department and any charity-tagged hours
        (those belong on the charity GL report, not payroll).
        """
        domain = [
            ('check_in', '>=', datetime.combine(date_from, time.min)),
            ('check_in', '<=', datetime.combine(date_to, time.max)),
            ('employee_id.department_id.name', '!=', 'Volunteers'),
            ('x_charity_task_id', '=', False),
        ]
        if employee:
            domain.append(('employee_id', '=', employee.id))
        return domain

    # ==================================================================
    # Period math
    # === HUMAN ===
    # Works out pay-period date ranges from the chosen frequency: the most-
    # recently-finished period (for the scheduled email) and the in-progress
    # period containing a date (for the post-shift email and everything else).
    # === AI AGENT ===
    # _get_send_period returns None when "today" isn't a send day (the scheduled
    # cron relies on that to no-op). _get_current_period ALWAYS returns a range
    # and is the shared period definition used by timecard creation, reminders,
    # and totals. semi_monthly = 1st–15th / 16th–EOM.
    # ==================================================================
    @api.model
    def _get_send_period(self, today, frequency):
        """Most-recently-COMPLETED period, or None if today isn't a send day."""
        if frequency == 'weekly':
            if today.weekday() != 0:                    # send Mondays
                return None
            date_to = today - timedelta(days=1)         # Sunday
            return (date_to - timedelta(days=6), date_to)
        elif frequency == 'semi_monthly':
            if today.day == 1:
                prev_last = today - timedelta(days=1)
                return (prev_last.replace(day=16), prev_last)
            elif today.day == 16:
                return (today.replace(day=1), today.replace(day=15))
            return None
        elif frequency == 'monthly':
            if today.day != 1:
                return None
            prev_last = today - timedelta(days=1)
            return (prev_last.replace(day=1), prev_last)
        return None

    @api.model
    def _get_current_period(self, ref_date, frequency):
        """The IN-PROGRESS period containing ref_date (for post-shift email)."""
        if frequency == 'weekly':
            start = ref_date - timedelta(days=ref_date.weekday())   # Monday
            return (start, start + timedelta(days=6))
        elif frequency == 'monthly':
            last = calendar.monthrange(ref_date.year, ref_date.month)[1]
            return (ref_date.replace(day=1), ref_date.replace(day=last))
        # semi_monthly (default)
        if ref_date.day <= 15:
            return (ref_date.replace(day=1), ref_date.replace(day=15))
        last = calendar.monthrange(ref_date.year, ref_date.month)[1]
        return (ref_date.replace(day=16), ref_date.replace(day=last))

    # ==================================================================
    # PDF + email building
    # === HUMAN ===
    # Renders one employee's timecard PDF for a period, and emails it to them
    # with the verification notice and a portal "review & approve" button.
    # === AI AGENT ===
    # _generate_timecard_pdf reuses the report wizard (elks.timecard.report.wizard)
    # and returns None when there's no payroll data (so we never render an empty/
    # erroring report). _send_employee_email goes to employee.work_email; the
    # portal link is best-effort in try/except so a link failure can't block mail.
    # 'kind' ∈ {'post_shift','scheduled'} only changes subject/intro wording.
    # ==================================================================
    @api.model
    def _generate_timecard_pdf(self, date_from, date_to, employee=None):
        """Render the timecard PDF for one employee (or all paid staff).

        Returns raw PDF bytes, or None when there is no payroll data in
        the period (so we never render an empty / error report).
        """
        if not self.env['hr.attendance'].search_count(
                self._payroll_domain(date_from, date_to, employee)):
            return None

        Wizard = self.env['elks.timecard.report.wizard']
        if date_from.day == 1 and date_to.day == 15:
            pay_period = 'first_half'
        elif date_from.day == 16:
            pay_period = 'second_half'
        else:
            pay_period = 'custom'

        wizard = Wizard.create({
            'pay_period': pay_period,
            'period_month': str(date_from.month),
            'period_year': str(date_from.year),
            'date_from': date_from,
            'date_to': date_to,
            'employee_ids': [(6, 0, employee.ids)] if employee else False,
        })
        report = self.env.ref('elksattendance.action_report_timecard_pdf')
        pdf_content, _ct = report._render_qweb_pdf(report.id, [wizard.id])
        return pdf_content

    @api.model
    def _send_employee_email(self, employee, date_from, date_to, pdf, kind):
        """Email one employee their own timecard PDF.

        kind: 'post_shift' (instant, after clock-out) or
              'scheduled'  (payroll-processing approval reminder).
        """
        if not employee.work_email:
            _logger.info(
                "Employee %s has no work email — timecard not sent.",
                employee.name)
            return

        period_str = (f"{date_from.strftime('%m/%d/%Y')} – "
                      f"{date_to.strftime('%m/%d/%Y')}")
        fname = (f"Timecard_{employee.name}_"
                 f"{date_from.strftime('%m-%d-%Y')}_to_"
                 f"{date_to.strftime('%m-%d-%Y')}.pdf").replace(' ', '_')

        if kind == 'scheduled':
            subject = _("Payroll is processing — verify your timecard (%s)") % period_str
            intro = (_("Payroll for the period %s is being processed. "
                       "Attached is your current timecard.") % period_str)
        else:
            subject = _("Your timecard for the current period (%s)") % period_str
            intro = _("This is your current timecard for the period %s.") % period_str

        hello = _("Hello %s,") % employee.name
        auto = _("Automated message from %s.") % self._lodge_name()

        # Portal review/approve link (best-effort — never block the email)
        review_html = ""
        try:
            tc = self.env['elks.timecard']._get_or_create(employee, date_from)
            if tc:
                review_html = (
                    "<p><a href='%s' "
                    "style='background:#003366;color:#fff;padding:8px 14px;"
                    "border-radius:4px;text-decoration:none;'>%s</a></p>"
                    % (tc._elks_portal_url(), _("Review &amp; approve your timecard")))
        except Exception:  # pragma: no cover - defensive
            _logger.exception("Could not build portal link for %s", employee.name)

        body_html = (
            "<p>%s</p>" % hello
            + "<p>%s</p>" % intro
            + "<p>%s</p>" % VERIFY_NOTICE
            + "<p><strong>%s</strong></p>" % VERIFY_RESPONSIBILITY
            + review_html
            + "<p style='color:#888;font-size:0.9em;'>%s</p>" % auto
        )

        attachment = self.env['ir.attachment'].create({
            'name': fname,
            'type': 'binary',
            'datas': base64.b64encode(pdf),
            'mimetype': 'application/pdf',
        })
        mail_vals = {
            'subject': subject,
            'body_html': body_html,
            'email_from': self.env.company.email or self.env.user.email,
            'email_to': employee.work_email,
            'attachment_ids': [(4, attachment.id)],
            'auto_delete': True,
        }
        ms_id = self._mail_server_id()
        if ms_id:
            mail_vals['mail_server_id'] = ms_id
        self.env['mail.mail'].sudo().create(mail_vals).send()
        _logger.info(
            "Timecard email (%s) sent to %s <%s> for %s",
            kind, employee.name, employee.work_email, period_str)

    # ==================================================================
    # 1. Post-shift email
    #    Shifts are QUEUED on clock-out (hr.attendance flips
    #    x_post_shift_email_pending).  This cron sends them once the
    #    configured delay has elapsed, so a tip keyed in at the kiosk
    #    right after clock-out is on the emailed card.
    # === HUMAN ===
    # Sends each employee their card a few minutes after they clock out.
    # === AI AGENT ===
    # Two-phase to keep clock-out instant: hr.attendance only flags pending;
    # this 5-min cron sends those whose check_out is older than the delay.
    # _send_post_shift_emails marks EVERY processed row done (sent+unqueued),
    # including skipped ones (Volunteer / no email / no hours), so none rescan.
    # ==================================================================
    @api.model
    def _cron_send_post_shift_emails(self):
        """Send queued post-shift emails whose delay window has passed."""
        if self._icp().get_param(
                PARAM_PREFIX + 'post_shift_enabled', 'True') != 'True':
            return
        cutoff = fields.Datetime.now() - timedelta(
            minutes=self._post_shift_delay_minutes())
        due = self.env['hr.attendance'].search([
            ('x_post_shift_email_pending', '=', True),
            ('x_post_shift_email_sent', '=', False),
            ('check_out', '!=', False),
            ('check_out', '<=', cutoff),
        ])
        if due:
            self._send_post_shift_emails(due)

    @api.model
    def _send_post_shift_emails(self, attendances):
        """Email each given closed shift's employee their current card.

        Marks every processed record done (sent + un-queued) so it is not
        re-scanned, even when the employee is skipped (Volunteer, no work
        email, or no payroll hours in the period).
        """
        if self._icp().get_param(
                PARAM_PREFIX + 'post_shift_enabled', 'True') != 'True':
            return
        frequency = self._icp().get_param(
            PARAM_PREFIX + 'email_frequency', 'semi_monthly')

        for att in attendances:
            emp = att.employee_id
            eligible = bool(
                emp and emp.work_email
                and not (emp.department_id
                         and emp.department_id.name == 'Volunteers')
            )
            if eligible:
                ref_date = fields.Date.context_today(self, att.check_out)
                date_from, date_to = self._get_current_period(ref_date, frequency)
                pdf = self._generate_timecard_pdf(
                    date_from, date_to, employee=emp)
                if pdf:
                    self._send_employee_email(
                        emp, date_from, date_to, pdf, 'post_shift')
            att.write({
                'x_post_shift_email_sent': True,
                'x_post_shift_email_pending': False,
            })

    # ==================================================================
    # 2. Scheduled email (daily cron, acts only on send days)
    # === HUMAN ===
    # On each pay-period boundary, emails every paid employee their finished
    # timecard as a "payroll is processing, please verify" reminder.
    # === AI AGENT ===
    # Runs daily but no-ops unless _get_send_period returns a range for today
    # (the send day for the frequency). One email per employee with hours in the
    # completed period. Disabled by default (email_enabled param).
    # ==================================================================
    @api.model
    def _cron_send_timecard_email(self):
        """On the configured send day, email every paid employee their card."""
        if self._icp().get_param(
                PARAM_PREFIX + 'email_enabled', 'False') != 'True':
            _logger.info("Scheduled timecard email is disabled — skipping.")
            return

        frequency = self._icp().get_param(
            PARAM_PREFIX + 'email_frequency', 'semi_monthly')
        today = fields.Date.context_today(self)
        period = self._get_send_period(today, frequency)
        if not period:
            _logger.debug("Today (%s) is not a send day for '%s'.",
                          today, frequency)
            return

        date_from, date_to = period
        attendances = self.env['hr.attendance'].search(
            self._payroll_domain(date_from, date_to))
        employees = attendances.mapped('employee_id')
        _logger.info(
            "Scheduled timecard email: %s employee(s) for %s – %s",
            len(employees), date_from, date_to)
        for emp in employees:
            pdf = self._generate_timecard_pdf(date_from, date_to, employee=emp)
            if pdf:
                self._send_employee_email(
                    emp, date_from, date_to, pdf, 'scheduled')

    # ==================================================================
    # 3. Long-shift alert (hourly cron)
    # === HUMAN ===
    # Hourly, finds anyone still clocked in past the threshold (default 20h) and
    # emails them + their approver in case they forgot to clock out.
    # === AI AGENT ===
    # Dedup via x_long_shift_alerted (set once per open shift). _send_long_shift_alert
    # builds recipients from work_email + attendance_manager_id.email. Threshold
    # is configurable (long_shift_threshold_hours). Does NOT colour rows — that
    # red row in the backend list is Odoo's native overtime decoration, separate.
    # ==================================================================
    @api.model
    def _cron_check_long_shifts(self):
        """Alert on still-open shifts older than the configured threshold."""
        icp = self._icp()
        if icp.get_param(PARAM_PREFIX + 'long_shift_enabled', 'True') != 'True':
            return
        try:
            threshold = float(
                icp.get_param(PARAM_PREFIX + 'long_shift_threshold_hours', '20')
                or 20)
        except (TypeError, ValueError):
            threshold = 20.0

        cutoff = fields.Datetime.now() - timedelta(hours=threshold)
        open_atts = self.env['hr.attendance'].search([
            ('check_out', '=', False),
            ('check_in', '<=', cutoff),
            ('x_long_shift_alerted', '=', False),
        ])
        if not open_atts:
            return
        _logger.info("Long-shift check: %s open shift(s) over %sh.",
                     len(open_atts), threshold)
        for att in open_atts:
            self._send_long_shift_alert(att)
            att.x_long_shift_alerted = True

    # ==================================================================
    # 4. Approval deadline reminders (daily cron)
    # === HUMAN ===
    # A set number of days before a pay period closes, nudges anyone who hasn't
    # approved: the employee if they haven't signed, the approver if they have.
    # === AI AGENT ===
    # Fires only on the exact day == period_end - lead_days. Ensures each paid
    # employee's current card exists, then branches on state (draft -> employee,
    # employee_approved -> approver). Lead days configurable (reminder_lead_days).
    # _send_long_shift_alert lives just below (shared helper, not a cron).
    # ==================================================================
    @api.model
    def _cron_approval_reminders(self):
        """N days before the current period closes, nudge anyone unapproved.

        Draft card  → remind the employee to approve.
        Employee-approved card → remind the approver to give final sign-off.
        """
        icp = self._icp()
        if icp.get_param(PARAM_PREFIX + 'reminder_enabled', 'True') != 'True':
            return
        try:
            lead = int(float(icp.get_param(
                PARAM_PREFIX + 'reminder_lead_days', '1') or 1))
        except (TypeError, ValueError):
            lead = 1

        frequency = icp.get_param(PARAM_PREFIX + 'email_frequency', 'semi_monthly')
        today = fields.Date.context_today(self)
        start, end = self._get_current_period(today, frequency)
        if today != end - timedelta(days=lead):
            return

        deadline = end.strftime('%m/%d/%Y')
        Timecard = self.env['elks.timecard']
        attendances = self.env['hr.attendance'].search(
            self._payroll_domain(start, end))
        for emp in attendances.mapped('employee_id'):
            tc = Timecard._get_or_create(emp, start)
            if tc.state == 'draft' and emp.work_email:
                self._elks_send_mail(
                    [emp.work_email],
                    _("Reminder: approve your timecard by %s") % deadline,
                    "<p>%s</p><p><a href='%s'>%s</a></p>" % (
                        _("Your pay period closes %(d)s. Please review and "
                          "approve your timecard before payroll is processed.",
                          d=deadline),
                        tc._elks_portal_url(), _("Review &amp; approve")))
            elif tc.state == 'employee_approved' and tc.approver_id.email:
                self._elks_send_mail(
                    [tc.approver_id.email],
                    _("Reminder: approve %s's timecard by %s")
                    % (emp.name, deadline),
                    "<p>%s</p><p><a href='%s'>%s</a></p>" % (
                        _("%(emp)s is awaiting your final approval; the pay "
                          "period closes %(d)s.", emp=emp.name, d=deadline),
                        tc._elks_portal_url(), _("Review &amp; approve")))

    @api.model
    def _send_long_shift_alert(self, attendance):
        emp = attendance.employee_id
        if not emp:
            return

        # Recipients: the employee + their Attendance approver
        recipients = set()
        if emp.work_email:
            recipients.add(emp.work_email)
        approver = (emp.attendance_manager_id
                    if 'attendance_manager_id' in emp._fields else False)
        if approver and approver.email:
            recipients.add(approver.email)
        if not recipients:
            _logger.info(
                "Long-shift alert for %s skipped — no email addresses.",
                emp.name)
            return

        check_in_local = fields.Datetime.context_timestamp(
            self, attendance.check_in)
        since_str = check_in_local.strftime('%m/%d/%Y %I:%M %p')
        elapsed = (fields.Datetime.now() - attendance.check_in).total_seconds() / 3600.0

        subject = _("Possible missed clock-out: %s") % emp.name
        line2 = _("%(name)s has been clocked in since %(since)s — about "
                  "%(hours)s hours — and may have forgotten to clock out.",
                  name=emp.name, since=since_str, hours=int(round(elapsed)))
        line3 = _("Please review the attendance record and adjust the "
                  "timecard if needed.")
        auto = _("Automated message from %s.") % self._lodge_name()
        body_html = (
            "<p>%s</p>" % _("Hello,")
            + "<p>%s</p>" % line2
            + "<p>%s</p>" % line3
            + "<p style='color:#888;font-size:0.9em;'>%s</p>" % auto
        )

        mail_vals = {
            'subject': subject,
            'body_html': body_html,
            'email_from': self.env.company.email or self.env.user.email,
            'email_to': ','.join(sorted(recipients)),
            'auto_delete': True,
        }
        ms_id = self._mail_server_id()
        if ms_id:
            mail_vals['mail_server_id'] = ms_id
        self.env['mail.mail'].sudo().create(mail_vals).send()
        _logger.info("Long-shift alert sent for %s (%.0fh) to %s",
                     emp.name, elapsed, mail_vals['email_to'])
