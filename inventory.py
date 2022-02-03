# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
from sql import Null

from trytond.model import Workflow, Model, ModelView, ModelSQL, fields, Check
from trytond.pyson import Eval, Bool, If
from trytond.transaction import Transaction
from trytond.pool import Pool
from trytond.wizard import Wizard, StateView, StateTransition, Button

__all__ = ['Inventory', 'InventoryLine',
    'Count', 'CountSearch', 'CountQuantity']

STATES = {
    'readonly': Eval('state') != 'draft',
}
DEPENDS = ['state']
INVENTORY_STATES = [
    ('draft', 'Draft'),
    ('done', 'Done'),
    ('cancel', 'Canceled'),
    ]


class Inventory(Workflow, ModelSQL, ModelView):
    'Stock Inventory'
    __name__ = 'stock.inventory'
    _rec_name = 'number'
    number = fields.Char('Number', readonly=True)
    location = fields.Many2One(
        'stock.location', 'Location', required=True,
        domain=[('type', '=', 'storage')], states={
            'readonly': (Eval('state') != 'draft') | Eval('lines', [0]),
            },
        depends=['state'])
    date = fields.Date('Date', required=True, states={
            'readonly': (Eval('state') != 'draft') | Eval('lines', [0]),
            },
        depends=['state'])
    lost_found = fields.Many2One(
        'stock.location', 'Lost and Found', required=True,
        domain=[('type', '=', 'lost_found')], states=STATES, depends=DEPENDS)
    lines = fields.One2Many(
        'stock.inventory.line', 'inventory', 'Lines',
        states={
            'readonly': (STATES['readonly'] | ~Eval('location')
                | ~Eval('date')),
            },
        depends=['location', 'date'] + DEPENDS)
    empty_quantity = fields.Selection([
            (None, ""),
            ('keep', "Keep"),
            ('empty', "Empty"),
            ], "Empty Quantity", states=STATES, depends=DEPENDS,
        help="How lines without quantity are handled.")
    company = fields.Many2One('company.company', 'Company', required=True,
        states={
            'readonly': (Eval('state') != 'draft') | Eval('lines', [0]),
            },
        depends=['state'])
    state = fields.Selection(
        INVENTORY_STATES, 'State', readonly=True, select=True)

    @classmethod
    def __setup__(cls):
        super(Inventory, cls).__setup__()
        cls.create_date.select = True
        cls._order.insert(0, ('date', 'DESC'))
        cls._error_messages.update({
                'delete_cancel': ('Inventory "%s" must be canceled before '
                    'deletion.'),
                'unique_line': ('Line "%s" is not unique '
                    'on Inventory "%s".'),
                })
        cls._transitions |= set((
                ('draft', 'done'),
                ('draft', 'cancel'),
                ))
        cls._buttons.update({
                'confirm': {
                    'invisible': Eval('state').in_(['done', 'cancel']),
                    'depends': ['state'],
                    },
                'cancel': {
                    'invisible': Eval('state').in_(['cancel', 'done']),
                    'depends': ['state'],
                    },
                'complete_lines': {
                    'readonly': Eval('state') != 'draft',
                    'depends': ['state'],
                    },
                'count': {
                    'readonly': Eval('state') != 'draft',
                    'depends': ['state'],
                    },
                })

    @classmethod
    def __register__(cls, module_name):
        super(Inventory, cls).__register__(module_name)

        table = cls.__table_handler__(module_name)

    @staticmethod
    def default_state():
        return 'draft'

    @staticmethod
    def default_date():
        Date = Pool().get('ir.date')
        return Date.today()

    @staticmethod
    def default_company():
        return Transaction().context.get('company')

    @classmethod
    def default_lost_found(cls):
        Location = Pool().get('stock.location')
        locations = Location.search(cls.lost_found.domain)
        if len(locations) == 1:
            return locations[0].id

    @classmethod
    def delete(cls, inventories):
        # Cancel before delete
        cls.cancel(inventories)
        for inventory in inventories:
            if inventory.state != 'cancel':
                cls.raise_user_error('delete_cancel', inventory.rec_name)
        super(Inventory, cls).delete(inventories)

    @classmethod
    @ModelView.button
    @Workflow.transition('done')
    def confirm(cls, inventories):
        Move = Pool().get('stock.move')
        moves = []
        for inventory in inventories:
            keys = set()
            for line in inventory.lines:
                key = line.unique_key
                if key in keys:
                    cls.raise_user_error('unique_line',
                        (line.rec_name, inventory.rec_name))
                keys.add(key)
                move = line.get_move()
                if move:
                    moves.append(move)
        if moves:
            Move.save(moves)
            Move.do(moves)

    @classmethod
    @ModelView.button
    @Workflow.transition('cancel')
    def cancel(cls, inventories):
        Line = Pool().get("stock.inventory.line")
        Line.cancel_move([l for i in inventories for l in i.lines])

    @classmethod
    def create(cls, vlist):
        pool = Pool()
        Sequence = pool.get('ir.sequence')
        Configuration = pool.get('stock.configuration')
        config = Configuration(1)
        vlist = [x.copy() for x in vlist]
        for values in vlist:
            if values.get('number') is None:
                values['number'] = Sequence.get_id(
                    config.inventory_sequence.id)
        inventories = super(Inventory, cls).create(vlist)
        cls.complete_lines(inventories, fill=False)
        return inventories

    @classmethod
    def write(cls, *args):
        super().write(*args)
        inventories = cls.browse(set(sum(args[::2], [])))
        cls.complete_lines(inventories, fill=False)

    @classmethod
    def copy(cls, inventories, default=None):
        pool = Pool()
        Date = pool.get('ir.date')

        if default is None:
            default = {}
        else:
            default = default.copy()
        default.setdefault('date', Date.today())
        default.setdefault('lines.moves', None)
        default.setdefault('number', None)

        new_inventories = super().copy(inventories, default=default)
        cls.complete_lines(new_inventories, fill=False)
        return new_inventories

    @staticmethod
    def grouping():
        return ('product',)

    @classmethod
    @ModelView.button
    def complete_lines(cls, inventories, fill=True):
        '''
        Complete or update the inventories
        '''
        pool = Pool()
        Line = pool.get('stock.inventory.line')
        Product = pool.get('product.product')

        grouping = cls.grouping()
        to_create, to_write = [], []
        for inventory in inventories:
            # Once done computation is wrong because include created moves
            if inventory.state == 'done':
                continue
            # Compute product quantities
            if fill:
                product_ids = None
            else:
                product_ids = [l.product.id for l in inventory.lines]
            with Transaction().set_context(stock_date_end=inventory.date):
                pbl = Product.products_by_location(
                    [inventory.location.id],
                    grouping=grouping,
                    grouping_filter=(product_ids,))

            # Index some data
            product2type = {}
            product2consumable = {}
            for product in Product.browse([line[1] for line in pbl]):
                product2type[product.id] = product.type
                product2consumable[product.id] = product.consumable

            # Update existing lines
            for line in inventory.lines:
                if not (line.product.type == 'goods'
                        and not line.product.consumable):
                    Line.delete([line])
                    continue

                key = (inventory.location.id,) + line.unique_key
                if key in pbl:
                    quantity = pbl.pop(key)
                else:
                    quantity = 0.0
                values = line.update_values4complete(quantity)
                if values:
                    to_write.extend(([line], values))

            if not fill:
                continue
            # Create lines if needed
            for key, quantity in pbl.items():
                product_id = key[grouping.index('product') + 1]
                if (product2type[product_id] != 'goods'
                        or product2consumable[product_id]):
                    continue
                if not quantity:
                    continue

                values = Line.create_values4complete(inventory, quantity)
                for i, fname in enumerate(grouping, 1):
                    values[fname] = key[i]
                to_create.append(values)
        if to_create:
            Line.create(to_create)
        if to_write:
            Line.write(*to_write)

    @classmethod
    @ModelView.button_action('stock.wizard_inventory_count')
    def count(cls, inventories):
        cls.complete_lines(inventories)


