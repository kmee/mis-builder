# -*- coding: utf-8 -*-
# © 2014-2015 ACSONE SA/NV (<http://acsone.eu>)
# License AGPL-3.0 or later (https://www.gnu.org/licenses/agpl.html).

import datetime
import dateutil
import logging
import re
import time
import traceback

import pytz

from openerp import api, fields, models, _
from openerp.tools.safe_eval import safe_eval

from .aep import AccountingExpressionProcessor as AEP
from .aggregate import _sum, _avg, _min, _max
from .accounting_none import AccountingNone

_logger = logging.getLogger(__name__)


class AutoStruct(object):

    def __init__(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)


def _get_selection_label(selection, value):
    for v, l in selection:
        if v == value:
            return l
    return ''


def _utc_midnight(d, tz_name, add_day=0):
    d = fields.Datetime.from_string(d) + datetime.timedelta(days=add_day)
    utc_tz = pytz.timezone('UTC')
    context_tz = pytz.timezone(tz_name)
    local_timestamp = context_tz.localize(d, is_dst=False)
    return fields.Datetime.to_string(local_timestamp.astimezone(utc_tz))


def _python_var(var_str):
    return re.sub(r'\W|^(?=\d)', '_', var_str).lower()


def _is_valid_python_var(name):
    return re.match("[_A-Za-z][_a-zA-Z0-9]*$", name)


def _sub_expressions(expression):
    return re.findall(r'[\w,\.]+', expression)


class MisReportKpi(models.Model):
    """ A KPI is an element (ie a line) of a MIS report.

    In addition to a name and description, it has an expression
    to compute it based on queries defined in the MIS report.
    It also has various informations defining how to render it
    (numeric or percentage or a string, a prefix, a suffix, divider) and
    how to render comparison of two values of the KPI.
    KPI's have a sequence and are ordered inside the MIS report.
    """

    _name = 'mis.report.kpi'

    name = fields.Char(size=32, required=True,
                       string='Name')
    description = fields.Char(required=True,
                              string='Description',
                              translate=True)
    column_break = fields.Boolean(string='Column Break?',
                                  default=False)
    column_title = fields.Boolean(string='Column Title?',
                                  default=False)
    invisible = fields.Boolean(string='Invisible?')
    expression = fields.Char(required=True,
                             string='Expression')
    default_css_style = fields.Char(string='Default CSS style')
    css_style = fields.Char(string='CSS style expression')
    type = fields.Selection([('num', _('Numeric')),
                             ('pct', _('Percentage')),
                             ('str', _('String'))],
                            required=True,
                            string='Type',
                            default='num')
    divider = fields.Selection([('1e-6', _('µ')),
                                ('1e-3', _('m')),
                                ('1', _('1')),
                                ('1e3', _('k')),
                                ('1e6', _('M'))],
                               string='Factor',
                               default='1')
    dp = fields.Integer(string='Rounding', default=0)
    prefix = fields.Char(size=16, string='Prefix')
    suffix = fields.Char(size=16, string='Suffix')
    compare_method = fields.Selection([('diff', _('Difference')),
                                       ('pct', _('Percentage')),
                                       ('none', _('None'))],
                                      required=True,
                                      string='Comparison Method',
                                      default='pct')
    sequence = fields.Integer(string='Sequence', default=100)
    report_id = fields.Many2one('mis.report',
                                string='Report',
                                ondelete='cascade')

    _order = 'sequence, id'

    @api.one
    @api.constrains('name')
    def _check_name(self):
        return _is_valid_python_var(self.name)

    @api.onchange('name')
    def _onchange_name(self):
        if self.name and not _is_valid_python_var(self.name):
            return {
                'warning': {
                    'title': 'Invalid name %s' % self.name,
                    'message': 'The name must be a valid python identifier'
                }
            }

    @api.onchange('description')
    def _onchange_description(self):
        """ construct name from description """
        if self.description and not self.name:
            self.name = _python_var(self.description)

    @api.onchange('type')
    def _onchange_type(self):
        if self.type == 'num':
            self.compare_method = 'pct'
            self.divider = '1'
            self.dp = 0
        elif self.type == 'pct':
            self.compare_method = 'diff'
            self.divider = '1'
            self.dp = 0
        elif self.type == 'str':
            self.compare_method = 'none'
            self.divider = ''
            self.dp = 0

    def render(self, lang_id, value):
        """ render a KPI value as a unicode string, ready for display """
        assert len(self) == 1
        if value is None or value is AccountingNone:
            return ''
        elif self.type == 'num':
            return self._render_num(lang_id, value, self.divider,
                                    self.dp, self.prefix, self.suffix)
        elif self.type == 'pct':
            return self._render_num(lang_id, value, 0.01,
                                    self.dp, '', '%')
        else:
            return unicode(value)

    def render_comparison(self, lang_id, value, base_value,
                          average_value, average_base_value):
        """ render the comparison of two KPI values, ready for display

        If the difference is 0, an empty string is returned.
        """
        assert len(self) == 1
        if value is None:
            value = AccountingNone
        if base_value is None:
            base_value = AccountingNone
        if self.type == 'pct':
            delta = value - base_value
            if delta and round(delta, self.dp) != 0:
                return self._render_num(
                    lang_id,
                    delta,
                    0.01, self.dp, '', _('pp'),
                    sign='+')
        elif self.type == 'num':
            if value and average_value:
                value = value / float(average_value)
            if base_value and average_base_value:
                base_value = base_value / float(average_base_value)
            if self.compare_method == 'diff':
                delta = value - base_value
                if delta and round(delta, self.dp) != 0:
                    return self._render_num(
                        lang_id,
                        delta,
                        self.divider, self.dp, self.prefix, self.suffix,
                        sign='+')
            elif self.compare_method == 'pct':
                if base_value and round(base_value, self.dp) != 0:
                    delta = (value - base_value) / abs(base_value)
                    if delta and round(delta * 100, self.dp) != 0:
                        return self._render_num(
                            lang_id,
                            delta,
                            0.01, self.dp, '', '%',
                            sign='+')
        return ''

    def _render_num(self, lang_id, value, divider,
                    dp, prefix, suffix, sign='-'):
        divider_label = _get_selection_label(
            self._columns['divider'].selection, divider)
        if divider_label == '1':
            divider_label = ''
        # format number following user language
        value = round(value / float(divider or 1), dp) or 0
        value = self.env['res.lang'].browse(lang_id).format(
            '%%%s.%df' % (sign, dp),
            value,
            grouping=True)
        value = u'%s\N{NARROW NO-BREAK SPACE}%s\N{NO-BREAK SPACE}%s%s' % \
            (prefix or '', value, divider_label, suffix or '')
        value = value.replace('-', u'\N{NON-BREAKING HYPHEN}')
        return value


