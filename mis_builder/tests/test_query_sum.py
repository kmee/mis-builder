# Copyright 2026 Namilosh
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).

from odoo.tests import TransactionCase


class TestQuerySum(TransactionCase):
    """A summed query on stored fields returns the count and the sums
    (read_group signature of Odoo 20), also when nothing matches."""

    def _report(self, domain):
        model = self.env["ir.model"]._get("res.partner")
        field = self.env["ir.model.fields"]._get("res.partner", "color")
        date_field = self.env["ir.model.fields"]._get("res.partner", "create_date")
        return self.env["mis.report"].create(
            {
                "name": "query sum",
                "query_ids": [
                    (
                        0,
                        0,
                        {
                            "name": "partners",
                            "model_id": model.id,
                            "field_ids": [(6, 0, [field.id])],
                            "aggregate": "sum",
                            "date_field": date_field.id,
                            "domain": domain,
                        },
                    )
                ],
            }
        )

    def test_sum_and_count(self):
        self.env["res.partner"].create(
            [{"name": "MQS 1", "color": 2}, {"name": "MQS 2", "color": 3}]
        )
        report = self._report("[('name', 'like', 'MQS ')]")
        today = "2000-01-01"
        res = report._fetch_queries(today, "2999-12-31")
        self.assertEqual(res["partners"].count, 2)
        self.assertEqual(res["partners"].color, 5)
        empty = report._fetch_queries(today, today)
        self.assertEqual(empty["partners"].count, 0)