class InventoryLine(ModelSQL, ModelView):
    'Stock Inventory Line'
    __name__ = 'stock.inventory.line'
    _states = {
        'readonly': Eval('inventory_state') != 'draft',
        }
    _depends = ['inventory_state']

    product = fields.Many2One('product.product', 'Product', required=True,
        domain=[
            ('type', '=', 'goods'),
            ('consumable', '=', False),
            ], states=_states, depends=_depends)
    uom = fields.Function(fields.Many2One('product.uom', 'UOM'), 'get_uom')
    unit_digits = fields.Function(fields.Integer('Unit Digits'),
            'get_unit_digits')
    expected_quantity = fields.Float('Expected Quantity', required=True,
        digits=(16, Eval('unit_digits', 2)), readonly=True,
        states={
            'invisible': Eval('id', -1) < 0,
        },
        depends=['unit_digits'])
    quantity = fields.Float('Quantity',
        digits=(16, Eval('unit_digits', 2)),
        states=_states, depends=['unit_digits'] + _depends)
    moves = fields.One2Many('stock.move', 'origin', 'Moves', readonly=True)
    inventory = fields.Many2One('stock.inventory', 'Inventory', required=True,
        ondelete='CASCADE',
        states={
            'readonly': _states['readonly'] & Bool(Eval('inventory')),
            },
        depends=_depends)
    inventory_state = fields.Function(
        fields.Selection(INVENTORY_STATES, 'Inventory State',
        depends=['inventory']),
        'on_change_with_inventory_state')

    @classmethod
    def __setup__(cls):
        super(InventoryLine, cls).__setup__()
        t = cls.__table__()
        cls._sql_constraints += [
            ('check_line_qty_pos', Check(t, t.quantity >= 0),
                'Line quantity must be positive.'),
            ]
        cls._order.insert(0, ('product', 'ASC'))
        cls._error_messages.update({
                'missing_empty_quantity': ('An option for empty quantity is '
                    'missing for inventory "%(inventory)s".'),
                'delete_cancel_draft': ('The line "%(line)s" must be on '
                    'canceled or draft inventory to be deleted.'),
                })

    @classmethod
    def __register__(cls, module_name):
        cursor = Transaction().connection.cursor()
        pool = Pool()
        Move = pool.get('stock.move')
        sql_table = cls.__table__()
        move_table = Move.__table__()

        super(InventoryLine, cls).__register__(module_name)

        table = cls.__table_handler__(module_name)

        # Migration from 3.0: use Move origin
        if table.column_exist('move'):
            cursor.execute(*sql_table.select(sql_table.id, sql_table.move,
                    where=sql_table.move != Null))
            for line_id, move_id in cursor.fetchall():
                cursor.execute(*move_table.update(
                        columns=[move_table.origin],
                        values=['%s,%s' % (cls.__name__, line_id)],
                        where=move_table.id == move_id))
            table.drop_column('move')

        # Migration from 4.6: drop required on quantity
        table.not_null_action('quantity', action='remove')

    @staticmethod
    def default_unit_digits():
        return 2

    @staticmethod
    def default_expected_quantity():
        return 0.

    @fields.depends('product')
    def on_change_product(self):
        self.unit_digits = 2
        if self.product:
            self.uom = self.product.default_uom
            self.unit_digits = self.product.default_uom.digits

    @fields.depends('inventory', '_parent_inventory.state')
    def on_change_with_inventory_state(self, name=None):
        if self.inventory:
            return self.inventory.state
        return 'draft'

    def get_rec_name(self, name):
        return self.product.rec_name

    @classmethod
    def search_rec_name(cls, name, clause):
        return [('product.rec_name',) + tuple(clause[1:])]

    def get_uom(self, name):
        return self.product.default_uom.id

    def get_unit_digits(self, name):
        return self.product.default_uom.digits

    @property
    def unique_key(self):
        key = []
        for fname in self.inventory.grouping():
            value = getattr(self, fname)
            if isinstance(value, Model):
                value = value.id
            key.append(value)
        return tuple(key)

    @classmethod
    def cancel_move(cls, lines):
        Move = Pool().get('stock.move')
        moves = [m for l in lines for m in l.moves if l.moves]
        Move.cancel(moves)
        Move.delete(moves)

    def get_move(self):
        '''
        Return Move instance for the inventory line
        '''
        pool = Pool()
        Move = pool.get('stock.move')
        Uom = pool.get('product.uom')

        qty = self.quantity
        if qty is None:
            if self.inventory.empty_quantity is None:
                self.raise_user_error('missing_empty_quantity', {
                        'inventory': self.inventory.rec_name,
                        })
            if self.inventory.empty_quantity == 'keep':
                return
            else:
                qty = 0.0

        delta_qty = Uom.compute_qty(self.uom,
            self.expected_quantity - qty,
            self.uom)
        if delta_qty == 0.0:
            return
        from_location = self.inventory.location
        to_location = self.inventory.lost_found
        if delta_qty < 0:
            (from_location, to_location, delta_qty) = \
                (to_location, from_location, -delta_qty)

        return Move(
            from_location=from_location,
            to_location=to_location,
            quantity=delta_qty,
            product=self.product,
            uom=self.uom,
            company=self.inventory.company,
            effective_date=self.inventory.date,
            origin=self,
            )

    def update_values4complete(self, quantity):
        '''
        Return update values to complete inventory
        '''
        values = {}
        # if nothing changed, no update
        if self.quantity == self.expected_quantity == quantity:
            return values
        values['expected_quantity'] = quantity
        return values

    @classmethod
    def create_values4complete(cls, inventory, quantity):
        '''
        Return create values to complete inventory
        '''
        return {
            'inventory': inventory.id,
            'expected_quantity': quantity,
        }

    @classmethod
    def delete(cls, lines):
        for line in lines:
            if line.inventory_state not in {'cancel', 'draft'}:
                cls.raise_user_error('delete_cancel_draft', {
                        'line': line.rec_name,
                        })
        super(InventoryLine, cls).delete(lines)