class MisReportQuery(models.Model):
    """ A query to fetch arbitrary data for a MIS report.

    A query works on a model and has a domain and list of fields to fetch.
    At runtime, the domain is expanded with a "and" on the date/datetime field.
    """

    _name = 'mis.report.query'

    @api.one
    @api.depends('field_ids')
    def _compute_field_names(self):
        field_names = [field.name for field in self.field_ids]
        self.field_names = ', '.join(field_names)

    name = fields.Char(size=32, required=True,
                       string='Name')
    model_id = fields.Many2one('ir.model', required=True,
                               string='Model')
    field_ids = fields.Many2many('ir.model.fields', required=True,
                                 string='Fields to fetch')
    field_names = fields.Char(compute='_compute_field_names',
                              string='Fetched fields name')
    aggregate = fields.Selection([('sum', _('Sum')),
                                  ('avg', _('Average')),
                                  ('min', _('Min')),
                                  ('max', _('Max'))],
                                 string='Aggregate')
    date_field = fields.Many2one('ir.model.fields', required=True,
                                 string='Date field',
                                 domain=[('ttype', 'in',
                                         ('date', 'datetime'))])
    domain = fields.Char(string='Domain')
    report_id = fields.Many2one('mis.report', string='Report',
                                ondelete='cascade')

    _order = 'name'

    @api.one
    @api.constrains('name')
    def _check_name(self):
        return _is_valid_python_var(self.name)


