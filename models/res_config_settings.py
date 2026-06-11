# -*- coding: utf-8 -*-
# =============================================================================
# === HUMAN ===
# The Settings → Attendances options for all the timecard automation: which mail
# server to send through, whether/when to email a card after clock-out, the
# scheduled approval-reminder email and its frequency, the missed-clock-out
# alert + threshold, and the approval-deadline reminder lead time.
#
# === AI AGENT ===
# TransientModel inheriting res.config.settings. Values are NOT real fields —
# they're persisted to ir.config_parameter under PARAM_PREFIX ('elksattendance.')
# via get_values/set_values, so they're global (single-lodge), not per-company.
# The crons/emails in timecard_cron.py read these params directly. mail_server_id
# is stored as a stringified int (''=system default). All numeric reads are
# wrapped in try/except so a bad/blank param can't break the Settings page.
# =============================================================================
"""Extend Settings → Attendances with timecard email + alert configuration.

Stores settings in ir.config_parameter so they persist across sessions
and are not company-dependent (single-lodge system).

Per-employee email model
-------------------------
Emails go to each employee's own ``work_email`` — there is no single
"recipient" address.  What is configurable here is the outgoing mail
server to send through (blank = system default), plus on/off toggles
and the long-shift threshold.
"""
from odoo import api, fields, models

PARAM_PREFIX = 'elksattendance.'


