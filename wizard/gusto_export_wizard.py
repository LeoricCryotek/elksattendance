# -*- coding: utf-8 -*-
# ============================================================================
# === HUMAN ===
# The "Gusto Export" screen under Attendances → Reporting. Pick a pay period,
# upload Gusto's own import template, and get it back filled with this period's
# hours + paycheck tips (per job: Primary + Event). Also builds the 1099
# contractor-payment CSV. A standalone screen so it's independent of the older
# Payroll Timecards pop-up.
# === AI AGENT ===
# Thin TransientModel. It does NOT re-implement the export math: it creates a
# throwaway elks.timecard.report.wizard with the same dates/employees/template
# and calls its existing action_export_gusto_hours / action_export_gusto_
# contractors, then copies the resulting file back. New _name + fresh view, so
# it is unaffected by whatever pinned the old wizard view.
# ============================================================================
from datetime import timedelta

from odoo import api, fields, models, _


class ElksGustoExportWizard(models.TransientModel):
    _name = 'elks.gusto.export.wizard'
    _description = 'Gusto Payroll Export'

    @api.model
    def _default_dates(self):
        """Default to the most-recently-finished pay period."""
        Cron = self.env['elksattendance.timecard.cron']
        freq = self.env['elks.timecard']._frequency()
        today = Cron._lodge_today()
        cur_start, _cur_end = Cron._get_current_period(today, freq)
        prev_ref = cur_start - timedelta(days=1)
        return Cron._get_current_period(prev_ref, freq)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        start, end = self._default_dates()
        res.setdefault('date_from', start)
        res.setdefault('date_to', end)
        return res

    date_from = fields.Date("Period Start", required=True)
    date_to = fields.Date("Period End", required=True)
    employee_ids = fields.Many2many(
        'hr.employee', string="Employees",
        domain="[('department_id.name', '!=', 'Volunteers')]")

    gusto_template_file = fields.Binary("Gusto Template")
    gusto_template_filename = fields.Char("Gusto Template Filename")

    csv_file = fields.Binary("Result File", readonly=True)
    csv_filename = fields.Char("Result Filename", readonly=True)
    no_hours_warning = fields.Text("Warning", readonly=True)

    def _report_wizard(self):
        """A throwaway report wizard carrying this screen's inputs."""
        self.ensure_one()
        return self.env['elks.timecard.report.wizard'].create({
            'pay_period': 'custom',
            'date_from': self.date_from,
            'date_to': self.date_to,
            'employee_ids': [(6, 0, self.employee_ids.ids)],
            'gusto_template_file': self.gusto_template_file,
            'gusto_template_filename': self.gusto_template_filename,
        })

    def _reopen(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _('Gusto Export'),
            'res_model': self._name,
            'res_id': self.id,
            'view_mode': 'form',
            'target': 'new',
        }

    def action_fill_gusto(self):
        """Fill the uploaded Gusto template with hours + paycheck tips."""
        self.ensure_one()
        rw = self._report_wizard()
        rw.action_export_gusto_hours()
        self.write({
            'csv_file': rw.csv_file,
            'csv_filename': rw.csv_filename,
            'no_hours_warning': rw.no_hours_warning,
        })
        return self._reopen()

    def action_export_1099(self):
        """Build the Gusto contractor-payment CSV for event 1099 workers."""
        self.ensure_one()
        rw = self._report_wizard()
        rw.action_export_gusto_contractors()
        self.write({
            'csv_file': rw.csv_file,
            'csv_filename': rw.csv_filename,
        })
        return self._reopen()