class MisReport(models.Model):
    """ A MIS report template (without period information)

    The MIS report holds:
    * a list of explicit queries; the result of each query is
      stored in a variable with same name as a query, containing as list
      of data structures populated with attributes for each fields to fetch;
      when queries have an aggregate method and no fields to group, it returns
      a data structure with the aggregated fields
    * a list of KPI to be evaluated based on the variables resulting
      from the accounting data and queries (KPI expressions can references
      queries and accounting expression - see AccoutingExpressionProcessor)
    """

    _name = 'mis.report'

    name = fields.Char(required=True,
                       string='Name', translate=True)
    description = fields.Char(required=False,
                              string='Description', translate=True)
    query_ids = fields.One2many('mis.report.query', 'report_id',
                                string='Queries',
                                copy=True)
    kpi_ids = fields.One2many('mis.report.kpi', 'report_id',
                              string='KPI\'s',
                              copy=True)
    matrix = fields.Boolean(string='Matrix report?')
    manual_position = fields.Boolean(string='Manual position?')
    period_ids = fields.One2many('mis.report.period', 'report_id',
                                 string='Lines',
                                 copy=True)
    event_ids = fields.One2many('mis.report.event', 'report_id',
                                string='Events',
                                copy=True)
    position_ids = fields.One2many(
        comodel_name='mis.report.position',
        inverse_name='report_id',
        string='Position',
        copy=True
    )


    code = fields.Char(size=32, string='Code', translate=True)

    @api.onchange('name')
    def _onchange_name_code(self):
        """ Construct code from Name"""
        if self.name and not self.code:
            self.code = _python_var(self.name)

    @api.one
    def copy(self, default=None):
        default = dict(default or {})
        default['name'] = _('%s (copy)') % self.name
        return super(MisReport, self).copy(default)

    # TODO: kpi name cannot be start with query name

    @api.multi
    def _prepare_aep(self, root_account):
        self.ensure_one()
        aep = AEP(self.env)
        for kpi in self.kpi_ids:
            aep.parse_expr(kpi.expression)
        aep.done_parsing(root_account)
        return aep

    @api.multi
    def _fetch_queries(self, date_from, date_to,
                       get_additional_query_filter=None, aep=None):
        self.ensure_one()
        res = {}
        for query in self.query_ids:
            model = self.env[query.model_id.model]
            eval_context = {
                'env': self.env,
                'time': time,
                'datetime': datetime,
                'dateutil': dateutil,
                # deprecated
                'uid': self.env.uid,
                'context': self.env.context,
            }
            domain = query.domain and \
                safe_eval(query.domain, eval_context) or []
            if get_additional_query_filter:
                domain.extend(get_additional_query_filter(query, aep))
            if query.date_field.ttype == 'date':
                domain.extend([(query.date_field.name, '>=', date_from),
                               (query.date_field.name, '<=', date_to)])
            else:
                datetime_from = _utc_midnight(
                    date_from, self._context.get('tz', 'UTC'))
                datetime_to = _utc_midnight(
                    date_to, self._context.get('tz', 'UTC'), add_day=1)
                domain.extend([(query.date_field.name, '>=', datetime_from),
                               (query.date_field.name, '<', datetime_to)])
            field_names = [f.name for f in query.field_ids]
            all_stored = all([model._fields[f].store for f in field_names])
            if not query.aggregate:
                data = model.search_read(domain, field_names)
                res[query.name] = [AutoStruct(**d) for d in data]
            elif query.aggregate == 'sum' and all_stored:
                # use read_group to sum stored fields
                data = model.read_group(
                    domain, field_names, [])
                s = AutoStruct(count=data[0]['__count'])
                for field_name in field_names:
                    try:
                        v = data[0][field_name]
                    except KeyError:
                        _logger.error('field %s not found in read_group '
                                      'for %s; not summable?',
                                      field_name, model._name)
                        v = AccountingNone
                    setattr(s, field_name, v)
                res[query.name] = s
            else:
                data = model.search_read(domain, field_names)
                s = AutoStruct(count=len(data))
                if query.aggregate == 'min':
                    agg = _min
                elif query.aggregate == 'max':
                    agg = _max
                elif query.aggregate == 'avg':
                    agg = _avg
                elif query.aggregate == 'sum':
                    agg = _sum
                for field_name in field_names:
                    setattr(s, field_name,
                            agg([d[field_name] for d in data]))
                res[query.name] = s
        return res

    @api.multi
    def _compute(self, lang_id, aep,
                 date_from, date_to,
                 period_from, period_to,
                 target_move,
                 get_additional_move_line_filter=None,
                 get_additional_query_filter=None,
                 period_id=None,
                 report_instance_id=None,
                 ):
        """ Evaluate a report for a given period.

        It returns a dictionary keyed on kpi.name with the following values:
            * val: the evaluated kpi, or None if there is no data or an error
            * val_r: the rendered kpi as a string, or #ERR, #DIV
            * val_c: a comment (explaining the error, typically)
            * style: the css style of the kpi
                     (may change in the future!)
            * prefix: a prefix to display in front of the rendered value
            * suffix: a prefix to display after rendered value
            * dp: the decimal precision of the kpi
            * is_percentage: true if the kpi is of percentage type
                             (may change in the future!)
            * expr: the kpi expression
            * drilldown: true if the drilldown method of
                         mis.report.instance.period is going to do something
                         useful in this kpi

        :param lang_id: id of a res.lang object
        :param aep: an AccountingExpressionProcessor instance created
                    using _prepare_aep()
        :param date_from, date_to: the starting and ending date
        :param period_from, period_to: the starting and ending accounting
                                       period (optional, if present must
                                       match date_from, date_to)
        :param target_move: all|posted
        :param get_additional_move_line_filter: a bound method that takes
                                                no arguments and returns
                                                a domain compatible with
                                                account.move.line
        :param get_additional_query_filter: a bound method that takes a single
                                            query argument and returns a
                                            domain compatible with the query
                                            underlying model
        :param period_id: an optional opaque value that is returned as
                          query_id field in the result (may change in the
                          future!)
        """
        self.ensure_one()
        res = {}

        localdict = {
            'registry': self.pool,
            'sum': _sum,
            'min': _min,
            'max': _max,
            'len': len,
            'avg': _avg,
            'AccountingNone': AccountingNone,
        }

        localdict.update(self._fetch_queries(
            date_from, date_to, get_additional_query_filter, aep))

        additional_move_line_filter = None
        if get_additional_move_line_filter:
            additional_move_line_filter = get_additional_move_line_filter(aep)
        aep.do_queries(date_from, date_to,
                       period_from, period_to,
                       target_move,
                       additional_move_line_filter)

        compute_queue = self.kpi_ids
        recompute_queue = self.env['mis.report.kpi']
        inherit_subreport_vals = {}

        while True:
            for kpi in compute_queue:
                inherit_report_id = False
                inherit_active_subreport_ids = self.env['mis.report']
                try:
                    kpi_val_comment = kpi.name + " = " + kpi.expression
                    kpi_eval_expression = aep.replace_expr(kpi.expression)

                    if '.' in kpi.expression:
                        #
                        # Sub report search
                        #
                        sub_expressions = _sub_expressions(kpi_eval_expression)
                        for sub_expression in sub_expressions:

                            if '.' not in sub_expression:
                                continue

                            report_code, kpi_name = sub_expression.split('.')
                            inherit_report_id = self.search(
                                [('code', '=', report_code)])

                            # If the report_id found really contains the
                            # kpi_name we need
                            if inherit_report_id and kpi_name in \
                                    inherit_report_id.kpi_ids.mapped('name'):

                                # Works in the original kpi_eval_expression,
                                # replacing the . (dot) by an _ (underscore)
                                # Note: Numbers, such as (800.0) shouldn't have
                                # their dot replaced by underscore
                                kpi_eval_expression = \
                                    re.sub(r'([a-zA-Z]\w+)\.([a-zA-Z]\w+)',
                                           r'\1_\2', kpi_eval_expression)

                                # Append the inherit_report_id ID to the
                                # list containing all the MisReports used
                                # in sub_expressions expressions
                                inherit_active_subreport_ids |= inherit_report_id

                                # If the inherit_subreport_vals DICT does not
                                # contains an entry for inherit_report_id.code
                                if not inherit_subreport_vals.get(
                                        inherit_report_id.code):

                                    # Creates an empty entry for it
                                    inherit_subreport_vals[
                                        inherit_report_id.code] = {}

                                    # Recursively compute all of the
                                    # inherit_report_id KPIs
                                    # TODO: Recompute only within period_id
                                    inherit_subreport_vals_res = \
                                        report_instance_id._compute(
                                            report_id=inherit_report_id
                                        )

                                    content = []
                                    for d in inherit_subreport_vals_res:
                                        content += d.get('content')

                                    for kpi_col in content:
                                        # Fill the inherit_subreport_vals DICT
                                        # with each of its KPI's informations

                                        kpi_unique_name = \
                                            kpi_col.get('kpi_unique_name')

                                        kpi_unique_col = {}
                                        for col in kpi_col.get('cols'):
                                            if (col.get('period_id')
                                                    == period_id):
                                                kpi_unique_col = col
                                                break

                                        inherit_subreport_vals.get(
                                            inherit_report_id.code)[
                                            kpi_unique_name] = \
                                            kpi_unique_col
                                        
                                        # Then fill the localdict with the KPI
                                        # val, using the underscore notation
                                        localdict[
                                            inherit_report_id.code + '_' +
                                            kpi_unique_name] = \
                                            kpi_unique_col.get('val')

                    kpi_val = safe_eval(kpi_eval_expression, localdict)
                    localdict[kpi.name] = kpi_val

                except ZeroDivisionError:
                    kpi_val = None
                    kpi_val_rendered = '#DIV/0'
                    kpi_val_comment += '\n\n%s' % (traceback.format_exc(),)
                except (NameError, ValueError):
                    recompute_queue |= kpi
                    kpi_val = None
                    kpi_val_rendered = '#ERR'
                    kpi_val_comment += '\n\n%s' % (traceback.format_exc(),)
                except:
                    kpi_val = None
                    kpi_val_rendered = '#ERR'
                    kpi_val_comment += '\n\n%s' % (traceback.format_exc(),)
                else:
                    kpi_val_rendered = kpi.render(lang_id, kpi_val)

                try:
                    kpi_style = None
                    if kpi.css_style:
                        kpi_style = safe_eval(kpi.css_style, localdict)
                except:
                    _logger.warning("error evaluating css style expression %s",
                                    kpi.css_style, exc_info=True)
                    kpi_style = None

                drilldown = (not inherit_active_subreport_ids and
                             kpi_val is not None and
                             AEP.has_account_var(kpi.expression))

                res[kpi.name] = {
                    'val': None if kpi_val is AccountingNone else kpi_val,
                    'val_r': kpi_val_rendered,
                    'val_c': kpi_val_comment,
                    'style': kpi_style,
                    'prefix': kpi.prefix,
                    'suffix': kpi.suffix,
                    'dp': kpi.dp,
                    'kpi_id': kpi.id,
                    'is_percentage': kpi.type == 'pct',
                    'period_id': period_id,
                    'expr': kpi.expression,
                    'drilldown': drilldown,
                    'sub_report_ids': inherit_active_subreport_ids.ids or
                                      False,
                    'inherit_subreport_vals': inherit_subreport_vals
                }

            if len(recompute_queue) == 0:
                # nothing to recompute, we are done
                break
            if len(recompute_queue) == len(compute_queue):
                # could not compute anything in this iteration
                # (ie real Value errors or cyclic dependency)
                # so we stop trying
                break
            # try again
            compute_queue = recompute_queue
            recompute_queue = self.env['mis.report.kpi']

        return res