class Count(Wizard):
    "Stock Inventory Count"
    __name__ = 'stock.inventory.count'
    start_state = 'search'

    search = StateView(
        'stock.inventory.count.search',
        'stock.inventory_count_search_view_form', [
            Button("End", 'end', 'tryton-cancel'),
            Button("Select", 'quantity', 'tryton-forward', default=True),
            ])
    quantity = StateView(
        'stock.inventory.count.quantity',
        'stock.inventory_count_quantity_view_form', [
            Button("Cancel", 'search', 'tryton-cancel'),
            Button("Add", 'add', 'tryton-ok', default=True),
            ])
    add = StateTransition()

    @classmethod
    def __setup__(cls):
        super(Count, cls).__setup__()
        cls._error_messages.update({
                'create_line': "No existing line found for %(search)s.",
                })

    def default_quantity(self, fields):
        pool = Pool()
        Inventory = pool.get('stock.inventory')
        InventoryLine = pool.get('stock.inventory.line')
        context = Transaction().context
        inventory = Inventory(context['active_id'])
        values = {}
        lines = InventoryLine.search(self.get_line_domain(inventory), limit=1)
        if not lines:
            warning_name = '%s.%s.count_create' % (
                inventory, self.search.search)
            self.raise_user_warning(warning_name, 'create_line', {
                    'search': self.search.search.rec_name,
                    })
            line, = InventoryLine.create([self.get_line_values(inventory)])
        else:
            line, = lines
        values['line'] = line.id
        values['product'] = line.product.id
        values['uom'] = line.uom.id
        values['unit_digits'] = line.unit_digits
        if line.uom.rounding == 1:
            values['quantity_added'] = 1
        return values

    def get_line_domain(self, inventory):
        pool = Pool()
        Product = pool.get('product.product')
        domain = [
            ('inventory', '=', inventory.id),
            ]
        if isinstance(self.search.search, Product):
            domain.append(('product', '=', self.search.search.id))
        return domain

    def get_line_values(self, inventory):
        pool = Pool()
        Product = pool.get('product.product')
        InventoryLine = pool.get('stock.inventory.line')
        values = InventoryLine.create_values4complete(inventory, 0)
        if isinstance(self.search.search, Product):
            values['product'] = self.search.search.id
        return values

    def transition_add(self):
        if self.quantity.line and self.quantity.quantity_added:
            line = self.quantity.line
            if line.quantity:
                line.quantity += self.quantity.quantity_added
            else:
                line.quantity = self.quantity.quantity_added
            line.save()
        return 'search'


