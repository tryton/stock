# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
import datetime
import functools
from decimal import Decimal
from collections import defaultdict

from sql import Literal, Null
from sql.aggregate import Max
from sql.functions import CurrentTimestamp
from sql.conditionals import Coalesce

from trytond.model import ModelSQL, ModelView, fields
from trytond.wizard import Wizard, StateTransition
from trytond.pyson import Eval, Or
from trytond.transaction import Transaction
from trytond.pool import Pool, PoolMeta
from trytond.tools import grouped_slice

from trytond.modules.product import TemplateFunction
from .move import StockMixin

__all__ = ['Template', 'Product',
    'ProductByLocationContext',
    'ProductQuantitiesByWarehouse', 'ProductQuantitiesByWarehouseContext',
    'RecomputeCostPrice']


def check_no_move(func):

    def find_moves(cls, records):
        pool = Pool()
        Move = pool.get('stock.move')
        if cls.__name__ == 'product.template':
            field = 'product.template'
        else:
            field = 'product'
        for sub_records in grouped_slice(records):
            moves = Move.search([
                    (field, 'in', list(map(int, sub_records))),
                    ],
                limit=1, order=[])
            if moves:
                return True
        return False

    @functools.wraps(func)
    def decorator(cls, *args):
        pool = Pool()
        Template = pool.get('product.template')
        transaction = Transaction()
        if (transaction.user != 0
                and transaction.context.get('_check_access')):
            actions = iter(args)
            for records, values in zip(actions, actions):
                for field, error in Template._modify_no_move:
                    if field in values:
                        if find_moves(cls, records):
                            Template.raise_user_error(error)
                        # No moves for those records
                        break
        func(cls, *args)
    return decorator


class Template(metaclass=PoolMeta):
    __name__ = "product.template"
    quantity = fields.Function(fields.Float('Quantity'), 'sum_product')
    forecast_quantity = fields.Function(fields.Float('Forecast Quantity'),
            'sum_product')
    cost_value = fields.Function(fields.Numeric('Cost Value'),
        'sum_product')

    def sum_product(self, name):
        if name not in ('quantity', 'forecast_quantity', 'cost_value'):
            raise Exception('Bad argument')
        sum_ = 0. if name != 'cost_value' else Decimal(0)
        for product in self.products:
            sum_ += getattr(product, name)
        return sum_

    @classmethod
    def __setup__(cls):
        super(Template, cls).__setup__()
        cls._error_messages.update({
                'change_default_uom': ('You cannot change the default uom for '
                    'a product which is associated to stock moves.'),
                'change_type': ('You cannot change the type for a product '
                    'which is associated to stock moves.'),
                })
        cls.cost_price.states['required'] = Or(
            cls.cost_price.states.get('required', True),
            Eval('type').in_(['goods', 'assets']))
        cls.cost_price.depends.append('type')
        cls._modify_no_move = [
            ('default_uom', 'change_default_uom'),
            ('type', 'change_type'),
            ]

    @classmethod
    @check_no_move
    def write(cls, *args):
        super(Template, cls).write(*args)

    @classmethod
    def recompute_cost_price(cls, templates):
        pool = Pool()
        Product = pool.get('product.product')

        products = [p for t in templates for p in t.products]
        Product.recompute_cost_price(products)