class MisReportPosition(models.Model):

    _name = 'mis.report.position'
    _order = 'report_id, collumn, line'

    report_id = fields.Many2one(
        comodel_name='mis.report',
        ondelete='cascade',
        string='Report'
    )
    collumn = fields.Integer()
    line = fields.Integer()
    period_id = fields.Many2one(
        comodel_name='mis.report.period',
        ondelete='cascade',
        string='Period'
    )
    kpi_id = fields.Many2one(
        comodel_name='mis.report.kpi',
    )
    name = fields.Char()

    @api.constrains('line', 'collumn')
    def _constraints_line_col(self):
        if self.line < 0:
            raise UserWarning('Line value can\'t be negative')
        if self.collumn < 0:
            raise UserWarning('Column value can\'t be negative')


class MisReportPeriod(models.Model):
    _name = 'mis.report.period'

    _order = 'sequence, id'

    name = fields.Char(
        string='Name',
    )

    report_id = fields.Many2one(
        comodel_name='mis.report',
        ondelete='cascade',
        string='Report'
    )
    type = fields.Selection([('d', _('Day')),
                             ('w', _('Week')),
                             ('fp', _('Fiscal Period')),
                             ],
                            required=True,
                            string='Period type')
    offset = fields.Integer(string='Offset',
                            help='Offset from current period',
                            default=-1)
    duration = fields.Integer(string='Duration',
                              help='Number of periods',
                              default=1)
    sequence = fields.Integer(string='Sequence', default=1)
    event_ids = fields.One2many('mis.report.event', 'period_id',
                                string='Events',
                                readonly=False,
                                copy=True)

    _sql_constraints = [
        ('duration', 'CHECK (duration>0)',
         'Wrong duration, it must be positive!'),
        ('name_unique', 'unique(name, report_id)',
         'Period template name should be unique by report template'),
    ]