class CountSearch(ModelView):
    "Stock Inventory Count"
    __name__ = 'stock.inventory.count.search'

    search = fields.Reference(
        "Search", [
            ('product.product', "Product"),
            ],
        required=True,
        domain=[If(Eval('search_model') == 'product.product',
                [
                    ('type', '=', 'goods'),
                    ('consumable', '=', False),
                    ],
                [])],
        depends=['search_model'])
    search_model = fields.Function(fields.Selection(
        'get_search_models', "Search Model"),
        'on_change_with_search_model')

    @classmethod
    def default_search(cls):
        return 'product.product,-1'

    @classmethod
    def get_search_models(cls):
        return cls.fields_get(['search'])['search']['selection']

    @fields.depends('search')
    def on_change_with_search_model(self, name=None):
        if self.search:
            return self.search.__name__


class CountQuantity(ModelView):
    "Stock Inventory Count"
    __name__ = 'stock.inventory.count.quantity'

    line = fields.Many2One(
        'stock.inventory.line', "Line", readonly=True, required=True)
    product = fields.Many2One('product.product', "Product", readonly=True)
    uom = fields.Many2One('product.uom', "UOM", readonly=True)
    quantity_resulting = fields.Float(
        "Resulting Quantity", digits=(16, Eval('unit_digits', 2)),
        readonly=True, depends=['unit_digits'])

    quantity_added = fields.Float(
        "Added Quantity", digits=(16, Eval('unit_digits', 2)), required=True,
        depends=['unit_digits'])

    unit_digits = fields.Integer("Unit Digits", readonly=True)

    @fields.depends('quantity_added', 'line')
    def on_change_quantity_added(self):
        if self.line:
            self.quantity_resulting = (
                (self.line.quantity or 0) + (self.quantity_added or 0))
