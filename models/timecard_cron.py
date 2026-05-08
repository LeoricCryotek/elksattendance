# -*- coding: utf-8 -*-
"""Scheduled timecard email sender.

A daily cron checks the configured frequency and, on the appropriate
day, generates a PDF timecard report for all paid employees covering
the most-recent completed pay period, then emails it as an attachment
to the configured recipient address using Odoo's outgoing mail server.
"""
import base64
import calendar
import logging
from collections import defaultdict
from datetime import date, datetime, time, timedelta

from odoo import api, fields, models, _

_logger = logging.getLogger(__name__)

PARAM_PREFIX = 'elksattendance.'


class TimecardCron(models.AbstractModel):
    """Abstract model that holds the cron method for timecard emails.

    Using an AbstractModel avoids creating a database table — the method
    is called by the ir.cron record defined in data/timecard_cron.xml.
    """
    _name = 'elksattendance.timecard.cron'
    _description = 'Timecard Email Cron Helper'

    @api.model
    def _cron_send_timecard_email(self):
        """Check frequency settings and send timecard email if today is a send day."""
        ICP = self.env['ir.config_parameter'].sudo()

        enabled = ICP.get_param(PARAM_PREFIX + 'email_enabled', 'False') == 'True'
        if not enabled:
            _logger.info("Timecard email is disabled — skipping.")
            return

        recipient = ICP.get_param(PARAM_PREFIX + 'email_recipient', '')
        if not recipient:
            _logger.warning("Timecard email enabled but no recipient configured — skipping.")
            return

        frequency = ICP.get_param(PARAM_PREFIX + 'email_frequency', 'semi_monthly')
        today = fields.Date.context_today(self)

        # Determine if today is a send day and what period to cover
        period = self._get_send_period(today, frequency)
        if not period:
            _logger.debug(
                "Today (%s) is not a send day for frequency '%s' — skipping.",
                today, frequency,
            )
            return

        date_from, date_to = period
        _logger.info(
            "Generating timecard email for period %s – %s (frequency: %s)",
            date_from, date_to, frequency,
        )

        # Generate the PDF
        pdf_content = self._generate_timecard_pdf(date_from, date_to)
        if not pdf_content:
            _logger.warning("No attendance data for period %s – %s — no email sent.", date_from, date_to)
            return

        # Send the email
        self._send_email(recipient, date_from, date_to, pdf_content)

    @api.model
    def _get_send_period(self, today, frequency):
        """Return (date_from, date_to) for the period to report, or None if today is not a send day.

        The report always covers the MOST RECENT COMPLETED period:
        - weekly: runs Monday, covers previous Mon–Sun
        - semi_monthly: runs 1st (covers 16th–end of prev month) and 16th (covers 1st–15th)
        - monthly: runs 1st, covers entire previous month
        """
        if frequency == 'weekly':
            # Send on Monday (weekday 0)
            if today.weekday() != 0:
                return None
            # Previous Monday through Sunday
            date_to = today - timedelta(days=1)  # Sunday
            date_from = date_to - timedelta(days=6)  # Monday
            return (date_from, date_to)

        elif frequency == 'semi_monthly':
            if today.day == 1:
                # Cover 16th – end of previous month
                prev_month_last = today - timedelta(days=1)
                date_from = prev_month_last.replace(day=16)
                date_to = prev_month_last
                return (date_from, date_to)
            elif today.day == 16:
                # Cover 1st – 15th of current month
                date_from = today.replace(day=1)
                date_to = today.replace(day=15)
                return (date_from, date_to)
            return None

        elif frequency == 'monthly':
            if today.day != 1:
                return None
            # Cover entire previous month
            prev_month_last = today - timedelta(days=1)
            date_from = prev_month_last.replace(day=1)
            date_to = prev_month_last
            return (date_from, date_to)

        return None

    @api.model
    def _generate_timecard_pdf(self, date_from, date_to):
        """Create a timecard wizard, generate the PDF, return raw bytes or None."""
        Wizard = self.env['elks.timecard.report.wizard']

        # Determine pay period type for the wizard
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
            # Empty employee_ids = all paid staff (wizard excludes Volunteers)
        })

        # Check if there's any attendance data
        domain = [
            ('check_in', '>=', datetime.combine(date_from, time.min)),
            ('check_in', '<=', datetime.combine(date_to, time.max)),
            ('employee_id.department_id.name', '!=', 'Volunteers'),
            ('x_charity_task_id', '=', False),
        ]
        if not self.env['hr.attendance'].search_count(domain):
            return None

        # Generate PDF using the report engine
        report = self.env.ref('elksattendance.action_report_timecard_pdf')
        pdf_content, _content_type = report._render_qweb_pdf(
            report.id, [wizard.id])
        return pdf_content

    @api.model
    def _send_email(self, recipient, date_from, date_to, pdf_content):
        """Send the timecard PDF as an email attachment via Odoo's mail system."""
        period_str = f"{date_from.strftime('%m-%d-%Y')}_to_{date_to.strftime('%m-%d-%Y')}"
        filename = f"Timecards_{period_str}.pdf"
        subject = f"Employee Timecards: {date_from.strftime('%m/%d/%Y')} – {date_to.strftime('%m/%d/%Y')}"

        # Get lodge name for the email body if available
        lodge_name = "Elks Lodge"
        try:
            settings = self.env['elks.lodge.settings'].sudo().search([], limit=1)
            if settings and settings.name:
                lodge_name = settings.name
                if settings.lodge_number:
                    lodge_name += f" #{settings.lodge_number}"
        except Exception:
            pass

        body_html = f"""
        <p>Attached are the employee timecards for the pay period
        <strong>{date_from.strftime('%m/%d/%Y')}</strong> through
        <strong>{date_to.strftime('%m/%d/%Y')}</strong>.</p>
        <p>This report was automatically generated by {lodge_name}'s
        attendance system. Each employee's timecard is on a separate page
        of the attached PDF.</p>
        <p style="color: #888; font-size: 0.9em;">
        This is an automated message. To change the recipient or frequency,
        go to Settings → Attendances → Timecard Email.</p>
        """

        # Create the attachment
        attachment = self.env['ir.attachment'].create({
            'name': filename,
            'type': 'binary',
            'datas': base64.b64encode(pdf_content),
            'mimetype': 'application/pdf',
        })

        # Build and send the mail
        mail_values = {
            'subject': subject,
            'body_html': body_html,
            'email_from': self.env.company.email or self.env.user.email,
            'email_to': recipient,
            'attachment_ids': [(4, attachment.id)],
            'auto_delete': True,
        }
        mail = self.env['mail.mail'].sudo().create(mail_values)
        mail.send()

        _logger.info(
            "Timecard email sent to %s for period %s – %s (%s)",
            recipient, date_from, date_to, filename,
        )