class MisReportEvent(models.Model):
    _name = 'mis.report.event'
    _order = 'sequence'

    report_id = fields.Many2one(
        comodel_name='mis.report',
        ondelete='cascade',
        string='Report'
    )
    period_id = fields.Many2one(
        comodel_name='mis.report.period',
        ondelete='cascade',
        string='Period'
    )
    name = fields.Char(size=32, required=True,
                       string='Description', translate=True)
    type = fields.Selection(
        selection=[
            ('modificativo', 'Modificativo'),
            ('permutativo', 'Permutativo'),
            ('saldo', 'Saldo'),
        ]
    )
    debit_kpi_ids = fields.Many2many(
        comodel_name='mis.report.kpi',
        relation='event_kpi_debit',
    )
    credit_kpi_ids = fields.Many2many(
        comodel_name='mis.report.kpi',
        relation='event_kpi_credit',
    )
    sequence = fields.Integer(string='Sequence', default=1)

    style_id = fields.Many2one(
        comodel_name='mis.report.style',
        string='Estilo',
    )


class MisReportInstancePeriod(models.Model):
    """ A MIS report instance has the logic to compute
    a report template for a given date period.

    Periods have a duration (day, week, fiscal period) and
    are defined as an offset relative to a pivot date.
    """

    @api.one
    @api.depends('report_instance_id.pivot_date', 'type', 'offset', 'duration')
    def _compute_dates(self):
        self.date_from = False
        self.date_to = False
        self.period_from = False
        self.period_to = False
        self.valid = False
        d = fields.Date.from_string(self.report_instance_id.pivot_date)
        if self.type == 'd':
            date_from = d + datetime.timedelta(days=self.offset)
            date_to = date_from + \
                datetime.timedelta(days=self.duration - 1)
            self.date_from = fields.Date.to_string(date_from)
            self.date_to = fields.Date.to_string(date_to)
            self.valid = True
        elif self.type == 'w':
            date_from = d - datetime.timedelta(d.weekday())
            date_from = date_from + datetime.timedelta(days=self.offset * 7)
            date_to = date_from + \
                datetime.timedelta(days=(7 * self.duration) - 1)
            self.date_from = fields.Date.to_string(date_from)
            self.date_to = fields.Date.to_string(date_to)
            self.valid = True
        elif self.type == 'fp':
            current_periods = self.env['account.period'].search(
                [('special', '=', False),
                 ('date_start', '<=', d),
                 ('date_stop', '>=', d),
                 ('company_id', '=',
                  self.report_instance_id.company_id.id)])
            if current_periods:
                all_periods = self.env['account.period'].search(
                    [('special', '=', False),
                     ('company_id', '=',
                      self.report_instance_id.company_id.id)],
                    order='date_start')
                all_period_ids = [p.id for p in all_periods]
                p = all_period_ids.index(current_periods[0].id) + self.offset
                if p >= 0 and p + self.duration <= len(all_period_ids):
                    periods = all_periods[p:p + self.duration]
                    self.date_from = periods[0].date_start
                    self.date_to = periods[-1].date_stop
                    self.period_from = periods[0]
                    self.period_to = periods[-1]
                    self.valid = True

    _name = 'mis.report.instance.period'

    name = fields.Char(size=32, required=True,
                       string='Description', translate=True)
    type = fields.Selection([('d', _('Day')),
                             ('w', _('Week')),
                             ('fp', _('Fiscal Period')),
                             # ('fy', _('Fiscal Year'))
                             ],
                            required=True,
                            string='Period type')
    offset = fields.Integer(string='Offset',
                            help='Offset from current period',
                            default=-1)
    duration = fields.Integer(string='Duration',
                              help='Number of periods',
                              default=1)
    date_from = fields.Date(compute='_compute_dates', string="From")
    date_to = fields.Date(compute='_compute_dates', string="To")
    period_from = fields.Many2one(compute='_compute_dates',
                                  comodel_name='account.period',
                                  string="From period")
    period_to = fields.Many2one(compute='_compute_dates',
                                comodel_name='account.period',
                                string="To period")
    valid = fields.Boolean(compute='_compute_dates',
                           type='boolean',
                           string='Valid')
    sequence = fields.Integer(string='Sequence', default=100)
    report_instance_id = fields.Many2one('mis.report.instance',
                                         string='Report Instance',
                                         ondelete='cascade')
    comparison_column_ids = fields.Many2many(
        comodel_name='mis.report.instance.period',
        relation='mis_report_instance_period_rel',
        column1='period_id',
        column2='compare_period_id',
        string='Compare with')
    normalize_factor = fields.Integer(
        string='Factor',
        help='Factor to use to normalize the period (used in comparison',
        default=1)
    # event_id = fields.Many2one(
    #     comodel_name='mis.report.event',
    #     string='Event',
    #     copy=True
    # )
    template_period_id = fields.Many2one(
        comodel_name='mis.report.period',
        string='Template Period',
        copy=True
    )

    _order = 'sequence, id'

    _sql_constraints = [
        ('duration', 'CHECK (duration>0)',
         'Wrong duration, it must be positive!'),
        ('normalize_factor', 'CHECK (normalize_factor>0)',
         'Wrong normalize factor, it must be positive!'),
        ('name_unique', 'unique(name, report_instance_id)',
         'Period name should be unique by report'),
    ]

    @api.multi
    def _get_additional_move_line_filter(self, aep):
        """ Prepare a filter to apply on all move lines

        This filter is applied with a AND operator on all
        accounting expression domains. This hook is intended
        to be inherited, and is useful to implement filtering
        on analytic dimensions or operational units.

        Returns an Odoo domain expression (a python list)
        compatible with account.move.line."""
        self.ensure_one()
        domain = []

        return domain
        #
        # if not self.event_id or self.event_id.type == 'saldo':
        #     return domain
        #
        # if self.event_id.type == 'modificativo':
        #     for expr in self.event_id.credit_kpi_ids.mapped(
        #             'expression'):
        #             domain += aep.get_aml_domain_for_expr(
        #                 expr,
        #                 self.date_from, self.date_to,
        #                 self.period_from, self.period_to,
        #                 self.report_instance_id.target_move)
        #     return domain
        #
        # accs_credit = []
        # for expr in self.event_id.credit_kpi_ids.mapped(
        #         'expression'):
        #     for mo in aep.ACC_RE.finditer(expr):
        #         for code in aep._parse_match_object(mo)[2]:
        #             accs_credit += list(aep._account_ids_by_code[code])
        #
        # domain_search = [('line_id.account_id', 'in', accs_credit)]
        #
        # accs_debit = []
        # for expr in self.event_id.debit_kpi_ids.mapped(
        #         'expression'):
        #     for mo in aep.ACC_RE.finditer(expr):
        #         for code in aep._parse_match_object(mo)[2]:
        #             accs_debit += list(aep._account_ids_by_code[code])
        #
        # domain_search += [('line_id.account_id', 'in', accs_debit)]
        #
        # moves = self.env['account.move'].search(domain_search)
        #
        # move_lines = []
        # for move in moves:
        #     for line_orig in move.line_id.filtered(
        #             lambda line: line.credit > 0.0 and
        #             line.account_id.id in accs_credit):
        #         if self.event_id.type == 'modificativo':
        #             move_lines += line_orig.ids
        #             continue
        #         line_dest = move.line_id.filtered(
        #             lambda line: line.debit == line_orig.credit and
        #             line.account_id.id in accs_debit)
        #         if line_dest:
        #             move_lines += line_orig.ids + line_dest.ids
        #
        # return domain + [('id', 'in', move_lines)]

    @api.multi
    def _get_additional_query_filter(self, query, aep):
        """ Prepare an additional filter to apply on the query

        This filter is combined to the query domain with a AND
        operator. This hook is intended
        to be inherited, and is useful to implement filtering
        on analytic dimensions or operational units.

        Returns an Odoo domain expression (a python list)
        compatible with the model of the query."""
        self.ensure_one()
        return []

    @api.multi
    def drilldown(self, expr):
        self.ensure_one()
        if AEP.has_account_var(expr):
            aep = AEP(self.env)
            aep.parse_expr(expr)
            aep.done_parsing(self.report_instance_id.root_account)
            domain = aep.get_aml_domain_for_expr(
                expr,
                self.date_from, self.date_to,
                self.period_from, self.period_to,
                self.report_instance_id.target_move)
            domain.extend(self._get_additional_move_line_filter(aep))
            return {
                'name': expr + ' - ' + self.name,
                'domain': domain,
                'type': 'ir.actions.act_window',
                'res_model': 'account.move.line',
                'views': [[False, 'list'], [False, 'form']],
                'view_type': 'list',
                'view_mode': 'list',
                'target': 'current',
            }
        else:
            return False

    @api.multi
    def sub_report(self, val, sub_report_ids):
        assert len(self) == 1

        context = dict(self.env.context)
        context.update({
            'sub_report_ids': sub_report_ids,
        })

        report_ids = self.env['mis.report.instance'].search([
            ('report_id', 'in', sub_report_ids)
        ])

        id_form = self.env.ref('mis_builder.'
                               'mis_report_instance_result_view_form')

        if len(sub_report_ids) > 1:
            action = {
                'type': 'ir.actions.act_window',
                'res_model': 'mis.report.instance',
                'view_mode': 'list',
                'view_type': 'list',
                'views': [(False, 'list'), (False, 'form')],
                'view_id': False,
                'domain': str([('id', 'in', report_ids.ids)]),
                'target': 'current',
                'context': context,
            }
        else:
            action = {
                'type': 'ir.actions.act_window',
                'res_model': 'mis.report.instance',
                'res_id': report_ids[0].id,
                'view_mode': 'form',
                'view_type': 'form',
                'views': [(id_form.id, 'form')],
                'view_id': id_form.id,
                'target': 'current',
                'context': context,
            }

        return action

    @api.multi
    def _compute(self, report_id, lang_id, aep):
        self.ensure_one()

        return report_id._compute(
            lang_id, aep,
            self.date_from, self.date_to,
            self.period_from, self.period_to,
            self.report_instance_id.target_move,
            self._get_additional_move_line_filter,
            self._get_additional_query_filter,
            period_id=self.id,
            report_instance_id=self.report_instance_id,
        )


