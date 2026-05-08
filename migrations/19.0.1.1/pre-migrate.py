"""
Force re-creation of noupdate record rules.

The original rules were created without companion "allow all" rules for
Officers/Admins.  Because Officer implies Team Manager, the restrictive
team-manager rule was hitting Officers too — locking admins out of
employee and attendance records.

Deleting the old rule records (and their ir_model_data entries) lets
the upgraded data file re-create them alongside the new "allow all"
companion rules.
"""
import logging

_logger = logging.getLogger(__name__)


def migrate(cr, version):
    if not version:
        return

    # XML IDs of the rules we need to force-recreate
    xml_ids = [
        'hr_employee_rule_team_manager',
        'hr_attendance_rule_team_manager',
    ]

    for xml_id in xml_ids:
        cr.execute("""
            SELECT res_id FROM ir_model_data
            WHERE module = 'elksattendance'
              AND name   = %s
              AND model  = 'ir.rule'
        """, (xml_id,))
        row = cr.fetchone()
        if row:
            rule_id = row[0]
            cr.execute("DELETE FROM ir_rule WHERE id = %s", (rule_id,))
            cr.execute("""
                DELETE FROM ir_model_data
                WHERE module = 'elksattendance'
                  AND name   = %s
                  AND model  = 'ir.rule'
            """, (xml_id,))
            _logger.info("Deleted stale rule elksattendance.%s (id=%s) "
                         "so it will be re-created with officer companion",
                         xml_id, rule_id)