# =============================================================================
# === HUMAN ===
# The settings fields shown on the page, and the read/write that maps them to
# the saved configuration values.
#
# === AI AGENT ===
# get_values reads each param (with safe int parsing + defaults); set_values
# writes them back as strings. Field <-> param name mapping is hand-maintained —
# if you add a setting, add it in BOTH methods. Param keys drop the 'timecard_'
# field prefix (e.g. timecard_email_enabled -> 'elksattendance.email_enabled').
# =============================================================================
class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # ── Outgoing mail server (shared by all timecard emails) ─────────
    timecard_mail_server_id = fields.Many2one(
        'ir.mail_server',
        string="Timecard Outgoing Mail Server",
        help="Outgoing mail server used for all timecard emails and "
             "alerts. Leave blank to use the system default.",
    )

    # ── Post-shift (delayed) email ───────────────────────────────────
    timecard_post_shift_enabled = fields.Boolean(
        "Email Timecard After Clock-Out",
        default=True,
        help="When enabled, each employee is emailed their current-period "
             "timecard a few minutes after they clock out.",
    )
    timecard_post_shift_delay_minutes = fields.Integer(
        "Send Delay (minutes)",
        default=5,
        help="How long to wait after clock-out before sending, so any tip "
             "the employee enters at the kiosk is included on the timecard.",
    )

    # ── Scheduled (payroll-reminder) email ───────────────────────────
    timecard_email_enabled = fields.Boolean(
        "Enable Scheduled Timecard Email",
        help="When enabled, every paid employee is emailed their own "
             "timecard on the configured schedule as a payroll-processing "
             "approval reminder.",
    )
    timecard_email_frequency = fields.Selection([
        ('weekly', 'Weekly (every Monday)'),
        ('semi_monthly', 'Semi-Monthly (1st and 16th)'),
        ('monthly', 'Monthly (1st of each month)'),
    ], string="Email Frequency", default='semi_monthly',
       help="How often the scheduled timecard email is sent. Also defines "
            "the pay-period boundaries used by the post-shift email.",
    )

    # ── Long-shift ("forgot to clock out") alert ─────────────────────
    timecard_long_shift_enabled = fields.Boolean(
        "Alert on Missed Clock-Out",
        default=True,
        help="When enabled, an employee still clocked in past the "
             "threshold below triggers an email to them and their "
             "Attendance approver.",
    )
    timecard_long_shift_threshold_hours = fields.Integer(
        "Missed Clock-Out Threshold (hours)",
        default=20,
        help="Hours an employee can stay clocked in before the system "
             "flags a possible missed clock-out.",
    )

    # ── Approval deadline reminder ───────────────────────────────────
    timecard_reminder_enabled = fields.Boolean(
        "Send Approval Deadline Reminders",
        default=True,
        help="When enabled, employees (and approvers) who haven't approved "
             "are reminded before the pay period closes.",
    )
    timecard_reminder_lead_days = fields.Integer(
        "Remind Days Before Close",
        default=1,
        help="How many days before the pay period closes to send the "
             "approval reminder.",
    )

    # ── Get / Set via ir.config_parameter ────────────────────────────
    # === HUMAN ===
    # Loads the saved settings onto the page, and saves them back when you hit
    # Save.
    # === AI AGENT ===
    # Both call super() first. Reads tolerate missing/garbage params via
    # try/except with sane defaults; writes coerce to str. Booleans stored as
    # 'True'/'False' strings (compared with == 'True').
    @api.model
    def get_values(self):
        res = super().get_values()
        ICP = self.env['ir.config_parameter'].sudo()
        ms_raw = ICP.get_param(PARAM_PREFIX + 'mail_server_id', '')
        try:
            ms_id = int(ms_raw) if ms_raw else False
        except (TypeError, ValueError):
            ms_id = False
        try:
            threshold = int(float(
                ICP.get_param(PARAM_PREFIX + 'long_shift_threshold_hours', '20')
                or 20))
        except (TypeError, ValueError):
            threshold = 20
        try:
            delay = int(float(
                ICP.get_param(PARAM_PREFIX + 'post_shift_delay_minutes', '5')
                or 5))
        except (TypeError, ValueError):
            delay = 5
        try:
            lead_days = int(float(
                ICP.get_param(PARAM_PREFIX + 'reminder_lead_days', '1') or 1))
        except (TypeError, ValueError):
            lead_days = 1
        res.update({
            'timecard_mail_server_id': ms_id,
            'timecard_post_shift_enabled': ICP.get_param(
                PARAM_PREFIX + 'post_shift_enabled', 'True') == 'True',
            'timecard_post_shift_delay_minutes': delay,
            'timecard_email_enabled': ICP.get_param(
                PARAM_PREFIX + 'email_enabled', 'False') == 'True',
            'timecard_email_frequency': ICP.get_param(
                PARAM_PREFIX + 'email_frequency', 'semi_monthly'),
            'timecard_long_shift_enabled': ICP.get_param(
                PARAM_PREFIX + 'long_shift_enabled', 'True') == 'True',
            'timecard_long_shift_threshold_hours': threshold,
            'timecard_reminder_enabled': ICP.get_param(
                PARAM_PREFIX + 'reminder_enabled', 'True') == 'True',
            'timecard_reminder_lead_days': lead_days,
        })
        return res

    def set_values(self):
        super().set_values()
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param(
            PARAM_PREFIX + 'mail_server_id',
            str(self.timecard_mail_server_id.id) if self.timecard_mail_server_id else '')
        ICP.set_param(
            PARAM_PREFIX + 'post_shift_enabled',
            str(self.timecard_post_shift_enabled))
        ICP.set_param(
            PARAM_PREFIX + 'post_shift_delay_minutes',
            str(self.timecard_post_shift_delay_minutes or 5))
        ICP.set_param(
            PARAM_PREFIX + 'email_enabled',
            str(self.timecard_email_enabled))
        ICP.set_param(
            PARAM_PREFIX + 'email_frequency',
            self.timecard_email_frequency or 'semi_monthly')
        ICP.set_param(
            PARAM_PREFIX + 'long_shift_enabled',
            str(self.timecard_long_shift_enabled))
        ICP.set_param(
            PARAM_PREFIX + 'long_shift_threshold_hours',
            str(self.timecard_long_shift_threshold_hours or 20))
        ICP.set_param(
            PARAM_PREFIX + 'reminder_enabled',
            str(self.timecard_reminder_enabled))
        ICP.set_param(
            PARAM_PREFIX + 'reminder_lead_days',
            str(self.timecard_reminder_lead_days or 1))