class Product(StockMixin, object, metaclass=PoolMeta):
    __name__ = "product.product"
    quantity = fields.Function(fields.Float('Quantity'), 'get_quantity',
            searcher='search_quantity')
    forecast_quantity = fields.Function(fields.Float('Forecast Quantity'),
            'get_quantity', searcher='search_quantity')
    cost_value = fields.Function(fields.Numeric('Cost Value'),
        'get_cost_value')

    @classmethod
    def get_quantity(cls, products, name):
        location_ids = Transaction().context.get('locations')
        product_ids = list(map(int, products))
        return cls._get_quantity(
            products, name, location_ids, grouping_filter=(product_ids,))

    @classmethod
    def search_quantity(cls, name, domain=None):
        location_ids = Transaction().context.get('locations')
        return cls._search_quantity(name, location_ids, domain)

    @classmethod
    def get_cost_value(cls, products, name):
        cost_values = {p.id: None for p in products}
        context = {}
        trans_context = Transaction().context
        if trans_context.get('stock_date_end'):
            # Use the last cost_price of the day
            context['_datetime'] = datetime.datetime.combine(
                trans_context['stock_date_end'], datetime.time.max)
            # The date could be before the product creation
            products = [p for p in products
                if p.create_date <= context['_datetime']]
        with Transaction().set_context(context):
            for product in cls.browse(products):
                # The product may not have a cost price
                if product.cost_price is not None:
                    cost_values[product.id] = (
                        Decimal(str(product.quantity)) * product.cost_price)
        return cost_values

    @classmethod
    @check_no_move
    def write(cls, *args):
        super(Product, cls).write(*args)

    @classmethod
    def products_by_location(cls, location_ids,
            with_childs=False, grouping=('product',), grouping_filter=None):
        """
        Compute for each location and product the stock quantity in the default
        uom of the product.

        The context with keys:
            stock_skip_warehouse: if set, quantities on a warehouse are no more
                quantities of all child locations but quantities of the storage
                zone.

        Return a dictionary with location id and grouping as key
                and quantity as value.
        """
        pool = Pool()
        Location = pool.get('stock.location')
        Move = pool.get('stock.move')

        # Skip warehouse location in favor of their storage location
        # to compute quantities. Keep track of which ids to remove
        # and to add after the query.
        storage_to_remove = set()
        wh_to_add = {}
        if Transaction().context.get('stock_skip_warehouse'):
            location_ids = set(location_ids)
            for location in Location.browse(list(location_ids)):
                if location.type == 'warehouse':
                    location_ids.remove(location.id)
                    if location.storage_location.id not in location_ids:
                        storage_to_remove.add(location.storage_location.id)
                    location_ids.add(location.storage_location.id)
                    wh_to_add[location.id] = location.storage_location.id
            location_ids = list(location_ids)

        query = Move.compute_quantities_query(location_ids, with_childs,
            grouping=grouping, grouping_filter=grouping_filter)
        if query is None:
            return {}
        quantities = Move.compute_quantities(query, location_ids, with_childs,
            grouping=grouping, grouping_filter=grouping_filter)

        if wh_to_add:
            for wh, storage in wh_to_add.items():
                for key in list(quantities.keys()):
                    if key[0] == storage:
                        quantities[(wh,) + key[1:]] = quantities[key]
                        if storage in storage_to_remove:
                            del quantities[key]
        return quantities

    @classmethod
    def recompute_cost_price(cls, products):
        digits = cls.cost_price.digits
        costs = defaultdict(list)
        for product in products:
            if product.type == 'service':
                continue
            cost = getattr(product,
                'recompute_cost_price_%s' % product.cost_price_method)()
            cost = cost.quantize(Decimal(str(10.0 ** -digits[1])))
            costs[cost].append(product)

        if not costs:
            return

        to_write = []
        for cost, records in costs.items():
            to_write.append(records)
            to_write.append({'cost_price': cost})

        # Enforce check access for account_stock*
        with Transaction().set_context(_check_access=True):
            cls.write(*to_write)

    def recompute_cost_price_fixed(self):
        return self.cost_price

    def recompute_cost_price_average(self):
        pool = Pool()
        Move = pool.get('stock.move')
        Currency = pool.get('currency.currency')
        Uom = pool.get('product.uom')

        context = Transaction().context

        if not isinstance(self.__class__.cost_price, TemplateFunction):
            product_clause = ('product', '=', self.id)
        else:
            product_clause = ('product.template', '=', self.template.id)

        moves = Move.search([
                product_clause,
                ('state', '=', 'done'),
                ('company', '=', context.get('company')),
                ['OR',
                    [
                        ('to_location.type', '=', 'storage'),
                        ('from_location.type', '!=', 'storage'),
                        ],
                    [
                        ('from_location.type', '=', 'storage'),
                        ('to_location.type', '!=', 'storage'),
                        ],
                    ],
                ], order=[('effective_date', 'ASC'), ('id', 'ASC')])

        cost_price = Decimal(0)
        quantity = 0
        for move in moves:
            qty = Uom.compute_qty(move.uom, move.quantity, self.default_uom)
            qty = Decimal(str(qty))
            if move.from_location.type == 'storage':
                qty *= -1
            if (move.from_location.type in ['supplier', 'production']
                    or move.to_location.type == 'supplier'):
                with Transaction().set_context(date=move.effective_date):
                    unit_price = Currency.compute(
                        move.currency, move.unit_price,
                        move.company.currency, round=False)
                unit_price = Uom.compute_price(move.uom, unit_price,
                    self.default_uom)
                if quantity + qty > 0 and quantity >= 0:
                    cost_price = (
                        (cost_price * quantity) + (unit_price * qty)
                        ) / (quantity + qty)
                elif qty > 0:
                    cost_price = unit_price
            quantity += qty
        return cost_price


class ProductByLocationContext(ModelView):
    'Product by Location'
    __name__ = 'product.by_location.context'
    forecast_date = fields.Date(
        'At Date', help=('Allow to compute expected '
            'stock quantities for this date.\n'
            '* An empty value is an infinite date in the future.\n'
            '* A date in the past will provide historical values.'))
    stock_date_end = fields.Function(fields.Date('At Date'),
        'on_change_with_stock_date_end')

    @staticmethod
    def default_forecast_date():
        Date = Pool().get('ir.date')
        return Date.today()

    @fields.depends('forecast_date')
    def on_change_with_stock_date_end(self, name=None):
        if self.forecast_date is None:
            return datetime.date.max
        return self.forecast_date


