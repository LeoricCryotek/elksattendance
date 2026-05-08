# -*- coding: utf-8 -*-
"""Extend Settings → Attendance with timecard email configuration.

Stores settings in ir.config_parameter so they persist across sessions
and are not company-dependent (single-lodge system).
"""
from odoo import api, fields, models

PARAM_PREFIX = 'elksattendance.'


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    # ── Timecard Email Settings ──────────────────────────────────
    timecard_email_enabled = fields.Boolean(
        "Enable Scheduled Timecard Email",
        help="When enabled, the system will automatically generate and "
             "email a PDF timecard report on the configured schedule.",
    )
    timecard_email_recipient = fields.Char(
        "Timecard Email Recipient",
        help="Email address that receives the timecard PDF report. "
             "Typically the payroll manager or bookkeeper.",
    )
    timecard_email_frequency = fields.Selection([
        ('weekly', 'Weekly (every Monday)'),
        ('semi_monthly', 'Semi-Monthly (1st and 16th)'),
        ('monthly', 'Monthly (1st of each month)'),
    ], string="Email Frequency", default='semi_monthly',
       help="How often the timecard report is generated and emailed.",
    )

    # ── Get / Set via ir.config_parameter ────────────────────────
    @api.model
    def get_values(self):
        res = super().get_values()
        ICP = self.env['ir.config_parameter'].sudo()
        res.update({
            'timecard_email_enabled': ICP.get_param(
                PARAM_PREFIX + 'email_enabled', 'False') == 'True',
            'timecard_email_recipient': ICP.get_param(
                PARAM_PREFIX + 'email_recipient', ''),
            'timecard_email_frequency': ICP.get_param(
                PARAM_PREFIX + 'email_frequency', 'semi_monthly'),
        })
        return res

    def set_values(self):
        super().set_values()
        ICP = self.env['ir.config_parameter'].sudo()
        ICP.set_param(
            PARAM_PREFIX + 'email_enabled',
            str(self.timecard_email_enabled))
        ICP.set_param(
            PARAM_PREFIX + 'email_recipient',
            self.timecard_email_recipient or '')
        ICP.set_param(
            PARAM_PREFIX + 'email_frequency',
            self.timecard_email_frequency or 'semi_monthly')