class MisReportInstance(models.Model):
    """The MIS report instance combines everything to compute
    a MIS report template for a set of periods."""

    @api.one
    @api.depends('date')
    def _compute_pivot_date(self):
        if self.date:
            self.pivot_date = self.date
        else:
            self.pivot_date = fields.Date.context_today(self)

    _name = 'mis.report.instance'

    name = fields.Char(required=True,
                       string='Name', translate=True)
    description = fields.Char(required=False,
                              string='Description', translate=True)
    date = fields.Date(string='Base date',
                       help='Report base date '
                            '(leave empty to use current date)')
    pivot_date = fields.Date(compute='_compute_pivot_date',
                             string="Pivot date")
    report_id = fields.Many2one('mis.report',
                                required=True,
                                string='Report')
    period_ids = fields.One2many('mis.report.instance.period',
                                 'report_instance_id',
                                 required=True,
                                 string='Periods',
                                 copy=True)
    target_move = fields.Selection([('posted', 'All Posted Entries'),
                                    ('all', 'All Entries')],
                                   string='Target Moves',
                                   required=True,
                                   default='posted')
    company_id = fields.Many2one(comodel_name='res.company',
                                 string='Company',
                                 readonly=True,
                                 related='root_account.company_id',
                                 store=True)
    root_account = fields.Many2one(comodel_name='account.account',
                                   domain='[("parent_id", "=", False)]',
                                   string="Account chart",
                                   required=True)
    landscape_pdf = fields.Boolean(string='Landscape PDF')

    @api.one
    def copy(self, default=None):
        default = dict(default or {})
        default['name'] = _('%s (copy)') % self.name
        return super(MisReportInstance, self).copy(default)

    def _format_date(self, lang_id, date):
        # format date following user language
        date_format = self.env['res.lang'].browse(lang_id).date_format
        return datetime.datetime.strftime(
            fields.Date.from_string(date), date_format)

    @api.onchange('report_id')
    def _onchange_report_id(self):
        if not self.report_id:
            return {}
        if not self.report_id.period_ids:
            return {}
        self.period_ids = [
            [0, 0, {
                'name': period_id.name,
                'type': period_id.type,
                'offset': period_id.offset,
                'duration': period_id.duration,
                'sequence': period_id.sequence,
                'template_period_id': period_id.id,
                # 'incluir_lancamentos_de_fechamento':
                #     self.incluir_lancamentos_de_fechamento
            }] for period_id in self.report_id.period_ids]

    @api.multi
    def preview(self):
        assert len(self) == 1
        view_id = self.env.ref('mis_builder.'
                               'mis_report_instance_result_view_form')
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'mis.report.instance',
            'res_id': self.id,
            'view_mode': 'form',
            'view_type': 'form',
            'view_id': view_id.id,
            'target': 'current',
        }

    @api.multi
    def print_pdf(self):
        self.ensure_one()
        data = {'context': self.env.context}
        return {
            'name': 'MIS report instance QWEB PDF report',
            'model': 'mis.report.instance',
            'type': 'ir.actions.report.xml',
            'report_name': 'mis_builder.report_mis_report_instance',
            'report_type': 'qweb-pdf',
            'context': self.env.context,
            'data': data,
        }

    @api.multi
    def export_xls(self):
        self.ensure_one()
        return {
            'name': 'MIS report instance XLS report',
            'model': 'mis.report.instance',
            'type': 'ir.actions.report.xml',
            'report_name': 'mis.report.instance.xls',
            'report_type': 'xls',
            'context': self.env.context,
        }

    @api.multi
    def display_settings(self):
        assert len(self._ids) <= 1
        view_id = self.env.ref('mis_builder.mis_report_instance_view_form')
        return {
            'type': 'ir.actions.act_window',
            'res_model': 'mis.report.instance',
            'res_id': self.id if self.id else False,
            'view_mode': 'form',
            'view_type': 'form',
            'views': [(view_id.id, 'form')],
            'view_id': view_id.id,
            'target': 'current',
        }

    @api.multi
    def compute(self):
        self.ensure_one()

        sub_report_ids = self.env.context.get('sub_report_ids')

        report_id = self.report_id

        if sub_report_ids:
            for sub_report_id in sub_report_ids:
                sub_report = self.search([('report_id', '=', sub_report_id)])
                if self == sub_report:
                    report_id = self.env['mis.report'].browse(sub_report_id)
                    break

        return self._compute(
            report_id=report_id,
            kpi_ids=report_id.kpi_ids,
        )

    def _compute(self, report_id, kpi_ids=False):

        aep = report_id._prepare_aep(self.root_account)

        # fetch user language only once
        # TODO: is this necessary?
        lang = self.env.user.lang
        if not lang:
            lang = 'en_US'
        lang_id = self.env['res.lang'].search([('code', '=', lang)]).id

        # compute kpi values for each period
        kpi_values_by_period_ids = {}

        for period in self.period_ids:
            if not period.valid:
                continue
            kpi_values = period._compute(report_id, lang_id, aep)
            kpi_values_by_period_ids[period.id] = kpi_values

        # prepare header and content
        header = [{
            'kpi_name': '',
            'cols': []
        }]
        content = []
        rows_by_kpi_name = {}

        kpi_ids = kpi_ids or report_id.kpi_ids

        column = 0

        for kpi in kpi_ids:
            if kpi.column_break:
                column += 1
                header_column = dict(header[0])
                header += [header_column]
                if kpi.column_title:
                    header_column['kpi_name'] = kpi.description or ''
                    
            rows_by_kpi_name[kpi.name] = {
                'kpi_name': kpi.description,
                'kpi_unique_name': kpi.name,
                'cols': [],
                'default_style': kpi.default_css_style,
                'column': column,
                'invisible': kpi.column_title or kpi.invisible,
            }
            content.append(rows_by_kpi_name[kpi.name])

        # populate header and content
        for period in self.period_ids:
            if not period.valid:
                continue
            # add the column header
            if period.duration > 1 or period.type == 'w':
                # from, to
                if period.period_from and period.period_to:
                    date_from = period.period_from.name
                    date_to = period.period_to.name
                else:
                    date_from = self._format_date(lang_id, period.date_from)
                    date_to = self._format_date(lang_id, period.date_to)
                header_date = _('from %s to %s') % (date_from, date_to)
            else:
                # one period or one day
                if period.period_from and period.period_to:
                    header_date = period.period_from.name
                else:
                    header_date = self._format_date(lang_id, period.date_from)
            header[0]['cols'].append(dict(name=period.name, date=header_date))
            # add kpi values
            kpi_values = kpi_values_by_period_ids[period.id]
            for kpi_name in kpi_values:
                rows_by_kpi_name[kpi_name]['cols'].append(kpi_values[kpi_name])

            # add comparison columns
            for compare_col in period.comparison_column_ids:
                compare_kpi_values = \
                    kpi_values_by_period_ids.get(compare_col.id)
                if compare_kpi_values:
                    # add the comparison column header
                    header[0]['cols'].append(
                        dict(name=_('%s vs %s') % (period.name,
                                                   compare_col.name),
                             date=''))
                    # add comparison values
                    for kpi in report_id.kpi_ids:
                        rows_by_kpi_name[kpi.name]['cols'].append({
                            'val_r': kpi.render_comparison(
                                lang_id,
                                kpi_values[kpi.name]['val'],
                                compare_kpi_values[kpi.name]['val'],
                                period.normalize_factor,
                                compare_col.normalize_factor)
                        })

        columns = set([rows_by_kpi_name[kpi_name]['column']
                       for kpi_name in rows_by_kpi_name])

        result = []
        for column in columns:
            result.append({
                'header': [header[column]],
                'content': [
                    row for row in content
                    if row['column'] == column and not row['invisible']],
            })

        if report_id.matrix:
            for res in result:
                lines = res['header'][0]['cols']
                res['header'][0]['cols'] = [
                    dict(name=col['kpi_name'],
                         date='')
                    for col in res['content']]
                res['content'] = [
                    dict(
                        kpi_name=line['name'],
                        cols=[col['cols'][lines.index(line)]
                              for col in res['content']],
                        default_style=res['content'][0]['default_style']
                    )
                    for line in lines]

        if report_id.manual_position:
            row_list = []
            collumn_size = max(report_id.position_ids.mapped('collumn'))
            row_size = max(report_id.position_ids.mapped('line'))

            # incia matriz auxiliar no formato [coluna][linha]
            for col in range(0, collumn_size+1):
                row_list.append([{} for i in range(0, row_size+1)])

            # preenche os cabeçalhos de colunas da matriz auxiliar
            for header_position in report_id.position_ids.filtered(
                    lambda x: x.line == 0 and x.collumn > 0 and
                              x.collumn <= collumn_size):
                    row_list[header_position.line][
                        header_position.collumn] = header_position.name

            # preenche cabeçalhos de linhas da matriz auxiliar
            for line_position in report_id.position_ids.filtered(
                        lambda x: x.collumn == 0 and x.line > 0 and
                                  x.line <= row_size):
                    row_list[line_position.line][
                        line_position.collumn] = line_position.name

            # preenche as celulas da matriz auxiliar
            for position in report_id.position_ids.filtered(
                    lambda x: x.line > 0 and x.collumn > 0):
                row_pos = position.line
                col_pos = position.collumn
                for res in result:
                    for row in res['content']:
                        for cell in row['cols']:
                            if not isinstance(cell, dict):
                                continue
                            instance_period = self.env[
                                'mis.report.instance.period'].browse(
                                cell.get('period_id'))
                            kpi_compare = (
                                    cell.get('kpi_id') == position.kpi_id.id)
                            period_compare = (
                                    instance_period.template_period_id ==
                                    position.period_id)
                            if kpi_compare and period_compare:
                                row_list[row_pos][col_pos] = cell

            # preenche o dict result com os valores da matriz auxiliar
            for res in result:
                default_style = res['content'][0]['default_style']
                # primeiro os titulos de coluna
                res['header'][0]['cols'] = []
                for col in range(1, collumn_size+1):
                    res['header'][0]['cols'].append({
                        'name': row_list[0][col] or 'coluna s/ nome',
                        'date': ''
                    })

                #depois as linhas
                res['content'] = []
                for line in range(1, row_size+1):
                    kpi_name = row_list[line][0] or 'linha s/ nome'
                    cols = [row_list[line][col] for col
                            in range(1, collumn_size+1)]
                    res['content'].append(
                        {'kpi_name':kpi_name,
                         'cols': cols,
                         'default_style': default_style})

        return result