class ProductQuantitiesByWarehouse(ModelSQL, ModelView):
    'Product Quantities By Warehouse'
    __name__ = 'stock.product_quantities_warehouse'

    class _Date(fields.Date):
        def get(self, ids, model, name, values=None):
            if values is None:
                values = {}
            result = {}
            for v in values:
                date = v[name]
                # SQLite does not convert to date
                if isinstance(date, str):
                    date = datetime.date(*map(int, date.split('-', 2)))
                result[v['id']] = date
            return result

    date = _Date('Date')
    quantity = fields.Function(fields.Float('Quantity'), 'get_quantity')

    del _Date

    @classmethod
    def __setup__(cls):
        super(ProductQuantitiesByWarehouse, cls).__setup__()
        cls._order.insert(0, ('date', 'ASC'))

    @staticmethod
    def table_query():
        pool = Pool()
        Move = pool.get('stock.move')
        Location = pool.get('stock.location')
        Product = pool.get('product.product')
        move = from_ = Move.__table__()
        context = Transaction().context

        if context.get('product_template') is not None:
            product = Product.__table__()
            from_ = move.join(product, condition=move.product == product.id)
            product_clause = (
                product.template == context['product_template'])
        else:
            product_clause = move.product == context.get('product', -1)

        warehouse_id = context.get('warehouse', -1)
        warehouse_query = Location.search([
                ('parent', 'child_of', [warehouse_id]),
                ], query=True, order=[])
        date_column = Coalesce(move.effective_date, move.planned_date
            ).as_('date')
        return from_.select(
            Max(move.id).as_('id'),
            Literal(0).as_('create_uid'),
            CurrentTimestamp().as_('create_date'),
            Literal(None).as_('write_uid'),
            Literal(None).as_('write_date'),
            date_column,
            where=product_clause
            & (move.from_location.in_(warehouse_query)
                | move.to_location.in_(warehouse_query))
            & (Coalesce(move.effective_date, move.planned_date) != Null),
            group_by=(date_column, move.product))

    @classmethod
    def get_quantity(cls, lines, name):
        Product = Pool().get('product.product')
        trans_context = Transaction().context

        def valid_context(name):
            return (trans_context.get(name) is not None
                and isinstance(trans_context[name], int))

        if not any(map(valid_context, ['product', 'product_template'])):
            return {l.id: None for l in lines}

        if trans_context.get('product') is not None:
            grouping = ('product',)
            grouping_filter = ([trans_context['product']],)
            key = trans_context['product']
        else:
            grouping = ('product.template',)
            grouping_filter = ([trans_context.get('product_template')],)
            key = trans_context['product_template']
        warehouse_id = trans_context.get('warehouse')

        dates = sorted({l.date for l in lines})
        quantities = {}
        date_start = None
        for date in dates:
            context = {
                'stock_date_start': date_start,
                'stock_date_end': date,
                'forecast': True,
                }
            with Transaction().set_context(**context):
                quantities[date] = Product.products_by_location(
                    [warehouse_id],
                    grouping=grouping,
                    grouping_filter=grouping_filter,
                    with_childs=True).get((warehouse_id, key), 0)
            try:
                date_start = date + datetime.timedelta(1)
            except OverflowError:
                pass
        cumulate = 0
        for date in dates:
            cumulate += quantities[date]
            quantities[date] = cumulate

        return dict((l.id, quantities[l.date]) for l in lines)


class ProductQuantitiesByWarehouseContext(ModelView):
    'Product Quantities By Warehouse'
    __name__ = 'stock.product_quantities_warehouse.context'
    warehouse = fields.Many2One('stock.location', 'Warehouse', required=True,
        domain=[
            ('type', '=', 'warehouse'),
            ])

    @staticmethod
    def default_warehouse():
        Location = Pool().get('stock.location')
        warehouses = Location.search([
                ('type', '=', 'warehouse'),
                ])
        if len(warehouses) == 1:
            return warehouses[0].id


class RecomputeCostPrice(Wizard):
    'Recompute Cost Price'
    __name__ = 'product.recompute_cost_price'
    start_state = 'recompute'
    recompute = StateTransition()

    def transition_recompute(self):
        pool = Pool()
        Product = pool.get('product.product')
        Template = pool.get('product.template')

        context = Transaction().context

        if context['active_model'] == 'product.product':
            products = Product.browse(context['active_ids'])
            Product.recompute_cost_price(products)
        elif context['active_model'] == 'product.template':
            templates = Template.browse(context['active_ids'])
            Template.recompute_cost_price(templates)
        return 'end'
