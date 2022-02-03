# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
import functools
import datetime
from collections import defaultdict
from functools import partial

from sql import Null
from sql.conditionals import Coalesce

from trytond.i18n import gettext
from trytond.model import Workflow, ModelView, ModelSQL, fields, dualmethod
from trytond.model.exceptions import AccessError
from trytond.modules.company import CompanyReport
from trytond.wizard import Wizard, StateTransition, StateView, Button
from trytond.pyson import Eval, If, Id, Bool
from trytond.transaction import Transaction
from trytond.pool import Pool

from trytond.modules.company.model import employee_field, set_employee


class ShipmentMixin:
    __slots__ = ()

    @classmethod
    def view_attributes(cls):
        return super().view_attributes() + [
            ('/tree', 'visual', If(Eval('state') == 'cancelled', 'muted', '')),
            ]


class ShipmentAssignMixin(ShipmentMixin):
    __slots__ = ()
    _assign_moves_field = None

    partially_assigned = fields.Function(
        fields.Boolean("Partially Assigned"),
        'get_partially_assigned',
        searcher='search_partially_assigned')

    @classmethod
    def assign_wizard(cls, shipments):
        raise NotImplementedError

    @property
    def assign_moves(self):
        return getattr(self, self._assign_moves_field)

    @dualmethod
    @ModelView.button
    def assign_try(cls, shipments):
        raise NotImplementedError

    @dualmethod
    def assign_reset(cls, shipments):
        cls.wait(shipments)

    @dualmethod
    @ModelView.button
    def assign_force(cls, shipments):
        cls.assign(shipments)

    @dualmethod
    def assign_ignore(cls, shipments):
        pool = Pool()
        Move = pool.get('stock.move')
        Move.write([
                m for s in shipments for m in s.assign_moves
                if m.assignation_required
                and m.state in {'staging', 'draft'}], {
                'quantity': 0,
                })
        cls.assign(shipments)

    @classmethod
    def _get_assign_domain(cls):
        pool = Pool()
        Date = pool.get('ir.date')
        context = Transaction().context
        return [
            ('company', '=', context.get('company')),
            ('state', '=', 'waiting'),
            ('planned_date', '=', Date.today()),
            ]

    @classmethod
    def assign_cron(cls):
        shipments = cls.search(cls._get_assign_domain())
        cls.assign_try(shipments)

    def get_partially_assigned(self, name):
        return (self.state != 'assigned'
            and any(m.state == 'assigned' for m in self.assign_moves
                if m.assignation_required))

    @classmethod
    def search_partially_assigned(cls, name, clause):
        operators = {
            '=': 'where',
            '!=': 'not where',
            }
        reverse = {
            '=': '!=',
            '!=': '=',
            }
        if clause[1] in operators:
            if not clause[2]:
                operator = reverse[clause[1]]
            else:
                operator = clause[1]
            return [
                (cls._assign_moves_field, operators[operator], [
                        ('state', '=', 'assigned'),
                        ('assignation_required', '=', True),
                        ]),
                ('state', '!=', 'assigned'),
                ]
        else:
            return []


class ShipmentIn(ShipmentMixin, Workflow, ModelSQL, ModelView):
    "Supplier Shipment"
    __name__ = 'stock.shipment.in'
    _rec_name = 'number'
    effective_date = fields.Date('Effective Date',
        states={
            'readonly': Eval('state').in_(['cancelled', 'done']),
            },
        depends=['state'],
        help="When the stock was actually received.")
    planned_date = fields.Date('Planned Date', states={
            'readonly': Eval('state') != 'draft',
            }, depends=['state'],
        help="When the stock is expected to be received.")
    company = fields.Many2One(
        'company.company', "Company", required=True,
        states={
            'readonly': Eval('state') != 'draft',
            },
        depends=['state'],
        help="The company the shipment is associated with.")
    reference = fields.Char("Reference", size=None, select=True,
        states={
            'readonly': Eval('state') != 'draft',
            }, depends=['state'],
        help="The supplier's identifier for the shipment.")
    supplier = fields.Many2One('party.party', 'Supplier',
        states={
            'readonly': (((Eval('state') != 'draft')
                    | Eval('incoming_moves', [0]))
                & Eval('supplier')),
            }, required=True,
        context={
            'company': Eval('company', -1),
            },
        depends=['state', 'supplier', 'company'],
        help="The party that supplied the stock.")
    supplier_location = fields.Function(fields.Many2One('stock.location',
            'Supplier Location'),
        'on_change_with_supplier_location')
    contact_address = fields.Many2One('party.address', 'Contact Address',
        states={
            'readonly': Eval('state') != 'draft',
            }, domain=[('party', '=', Eval('supplier'))],
        depends=['state', 'supplier'],
        help="The address at which the supplier can be contacted.")
    warehouse = fields.Many2One('stock.location', "Warehouse",
        required=True, domain=[('type', '=', 'warehouse')],
        states={
            'readonly': (Eval('state').in_(['cancelled', 'done'])
                | Eval('incoming_moves', [0]) | Eval('inventory_moves', [0])),
            }, depends=['state'],
        help="Where the stock is received.")
    warehouse_input = fields.Function(fields.Many2One('stock.location',
            'Warehouse Input'),
        'on_change_with_warehouse_input')
    warehouse_storage = fields.Function(fields.Many2One('stock.location',
            'Warehouse Storage'),
        'on_change_with_warehouse_storage')
    incoming_moves = fields.Function(fields.One2Many('stock.move', 'shipment',
            'Incoming Moves',
            add_remove=[
                ('shipment', '=', None),
                ('from_location', '=', Eval('supplier_location')),
                ('state', '=', 'draft'),
                If(Eval('warehouse_input') == Eval('warehouse_storage'),
                    ('to_location', 'child_of',
                        [Eval('warehouse_input', -1)], 'parent'),
                    ('to_location', '=', Eval('warehouse_input'))),
                ],
            domain=[
                ('from_location', '=', Eval('supplier_location')),
                If(Eval('warehouse_input') == Eval('warehouse_storage'),
                    ('to_location', 'child_of',
                        [Eval('warehouse_input', -1)], 'parent'),
                    ('to_location', '=', Eval('warehouse_input'))),
                ('company', '=', Eval('company')),
                ],
            states={
                'readonly': (
                    Eval('state').in_(['received', 'done', 'cancelled'])
                    | ~Eval('warehouse') | ~Eval('supplier')),
                },
            depends=['state', 'warehouse', 'supplier_location',
                'warehouse_input', 'warehouse_storage', 'company'],
            help="The moves that bring the stock into the warehouse."),
        'get_incoming_moves', setter='set_incoming_moves')
    inventory_moves = fields.Function(fields.One2Many('stock.move', 'shipment',
            'Inventory Moves',
            domain=[
                ('from_location', '=', Eval('warehouse_input')),
                If(~Eval('state').in_(['done', 'cancelled']),
                    ['OR',
                        ('to_location', 'child_of',
                            [Eval('warehouse_storage', -1)], 'parent'),
                        ('to_location.waste_warehouses', '=',
                            Eval('warehouse')),
                        ],
                    [],),
                ('company', '=', Eval('company')),
                ],
            states={
                'readonly': Eval('state').in_(['draft', 'done', 'cancelled']),
                'invisible': (
                    Eval('warehouse_input') == Eval('warehouse_storage')),
                },
            depends=['state', 'warehouse', 'warehouse_input',
                'warehouse_storage', 'company'],
            help="The moves that put the stock away into the storage area."),
        'get_inventory_moves', setter='set_inventory_moves')
    moves = fields.One2Many('stock.move', 'shipment', 'Moves',
        domain=[('company', '=', Eval('company'))], readonly=True,
        depends=['company'])
    origins = fields.Function(fields.Char('Origins'), 'get_origins')
    number = fields.Char('Number', size=None, select=True, readonly=True,
        help="The main identifier for the shipment.")
    received_by = employee_field("Received By")
    done_by = employee_field("Done By")
    state = fields.Selection([
        ('draft', 'Draft'),
        ('done', 'Done'),
        ('cancelled', 'Cancelled'),
        ('received', 'Received'),
        ], 'State', readonly=True,
        help="The current state of the shipment.")

    @classmethod
    def __setup__(cls):
        super(ShipmentIn, cls).__setup__()
        cls.create_date.select = True
        cls._order = [
            ('id', 'DESC'),
            ]
        cls._transitions |= set((
                ('draft', 'received'),
                ('received', 'done'),
                ('draft', 'cancelled'),
                ('received', 'cancelled'),
                ('cancelled', 'draft'),
                ))
        cls._buttons.update({
                'cancel': {
                    'invisible': Eval('state').in_(['cancelled', 'done']),
                    'depends': ['state'],
                    },
                'draft': {
                    'invisible': Eval('state') != 'cancelled',
                    'depends': ['state'],
                    },
                'receive': {
                    'invisible': Eval('state') != 'draft',
                    'depends': ['state'],
                    },
                'done': {
                    'invisible': Eval('state') != 'received',
                    'depends': ['state'],
                    },
                })

    @classmethod
    def __register__(cls, module_name):
        cursor = Transaction().connection.cursor()
        table = cls.__table_handler__(module_name)
        sql_table = cls.__table__()

        # Migration from 3.8: rename code into number
        if table.column_exist('code'):
            table.column_rename('code', 'number')

        super(ShipmentIn, cls).__register__(module_name)

        # Migration from 5.6: rename state cancel to cancelled
        cursor.execute(*sql_table.update(
                [sql_table.state], ['cancelled'],
                where=sql_table.state == 'cancel'))

    @classmethod
    def order_effective_date(cls, tables):
        table, _ = tables[None]
        return [Coalesce(table.effective_date, table.planned_date)]

    @staticmethod
    def default_planned_date():
        return Pool().get('ir.date').today()

    @staticmethod
    def default_state():
        return 'draft'

    @classmethod
    def default_warehouse(cls):
        Location = Pool().get('stock.location')
        return Location.get_default_warehouse()

    @staticmethod
    def default_company():
        return Transaction().context.get('company')

    @fields.depends('supplier')
    def on_change_supplier(self):
        self.contact_address = None
        if self.supplier:
            self.contact_address = self.supplier.address_get()

    @fields.depends('supplier')
    def on_change_with_supplier_location(self, name=None):
        if self.supplier and self.supplier.supplier_location:
            return self.supplier.supplier_location.id

    @classmethod
    def default_warehouse_input(cls):
        warehouse = cls.default_warehouse()
        if warehouse:
            return cls(warehouse=warehouse).on_change_with_warehouse_input()

    @fields.depends('warehouse')
    def on_change_with_warehouse_input(self, name=None):
        if self.warehouse:
            return self.warehouse.input_location.id

    @classmethod
    def default_warehouse_storage(cls):
        warehouse = cls.default_warehouse()
        if warehouse:
            return cls(warehouse=warehouse).on_change_with_warehouse_storage()

    @fields.depends('warehouse')
    def on_change_with_warehouse_storage(self, name=None):
        if self.warehouse:
            return self.warehouse.storage_location.id

    def get_incoming_moves(self, name):
        moves = []
        if self.warehouse_input == self.warehouse_storage:
            return [m.id for m in self.moves]
        for move in self.moves:
            if move.to_location == self.warehouse_input:
                moves.append(move.id)
        return moves

    @classmethod
    def set_incoming_moves(cls, shipments, name, value):
        if not value:
            return
        cls.write(shipments, {
                'moves': value,
                })

    def get_inventory_moves(self, name):
        moves = []
        for move in self.moves:
            if move.from_location == self.warehouse_input:
                moves.append(move.id)
        return moves

    @classmethod
    def set_inventory_moves(cls, shipments, name, value):
        if not value:
            return
        cls.write(shipments, {
                'moves': value,
                })

    @property
    def _move_planned_date(self):
        '''
        Return the planned date for incoming moves and inventory_moves
        '''
        return self.planned_date, self.planned_date

    @classmethod
    def _set_move_planned_date(cls, shipments):
        '''
        Set planned date of moves for the shipments
        '''
        Move = Pool().get('stock.move')
        to_write = []
        for shipment in shipments:
            dates = shipment._move_planned_date
            incoming_date, inventory_date = dates
            # Update planned_date only for later to not be too optimistic if
            # the shipment is not directly received.
            incoming_moves_to_write = [m for m in shipment.incoming_moves
                if (m.state not in ('assigned', 'done', 'cancelled')
                    and ((m.planned_date or datetime.date.max)
                        < (incoming_date or datetime.date.max)))]
            if incoming_moves_to_write:
                to_write.extend((incoming_moves_to_write, {
                            'planned_date': incoming_date,
                            }))
            inventory_moves_to_write = [m for m in shipment.inventory_moves
                if (m.state not in ('assigned', 'done', 'cancelled')
                    and ((m.planned_date or datetime.date.max)
                        < (inventory_date or datetime.date.max)))]
            if inventory_moves_to_write:
                to_write.extend((inventory_moves_to_write, {
                            'planned_date': inventory_date,
                            }))
        if to_write:
            Move.write(*to_write)

    def get_origins(self, name):
        return ', '.join(set(filter(None,
                    (m.origin_name for m in self.incoming_moves))))

    @classmethod
    def create(cls, vlist):
        pool = Pool()
        Config = pool.get('stock.configuration')

        vlist = [x.copy() for x in vlist]
        config = Config(1)
        default_company = cls.default_company()
        for values in vlist:
            if values.get('number') is None:
                values['number'] = config.get_multivalue(
                    'shipment_in_sequence',
                    company=values.get('company', default_company)).get()
        shipments = super(ShipmentIn, cls).create(vlist)
        cls._set_move_planned_date(shipments)
        return shipments

    @classmethod
    def write(cls, *args):
        super(ShipmentIn, cls).write(*args)
        cls._set_move_planned_date(sum(args[::2], []))

    @classmethod
    def copy(cls, shipments, default=None):
        if default is None:
            default = {}
        else:
            default = default.copy()
        default.setdefault('inventory_moves', None)
        default.setdefault('incoming_moves', None)
        default.setdefault('number', None)
        default.setdefault('received_by', None)
        default.setdefault('done_by', None)
        return super(ShipmentIn, cls).copy(shipments, default=default)

    def _get_inventory_move(self, incoming_move):
        'Return inventory move for the incoming move'
        pool = Pool()
        Move = pool.get('stock.move')
        Date = pool.get('ir.date')
        if incoming_move.quantity <= 0.0:
            return None
        move = Move()
        move.product = incoming_move.product
        move.uom = incoming_move.uom
        move.quantity = incoming_move.quantity
        move.from_location = incoming_move.to_location
        move.to_location = self.warehouse_storage
        move.state = Move.default_state()
        move.planned_date = max(
            filter(None, [self._move_planned_date[1], Date.today()]))
        move.company = incoming_move.company
        move.origin = incoming_move
        return move

    @classmethod
    def create_inventory_moves(cls, shipments):
        for shipment in shipments:
            if shipment.warehouse_storage == shipment.warehouse_input:
                # Do not create inventory moves
                continue
            # Use moves instead of inventory_moves because save reset before
            # adding new records and as set_inventory_moves is just a proxy to
            # moves, it will reset also the incoming_moves
            moves = list(shipment.moves)
            for incoming_move in shipment.incoming_moves:
                move = shipment._get_inventory_move(incoming_move)
                if move:
                    moves.append(move)
            shipment.moves = moves
        cls.save(shipments)

    @classmethod
    def delete(cls, shipments):
        Move = Pool().get('stock.move')
        # Cancel before delete
        cls.cancel(shipments)
        for shipment in shipments:
            if shipment.state != 'cancelled':
                raise AccessError(
                    gettext('stock.msg_shipment_delete_cancel',
                        shipment=shipment.rec_name))
        Move.delete([m for s in shipments for m in s.moves])
        super(ShipmentIn, cls).delete(shipments)

    @classmethod
    @ModelView.button
    @Workflow.transition('cancelled')
    def cancel(cls, shipments):
        Move = Pool().get('stock.move')
        Move.cancel([m for s in shipments
                for m in s.incoming_moves + s.inventory_moves])

    @classmethod
    @ModelView.button
    @Workflow.transition('draft')
    def draft(cls, shipments):
        Move = Pool().get('stock.move')
        Move.draft([m for s in shipments for m in s.incoming_moves
                if m.state != 'staging'])
        Move.delete([m for s in shipments for m in s.inventory_moves
                if m.state in ('draft', 'cancelled')])

    @classmethod
    @ModelView.button
    @Workflow.transition('received')
    @set_employee('received_by')
    def receive(cls, shipments):
        Move = Pool().get('stock.move')
        Move.do([m for s in shipments for m in s.incoming_moves])
        Move.delete([m for s in shipments for m in s.inventory_moves
            if m.state in ('draft', 'cancelled')])
        cls.create_inventory_moves(shipments)
        # Set received state to allow done transition
        cls.write(shipments, {'state': 'received'})
        to_do = [s for s in shipments
            if s.warehouse_storage == s.warehouse_input]
        if to_do:
            cls.done(to_do)

    @classmethod
    @ModelView.button
    @Workflow.transition('done')
    @set_employee('done_by')
    def done(cls, shipments):
        pool = Pool()
        Move = pool.get('stock.move')
        Date = pool.get('ir.date')
        Move.do([m for s in shipments for m in s.inventory_moves])
        cls.write([s for s in shipments if not s.effective_date], {
                'effective_date': Date.today(),
                })


class ShipmentInReturn(ShipmentAssignMixin, Workflow, ModelSQL, ModelView):
    "Supplier Return Shipment"
    __name__ = 'stock.shipment.in.return'
    _rec_name = 'number'
    _assign_moves_field = 'moves'

    effective_date = fields.Date('Effective Date',
        states={
            'readonly': Eval('state').in_(['cancelled', 'done']),
            },
        depends=['state'],
        help="When the stock was actually returned.")
    planned_date = fields.Date('Planned Date',
        states={
            'readonly': Eval('state') != 'draft',
            }, depends=['state'],
        help="When the stock is expected to be returned.")
    company = fields.Many2One(
        'company.company', "Company", required=True,
        states={
            'readonly': Eval('state') != 'draft',
            },
        depends=['state'],
        help="The company the shipment is associated with.")
    number = fields.Char('Number', size=None, select=True, readonly=True,
        help="The main identifier for the shipment.")
    reference = fields.Char("Reference", size=None, select=True,
        states={
            'readonly': Eval('state') != 'draft',
            }, depends=['state'],
        help="The supplier's identifier for the shipment.")
    supplier = fields.Many2One('party.party', 'Supplier',
        states={
            'readonly': (((Eval('state') != 'draft')
                    | Eval('moves', [0]))
                    & Eval('supplier', 0)),
            }, required=True,
        context={
            'company': Eval('company', -1),
            },
        depends=['state', 'supplier', 'company'],
        help="The party that supplied the stock.")
    delivery_address = fields.Many2One('party.address', 'Delivery Address',
        states={
            'readonly': Eval('state') != 'draft',
            },
        domain=[
            ('party', '=', Eval('supplier'))
            ],
        depends=['state', 'supplier'],
        help="Where the stock is sent to.")
    from_location = fields.Many2One('stock.location', "From Location",
        required=True, states={
            'readonly': (Eval('state') != 'draft') | Eval('moves', [0]),
            }, domain=[('type', 'in', ['storage', 'view'])],
        depends=['state'],
        help="Where the stock is moved from.")
    to_location = fields.Many2One('stock.location', "To Location",
        required=True, states={
            'readonly': (Eval('state') != 'draft') | Eval('moves', [0]),
            }, domain=[('type', '=', 'supplier')],
        depends=['state'],
        help="Where the stock is moved to.")
    moves = fields.One2Many('stock.move', 'shipment', 'Moves',
        states={
            'readonly': (((Eval('state') != 'draft') | ~Eval('from_location'))
                & Eval('to_location')),
            },
        domain=[
            If(Eval('state') == 'draft', [
                    ('from_location', '=', Eval('from_location')),
                    ('to_location', '=', Eval('to_location')),
                    ],
                If(~Eval('state').in_(['done', 'cancelled']), [
                        ('from_location', 'child_of',
                            [Eval('from_location', -1)], 'parent'),
                        ('to_location', 'child_of',
                            [Eval('to_location', -1)], 'parent'),
                        ],
                    [])),
            ('company', '=', Eval('company')),
            ],
        depends=['state', 'from_location', 'to_location', 'company'],
        help="The moves that return the stock to the supplier.")
    origins = fields.Function(fields.Char('Origins'), 'get_origins')
    assigned_by = employee_field("Assigned By")
    done_by = employee_field("Done By")
    state = fields.Selection([
        ('draft', 'Draft'),
        ('cancelled', 'Cancelled'),
        ('assigned', 'Assigned'),
        ('waiting', 'Waiting'),
        ('done', 'Done'),
        ], 'State', readonly=True,
        help="The current state of the shipment.")

    @classmethod
    def __setup__(cls):
        super(ShipmentInReturn, cls).__setup__()
        cls.create_date.select = True
        cls._order = [
            ('effective_date', 'ASC NULLS LAST'),
            ('id', 'ASC'),
            ]
        cls._transitions |= set((
                ('draft', 'waiting'),
                ('waiting', 'assigned'),
                ('waiting', 'draft'),
                ('assigned', 'done'),
                ('assigned', 'waiting'),
                ('draft', 'cancelled'),
                ('waiting', 'cancelled'),
                ('assigned', 'cancelled'),
                ('cancelled', 'draft'),
                ))
        cls._buttons.update({
                'cancel': {
                    'invisible': Eval('state').in_(['cancelled', 'done']),
                    'depends': ['state'],
                    },
                'draft': {
                    'invisible': ~Eval('state').in_(['waiting', 'cancelled']),
                    'icon': If(Eval('state') == 'cancelled',
                        'tryton-undo',
                        If(Eval('state') == 'waiting',
                            'tryton-back',
                            'tryton-forward')),
                    'depends': ['state'],
                    },
                'wait': {
                    'invisible': ~Eval('state').in_(['assigned', 'draft']),
                    'icon': If(Eval('state') == 'assigned',
                        'tryton-back', 'tryton-forward'),
                    'depends': ['state'],
                    },
                'done': {
                    'invisible': Eval('state') != 'assigned',
                    'depends': ['state'],
                    },
                'assign_wizard': {
                    'invisible': Eval('state') != 'waiting',
                    'depends': ['state'],
                    },
                'assign_try': {},
                'assign_force': {},
                })

    @classmethod
    def __register__(cls, module_name):
        cursor = Transaction().connection.cursor()
        table = cls.__table_handler__(module_name)
        sql_table = cls.__table__()

        # Migration from 3.8: rename code into number
        if table.column_exist('code'):
            table.column_rename('code', 'number')

        super(ShipmentInReturn, cls).__register__(module_name)

        # Migration from 5.6: rename state cancel to cancelled
        cursor.execute(*sql_table.update(
                [sql_table.state], ['cancelled'],
                where=sql_table.state == 'cancel'))

    @classmethod
    def order_effective_date(cls, tables):
        table, _ = tables[None]
        return [Coalesce(table.effective_date, table.planned_date)]

    @staticmethod
    def default_state():
        return 'draft'

    @staticmethod
    def default_company():
        return Transaction().context.get('company')

    @fields.depends('supplier')
    def on_change_supplier(self):
        if self.supplier:
            self.delivery_address = self.supplier.address_get('delivery')
            self.to_location = self.supplier.supplier_location

    @property
    def _move_planned_date(self):
        '''
        Return the planned date for the moves
        '''
        return self.planned_date

    @classmethod
    def _set_move_planned_date(cls, shipments):
        '''
        Set planned date of moves for the shipments
        '''
        Move = Pool().get('stock.move')
        to_write = []
        for shipment in shipments:
            moves = [m for m in shipment.moves
                    if (m.state not in ('assigned', 'done', 'cancelled')
                        and m.planned_date != shipment._move_planned_date)]
            if moves:
                to_write.extend((moves, {
                            'planned_date': shipment._move_planned_date,
                            }))
        if to_write:
            Move.write(*to_write)

    def get_origins(self, name):
        return ', '.join(set(filter(None,
                    (m.origin_name for m in self.moves))))

    @classmethod
    def create(cls, vlist):
        pool = Pool()
        Config = pool.get('stock.configuration')

        vlist = [x.copy() for x in vlist]
        config = Config(1)
        default_company = cls.default_company()
        for values in vlist:
            if values.get('number') is None:
                values['number'] = config.get_multivalue(
                    'shipment_in_return_sequence',
                    company=values.get('company', default_company)).get()
        shipments = super(ShipmentInReturn, cls).create(vlist)
        cls._set_move_planned_date(shipments)
        return shipments

    @classmethod
    def write(cls, *args):
        super(ShipmentInReturn, cls).write(*args)
        cls._set_move_planned_date(sum(args[::2], []))

    @classmethod
    def delete(cls, shipments):
        Move = Pool().get('stock.move')
        # Cancel before delete
        cls.cancel(shipments)
        for shipment in shipments:
            if shipment.state != 'cancelled':
                raise AccessError(
                    gettext('stock.msg_shipment_delete_cancel',
                        shipment=shipment.rec_name))
        Move.delete([m for s in shipments for m in s.moves])
        super(ShipmentInReturn, cls).delete(shipments)

    @classmethod
    def copy(cls, shipments, default=None):
        if default is None:
            default = {}
        else:
            default = default.copy()
        default.setdefault('number', None)
        default.setdefault('assigned_by', None)
        default.setdefault('done_by', None)
        return super(ShipmentInReturn, cls).copy(shipments, default=default)

    @classmethod
    @ModelView.button
    @Workflow.transition('draft')
    def draft(cls, shipments):
        Move = Pool().get('stock.move')
        Move.draft([m for s in shipments for m in s.moves
                if m.state != 'staging'])
        for shipment in shipments:
            Move.write([m for m in shipment.moves
                    if m.state != 'done'], {
                    'from_location': shipment.from_location.id,
                    'to_location': shipment.to_location.id,
                    'planned_date': shipment.planned_date,
                    })

    @classmethod
    @ModelView.button
    @Workflow.transition('waiting')
    def wait(cls, shipments, moves=None):
        """
        If moves is set, only this subset is set to draft.
        """
        Move = Pool().get('stock.move')
        if moves is None:
            moves = sum((s.moves for s in shipments), ())
        else:
            assert all(m.shipment in shipments for m in moves)
        Move.draft(moves)
        cls._set_move_planned_date(shipments)

    @classmethod
    @Workflow.transition('assigned')
    @set_employee('assigned_by')
    def assign(cls, shipments):
        Move = Pool().get('stock.move')
        Move.assign([m for s in shipments for m in s.assign_moves])

    @classmethod
    @ModelView.button
    @Workflow.transition('done')
    @set_employee('done_by')
    def done(cls, shipments):
        pool = Pool()
        Move = pool.get('stock.move')
        Date = pool.get('ir.date')

        Move.do([m for s in shipments for m in s.moves])
        cls.write([s for s in shipments if not s.effective_date], {
                'effective_date': Date.today(),
                })

    @classmethod
    @ModelView.button
    @Workflow.transition('cancelled')
    def cancel(cls, shipments):
        Move = Pool().get('stock.move')
        Move.cancel([m for s in shipments for m in s.moves])

    @classmethod
    @ModelView.button_action('stock.wizard_shipment_in_return_assign')
    def assign_wizard(cls, shipments):
        pass

    @dualmethod
    @ModelView.button
    def assign_try(cls, shipments, with_childs=None):
        pool = Pool()
        Move = pool.get('stock.move')
        to_assign = defaultdict(list)
        for shipment in shipments:
            location_type = shipment.from_location.type
            for move in shipment.assign_moves:
                if move.assignation_required:
                    to_assign[location_type].append(move)
        success = True
        for location_type, moves in to_assign.items():
            if with_childs is None:
                _with_childs = location_type == 'view'
            elif not with_childs and location_type == 'view':
                _with_childs = True
            else:
                _with_childs = with_childs
            if not Move.assign_try(moves, with_childs=_with_childs):
                success = False
        if success:
            cls.assign(shipments)
        return success

    @classmethod
    def _get_reschedule_domain(cls, date):
        return [
            ('state', '=', 'waiting'),
            ('planned_date', '<', date),
            ]

    @classmethod
    def reschedule(cls, date=None):
        pool = Pool()
        Date = pool.get('ir.date')
        if date is None:
            date = Date.today()
        shipments = cls.search(cls._get_reschedule_domain(date))
        cls.write(shipments, {'planned_date': date})


class ShipmentOut(ShipmentAssignMixin, Workflow, ModelSQL, ModelView):
    "Customer Shipment"
    __name__ = 'stock.shipment.out'
    _rec_name = 'number'
    _assign_moves_field = 'moves'
    effective_date = fields.Date('Effective Date',
        states={
            'readonly': Eval('state').in_(['cancelled', 'done']),
            },
        depends=['state'],
        help="When the stock was actually sent.")
    planned_date = fields.Date('Planned Date',
        states={
            'readonly': Eval('state') != 'draft',
            }, depends=['state'],
        help="When the stock is expected to be sent.")
    company = fields.Many2One(
        'company.company', "Company", required=True,
        states={
            'readonly': Eval('state') != 'draft',
            },
        depends=['state'],
        help="The company the shipment is associated with.")
    customer = fields.Many2One('party.party', 'Customer', required=True,
        states={
            'readonly': ((Eval('state') != 'draft')
                | Eval('outgoing_moves', [0])),
            },
        context={
            'company': Eval('company', -1),
            },
        depends=['state', 'company'],
        help="The party that purchased the stock.")
    customer_location = fields.Function(fields.Many2One('stock.location',
            'Customer Location'), 'on_change_with_customer_location')
    delivery_address = fields.Many2One('party.address',
        'Delivery Address', required=True,
        states={
            'readonly': Eval('state') != 'draft',
            }, domain=[('party', '=', Eval('customer'))],
        depends=['state', 'customer'],
        help="Where the stock is sent to.")
    reference = fields.Char("Reference", size=None, select=True,
        states={
            'readonly': Eval('state') != 'draft',
            }, depends=['state'],
        help="The customer's identifier for the shipment.")
    warehouse = fields.Many2One('stock.location', "Warehouse", required=True,
        states={
            'readonly': ((Eval('state') != 'draft')
                | Eval('outgoing_moves', [0]) | Eval('inventory_moves', [0])),
            }, domain=[('type', '=', 'warehouse')],
        depends=['state'],
        help="Where the stock is sent from.")
    warehouse_storage = fields.Function(fields.Many2One('stock.location',
            'Warehouse Storage'), 'on_change_with_warehouse_storage')
    warehouse_output = fields.Function(fields.Many2One('stock.location',
            'Warehouse Output'), 'on_change_with_warehouse_output')
    outgoing_moves = fields.Function(fields.One2Many('stock.move', 'shipment',
            'Outgoing Moves',
            domain=[
                ('from_location', '=', Eval('warehouse_output')),
                ('to_location', '=', Eval('customer_location')),
                ('company', '=', Eval('company')),
                ],
            states={
                'readonly': (Eval('state').in_(
                        If(Eval('warehouse_storage')
                            == Eval('warehouse_output'),
                            ['done', 'cancelled'],
                            ['waiting', 'packed', 'done', 'cancelled'],
                            ))
                    | ~Eval('warehouse') | ~Eval('customer')),
                },
            depends=['state', 'warehouse', 'customer', 'warehouse_output',
                'customer_location', 'company'],
            help="The moves that send the stock to the customer."),
        'get_outgoing_moves', setter='set_outgoing_moves')
    inventory_moves = fields.Function(fields.One2Many('stock.move', 'shipment',
            'Inventory Moves',
            domain=[
                If(Eval('state').in_(['waiting']),
                    ('from_location', 'child_of',
                        [Eval('warehouse_storage', -1)], 'parent'),
                    ()),
                ('to_location', '=', Eval('warehouse_output')),
                ('company', '=', Eval('company')),
                ],
            states={
                'readonly': Eval('state').in_(
                    ['draft', 'assigned', 'packed', 'done', 'cancelled']),
                'invisible': (
                    Eval('warehouse_storage') == Eval('warehouse_output')),
                },
            depends=['state', 'warehouse', 'warehouse_storage',
                'warehouse_output', 'company'],
            help="The moves that pick the stock from the storage area."),
        'get_inventory_moves', setter='set_inventory_moves')
    moves = fields.One2Many('stock.move', 'shipment', 'Moves',
        domain=[('company', '=', Eval('company'))], depends=['company'],
        readonly=True)
    origins = fields.Function(fields.Char('Origins'), 'get_origins')
    number = fields.Char('Number', size=None, select=True, readonly=True,
        help="The main identifier for the shipment.")
    picked_by = employee_field("Picked By")
    packed_by = employee_field("Packed By")
    done_by = employee_field("Done By")
    state = fields.Selection([
        ('draft', 'Draft'),
        ('done', 'Done'),
        ('cancelled', 'Cancelled'),
        ('assigned', 'Assigned'),
        ('picked', 'Picked'),
        ('packed', 'Packed'),
        ('waiting', 'Waiting'),
        ], 'State', readonly=True,
        help="The current state of the shipment.")

    @classmethod
    def __setup__(cls):
        super(ShipmentOut, cls).__setup__()
        cls.create_date.select = True
        cls._order = [
            ('effective_date', 'ASC NULLS LAST'),
            ('id', 'ASC'),
            ]
        cls._transitions |= set((
                ('draft', 'waiting'),
                ('waiting', 'assigned'),
                ('waiting', 'picked'),
                ('assigned', 'picked'),
                ('waiting', 'packed'),
                ('picked', 'packed'),
                ('packed', 'done'),
                ('assigned', 'waiting'),
                ('waiting', 'waiting'),
                ('waiting', 'draft'),
                ('draft', 'cancelled'),
                ('waiting', 'cancelled'),
                ('assigned', 'cancelled'),
                ('picked', 'cancelled'),
                ('packed', 'cancelled'),
                ('cancelled', 'draft'),
                ))
        cls._buttons.update({
                'cancel': {
                    'invisible': Eval('state').in_(['cancelled', 'done']),
                    'depends': ['state'],
                    },
                'draft': {
                    'invisible': ~Eval('state').in_(['waiting', 'cancelled']),
                    'icon': If(Eval('state') == 'cancelled',
                        'tryton-undo',
                        If(Eval('state') == 'waiting',
                            'tryton-back',
                            'tryton-forward')),
                    'depends': ['state'],
                    },
                'wait': {
                    'invisible': ~Eval('state').in_(['assigned', 'waiting',
                            'draft']),
                    'icon': If(Eval('state') == 'assigned',
                        'tryton-back',
                        If(Eval('state') == 'waiting',
                            'tryton-clear',
                            'tryton-forward')),
                    'depends': ['state'],
                    },
                'pick': {
                    'invisible': If(
                        Eval('warehouse_storage') == Eval('warehouse_output'),
                        True,
                        Eval('state') != 'assigned'),
                    'depends': [
                        'state', 'warehouse_storage', 'warehouse_output'],
                    },
                'pack': {
                    'invisible': If(
                        Eval('warehouse_storage') == Eval('warehouse_output'),
                        Eval('state') != 'waiting',
                        Eval('state') != 'picked'),
                    'depends': [
                        'state', 'warehouse_storage', 'warehouse_output'],
                    },
                'done': {
                    'invisible': Eval('state') != 'packed',
                    },
                'assign_wizard': {
                    'invisible': ((Eval('state') != 'waiting')
                        | (Eval('warehouse_storage')
                            == Eval('warehouse_output'))),
                    },
                'assign_try': {},
                'assign_force': {},
                })

    @classmethod
    def __register__(cls, module_name):
        cursor = Transaction().connection.cursor()
        table = cls.__table_handler__(module_name)
        sql_table = cls.__table__()

        # Migration from 3.8: rename code into number
        if table.column_exist('code'):
            table.column_rename('code', 'number')

        # Migration from 5.6: rename assigned_by into picked_by
        if table.column_exist('assigned_by'):
            table.column_rename('assigned_by', 'picked_by')

        super(ShipmentOut, cls).__register__(module_name)

        # Migration from 5.6: rename state cancel to cancelled
        cursor.execute(*sql_table.update(
                [sql_table.state], ['cancelled'],
                where=sql_table.state == 'cancel'))

    @classmethod
    def order_effective_date(cls, tables):
        table, _ = tables[None]
        return [Coalesce(table.effective_date, table.planned_date)]

    @staticmethod
    def default_state():
        return 'draft'

    @classmethod
    def default_warehouse(cls):
        Location = Pool().get('stock.location')
        return Location.get_default_warehouse()

    @staticmethod
    def default_company():
        return Transaction().context.get('company')

    @fields.depends('customer')
    def on_change_customer(self):
        self.delivery_address = None
        if self.customer:
            self.delivery_address = self.customer.address_get(type='delivery')

    @fields.depends('customer')
    def on_change_with_customer_location(self, name=None):
        if self.customer:
            return self.customer.customer_location.id

    @classmethod
    def default_warehouse_storage(cls):
        warehouse = cls.default_warehouse()
        if warehouse:
            return cls(warehouse=warehouse).on_change_with_warehouse_storage()

    @fields.depends('warehouse')
    def on_change_with_warehouse_storage(self, name=None):
        if self.warehouse:
            if self.warehouse.picking_location:
                location = self.warehouse.picking_location
            else:
                location = self.warehouse.storage_location
            return location.id

    @classmethod
    def default_warehouse_output(cls):
        warehouse = cls.default_warehouse()
        if warehouse:
            return cls(warehouse=warehouse).on_change_with_warehouse_output()

    @fields.depends('warehouse')
    def on_change_with_warehouse_output(self, name=None):
        if self.warehouse:
            return self.warehouse.output_location.id

    def get_outgoing_moves(self, name):
        moves = []
        for move in self.moves:
            if move.from_location == self.warehouse_output:
                moves.append(move.id)
        return moves

    @classmethod
    def set_outgoing_moves(cls, shipments, name, value):
        if not value:
            return
        cls.write(shipments, {
                'moves': value,
                })

    def get_inventory_moves(self, name):
        moves = []
        for move in self.moves:
            if move.to_location == self.warehouse_output:
                moves.append(move.id)
        return moves

    @classmethod
    def set_inventory_moves(cls, shipments, name, value):
        if not value:
            return
        cls.write(shipments, {
                'moves': value,
                })

    def get_origins(self, name):
        return ', '.join(set(filter(None,
                    (m.origin_name for m in self.outgoing_moves))))

    @classmethod
    @ModelView.button
    @Workflow.transition('draft')
    def draft(cls, shipments):
        Move = Pool().get('stock.move')
        Move.draft([m for s in shipments
                for m in s.inventory_moves + s.outgoing_moves
                if m.state != 'staging'])

    @classmethod
    @ModelView.button
    @Workflow.transition('waiting')
    def wait(cls, shipments, moves=None):
        """
        Complete inventory moves to match the products and quantities
        that are in the outgoing moves.
        If moves is set, only this subset is set to draft.
        """
        Move = Pool().get('stock.move')

        if moves is None:
            moves = sum((s.inventory_moves for s in shipments), ())
        else:
            assert all(m.shipment in shipments for m in moves)
        Move.draft(moves)
        Move.delete([m for s in shipments for m in s.inventory_moves
                if m.state in ('draft', 'cancelled')])

        to_create = []
        for shipment in shipments:
            if shipment.warehouse_storage == shipment.warehouse_output:
                # Do not create inventory moves
                continue
            for move in shipment.outgoing_moves:
                if move.state in ('cancelled', 'done'):
                    continue
                inventory_move = shipment._get_inventory_move(move)
                if inventory_move:
                    to_create.append(inventory_move)
        if to_create:
            Move.save(to_create)

    def _get_inventory_move(self, move):
        'Return inventory move for the outgoing move if necessary'
        pool = Pool()
        Move = pool.get('stock.move')
        Uom = pool.get('product.uom')
        quantity = move.quantity
        for inventory_move in self.inventory_moves:
            if (inventory_move.origin == move
                    and inventory_move.state != 'cancelled'):
                quantity -= Uom.compute_qty(
                    inventory_move.uom, inventory_move.quantity, move.uom)
        quantity = move.uom.round(quantity)
        if quantity <= 0:
            return
        inventory_move = Move(
            from_location=self.warehouse_storage,
            to_location=move.from_location,
            product=move.product,
            uom=move.uom,
            quantity=quantity,
            shipment=self,
            planned_date=move.planned_date,
            company=move.company,
            origin=move,
            state='staging' if move.state == 'staging' else 'draft',
            )
        if inventory_move.on_change_with_unit_price_required():
            inventory_move.unit_price = move.unit_price
            inventory_move.currency = move.currency
        return inventory_move

    @classmethod
    @Workflow.transition('assigned')
    def assign(cls, shipments):
        pool = Pool()
        Move = pool.get('stock.move')
        Move.assign([m for s in shipments for m in s.assign_moves])
        cls._sync_inventory_to_outgoing(shipments, quantity=False)

    @classmethod
    @ModelView.button
    @Workflow.transition('picked')
    @set_employee('picked_by')
    def pick(cls, shipments):
        pool = Pool()
        Move = pool.get('stock.move')
        Move.delete([
                m for s in shipments for m in s.inventory_moves
                if m.state == 'staging'])
        Move.do([m for s in shipments for m in s.inventory_moves])
        cls._sync_inventory_to_outgoing(shipments, quantity=True)

    @classmethod
    @ModelView.button
    @Workflow.transition('packed')
    @set_employee('packed_by')
    def pack(cls, shipments):
        pool = Pool()
        Move = pool.get('stock.move')
        for shipment in shipments:
            for move in shipment.inventory_moves:
                if move.state != 'done':
                    raise AccessError(
                        gettext('stock.msg_shipment_pack_inventory_done',
                            shipment=shipment.rec_name))
        Move.delete([m for s in shipments for m in s.outgoing_moves
            if not m.quantity])
        Move.assign([m for s in shipments for m in s.outgoing_moves])

    def _sync_move_key(self, move):
        return (
            ('product', move.product),
            ('uom', move.uom),
            )

    def _sync_outgoing_move(self, template=None):
        pool = Pool()
        Move = pool.get('stock.move')
        move = Move(
            from_location=self.warehouse_output,
            to_location=self.customer_location,
            quantity=0,
            shipment=self,
            planned_date=self.planned_date,
            company=self.company,
            )
        if template:
            move.origin = template.origin
        if move.on_change_with_unit_price_required():
            if template:
                move.unit_price = template.unit_price
                move.currency = template.currency
            else:
                move.unit_price = 0
                move.currency = self.company.currency
        return move

    @classmethod
    def _sync_inventory_to_outgoing(cls, shipments, quantity=True):
        pool = Pool()
        Move = pool.get('stock.move')
        Uom = pool.get('product.uom')

        def active(move):
            return move.state != 'cancelled'

        moves = []
        for shipment in shipments:
            if shipment.warehouse_storage == shipment.warehouse_output:
                # Do not have inventory moves
                continue

            outgoing_moves = {m: m for m in shipment.outgoing_moves}
            inventory_qty = defaultdict(lambda: defaultdict(float))
            for move in filter(active, shipment.outgoing_moves):
                key = shipment._sync_move_key(move)
                inventory_qty[move][key] = 0
            for move in filter(active, shipment.inventory_moves):
                key = shipment._sync_move_key(move)
                outgoing_move = outgoing_moves.get(move.origin)
                qty_default_uom = Uom.compute_qty(
                    move.uom, move.quantity,
                    move.product.default_uom, round=False)
                inventory_qty[outgoing_move][key] += qty_default_uom

            for outgoing_move in inventory_qty:
                if outgoing_move:
                    outgoing_key = shipment._sync_move_key(outgoing_move)
                for key, qty in inventory_qty[outgoing_move].items():
                    if not quantity and outgoing_move:
                        # Do not create outgoing move with origin
                        # to allow to reset to draft
                        continue
                    if outgoing_move and key == outgoing_key:
                        move = outgoing_move
                    else:
                        move = shipment._sync_outgoing_move(outgoing_move)
                        for name, value in key:
                            setattr(move, name, value)
                    qty = Uom.compute_qty(
                        move.product.default_uom, qty,
                        move.uom)
                    if quantity and move.quantity != qty:
                        move.quantity = qty
                        moves.append(move)
        Move.save(moves)

    @classmethod
    @ModelView.button
    @Workflow.transition('done')
    @set_employee('done_by')
    def done(cls, shipments):
        pool = Pool()
        Move = pool.get('stock.move')
        Date = pool.get('ir.date')

        Move.delete([
                m for s in shipments for m in s.outgoing_moves
                if m.state == 'staging'])
        Move.do([m for s in shipments for m in s.outgoing_moves])
        cls.write([s for s in shipments if not s.effective_date], {
                'effective_date': Date.today(),
                })

    @classmethod
    @ModelView.button
    @Workflow.transition('cancelled')
    def cancel(cls, shipments):
        Move = Pool().get('stock.move')
        Move.cancel([m for s in shipments
                for m in s.outgoing_moves + s.inventory_moves])

    @property
    def _move_planned_date(self):
        '''
        Return the planned date for outgoing moves and inventory moves
        '''
        return self.planned_date, self.planned_date

    @classmethod
    def _set_move_planned_date(cls, shipments):
        '''
        Set planned date of moves for the shipments
        '''
        Move = Pool().get('stock.move')
        to_write = []
        for shipment in shipments:
            outgoing_date, inventory_date = shipment._move_planned_date
            out_moves_to_write = [x for x in shipment.outgoing_moves
                    if (x.state not in ('assigned', 'done', 'cancelled')
                        and x.planned_date != outgoing_date)]
            if out_moves_to_write:
                to_write.extend((out_moves_to_write, {
                        'planned_date': outgoing_date,
                        }))

            inv_moves_to_write = [x for x in shipment.inventory_moves
                    if (x.state not in ('assigned', 'done', 'cancelled')
                        and x.planned_date != inventory_date)]
            if inv_moves_to_write:
                to_write.extend((inv_moves_to_write, {
                        'planned_date': inventory_date,
                        }))
        if to_write:
            Move.write(*to_write)

    @classmethod
    def create(cls, vlist):
        pool = Pool()
        Config = pool.get('stock.configuration')

        vlist = [x.copy() for x in vlist]
        config = Config(1)
        default_company = cls.default_company()
        for values in vlist:
            if values.get('number') is None:
                values['number'] = config.get_multivalue(
                    'shipment_out_sequence',
                    company=values.get('company', default_company)).get()
        shipments = super(ShipmentOut, cls).create(vlist)
        cls._set_move_planned_date(shipments)
        return shipments

    @classmethod
    def write(cls, *args):
        super(ShipmentOut, cls).write(*args)
        cls._set_move_planned_date(sum(args[::2], []))

    @classmethod
    def copy(cls, shipments, default=None):
        if default is None:
            default = {}
        else:
            default = default.copy()
        default.setdefault('inventory_moves', None)
        default.setdefault('outgoing_moves', None)
        default.setdefault('number', None)
        default.setdefault('picked_by', None)
        default.setdefault('packed_by', None)
        default.setdefault('done_by', None)
        return super(ShipmentOut, cls).copy(shipments, default=default)

    @classmethod
    def delete(cls, shipments):
        Move = Pool().get('stock.move')
        # Cancel before delete
        cls.cancel(shipments)
        for shipment in shipments:
            if shipment.state != 'cancelled':
                raise AccessError(
                    gettext('stock.msg_shipment_delete_cancel',
                        shipment=shipment.rec_name))
        Move.delete([m for s in shipments for m in s.moves])
        super(ShipmentOut, cls).delete(shipments)

    @staticmethod
    def _location_amount(target_uom, qty_uom, uom_index):
        """
        Take a raw list of quantities and uom and convert it to
        the target uom.
        """
        Uom = Pool().get('product.uom')
        res = 0
        for uom, qty in qty_uom:
            res += Uom.compute_qty(uom_index[uom], qty,
                    uom_index[target_uom])
        return res

    @classmethod
    @ModelView.button_action('stock.wizard_shipment_out_assign')
    def assign_wizard(cls, shipments):
        pass

    @property
    def assign_moves(self):
        return self.inventory_moves

    @dualmethod
    @ModelView.button
    def assign_try(cls, shipments):
        Move = Pool().get('stock.move')
        to_assign = [
            m for s in shipments for m in s.assign_moves
            if m.assignation_required]
        if Move.assign_try(to_assign):
            cls.assign(shipments)
            return True
        else:
            return False

    @classmethod
    def _get_reschedule_domain(cls, date):
        return [
            ('state', '=', 'waiting'),
            ('planned_date', '<', date),
            ]

    @classmethod
    def reschedule(cls, date=None):
        pool = Pool()
        Date = pool.get('ir.date')
        if date is None:
            date = Date.today()
        shipments = cls.search(cls._get_reschedule_domain(date))
        cls.write(shipments, {'planned_date': date})


class ShipmentOutReturn(ShipmentMixin, Workflow, ModelSQL, ModelView):
    "Customer Return Shipment"
    __name__ = 'stock.shipment.out.return'
    _rec_name = 'number'
    effective_date = fields.Date('Effective Date',
        states={
            'readonly': Eval('state').in_(['cancelled', 'done']),
            },
        depends=['state'],
        help="When the stock was returned.")
    planned_date = fields.Date('Planned Date',
        states={
            'readonly': Eval('state') != 'draft',
            }, depends=['state'],
        help="When the stock is expected to be returned.")
    company = fields.Many2One(
        'company.company', "Company", required=True,
        states={
            'readonly': Eval('state') != 'draft',
            },
        depends=['state'],
        help="The company the shipment is associated with.")
    customer = fields.Many2One('party.party', 'Customer', required=True,
        states={
            'readonly': ((Eval('state') != 'draft')
                | Eval('incoming_moves', [0])),
            },
        context={
            'company': Eval('company', -1),
            },
        depends=['state', 'company'],
        help="The party that purchased the stock.")
    customer_location = fields.Function(fields.Many2One('stock.location',
            'Customer Location'), 'on_change_with_customer_location')
    delivery_address = fields.Many2One('party.address',
        'Delivery Address', required=True,
        states={
            'readonly': Eval('state') != 'draft',
            }, domain=[('party', '=', Eval('customer'))],
        depends=['state', 'customer'],
        help="The address the customer can be contacted at.")
    reference = fields.Char("Reference", size=None, select=True,
        states={
            'readonly': Eval('state') != 'draft',
            }, depends=['state'],
        help="The customer's identifier for the shipment.")
    warehouse = fields.Many2One('stock.location', "Warehouse", required=True,
        states={
            'readonly': ((Eval('state') != 'draft')
                | Eval('incoming_moves', [0]) | Eval('inventory_moves', [0])),
            }, domain=[('type', '=', 'warehouse')],
        depends=['state'],
        help="Where the stock is returned.")
    warehouse_storage = fields.Function(fields.Many2One('stock.location',
            'Warehouse Storage'), 'on_change_with_warehouse_storage')
    warehouse_input = fields.Function(fields.Many2One('stock.location',
            'Warehouse Input'), 'on_change_with_warehouse_input')
    incoming_moves = fields.Function(fields.One2Many('stock.move', 'shipment',
            'Incoming Moves',
            domain=[
                ('from_location', '=', Eval('customer_location')),
                If(Eval('warehouse_input') == Eval('warehouse_storage'),
                    ('to_location', 'child_of',
                        [Eval('warehouse_input', -1)], 'parent'),
                    ('to_location', '=', Eval('warehouse_input'))),
                ('company', '=', Eval('company')),
                ],
            states={
                'readonly': ((Eval('state') != 'draft')
                    | ~Eval('warehouse') | ~Eval('customer')),
                },
            depends=['state', 'warehouse', 'customer', 'customer_location',
                'warehouse_input', 'warehouse_storage', 'company'],
            help="The moves that bring the stock into the warehouse."),
        'get_incoming_moves', setter='set_incoming_moves')
    inventory_moves = fields.Function(fields.One2Many('stock.move', 'shipment',
            'Inventory Moves',
            domain=[
                ('from_location', '=', Eval('warehouse_input')),
                If(Eval('state').in_(['received']),
                    ['OR',
                        ('to_location', 'child_of',
                            [Eval('warehouse_storage', -1)], 'parent'),
                        ('to_location.waste_warehouses', '=',
                            Eval('warehouse')),
                        ],
                    []),
                ('company', '=', Eval('company')),
                ],
            states={
                'readonly': Eval('state').in_(['draft', 'cancelled', 'done']),
                'invisible': (
                    Eval('warehouse_input') == Eval('warehouse_storage')),
                },
            depends=['state', 'warehouse', 'warehouse_input',
                'warehouse_storage', 'warehouse_input', 'company'],
            help="The moves that put the stock away into the storage area."),
        'get_inventory_moves', setter='set_inventory_moves')
    moves = fields.One2Many('stock.move', 'shipment', 'Moves',
        domain=[('company', '=', Eval('company'))], depends=['company'],
        readonly=True)
    origins = fields.Function(fields.Char('Origins'), 'get_origins')
    number = fields.Char('Number', size=None, select=True, readonly=True,
        help="The main identifier for the shipment.")
    received_by = employee_field("Received By")
    done_by = employee_field("Done By")
    state = fields.Selection([
        ('draft', 'Draft'),
        ('done', 'Done'),
        ('cancelled', 'Cancelled'),
        ('received', 'Received'),
        ], 'State', readonly=True,
        help="The current state of the shipment.")

    @classmethod
    def __setup__(cls):
        super(ShipmentOutReturn, cls).__setup__()
        cls.create_date.select = True
        cls._order = [
            ('effective_date', 'ASC NULLS LAST'),
            ('id', 'ASC'),
            ]
        cls._transitions |= set((
                ('draft', 'received'),
                ('received', 'done'),
                ('received', 'draft'),
                ('draft', 'cancelled'),
                ('received', 'cancelled'),
                ('cancelled', 'draft'),
                ))
        cls._buttons.update({
                'cancel': {
                    'invisible': Eval('state').in_(['cancelled', 'done']),
                    'depends': ['state'],
                    },
                'draft': {
                    'invisible': Eval('state') != 'cancelled',
                    'depends': ['state'],
                    },
                'receive': {
                    'invisible': Eval('state') != 'draft',
                    'depends': ['state'],
                    },
                'done': {
                    'invisible': Eval('state') != 'received',
                    'depends': ['state'],
                    },
                })

    @classmethod
    def __register__(cls, module_name):
        cursor = Transaction().connection.cursor()
        table = cls.__table_handler__(module_name)
        sql_table = cls.__table__()

        # Migration from 3.8: rename code into number
        if table.column_exist('code'):
            table.column_rename('code', 'number')

        super(ShipmentOutReturn, cls).__register__(module_name)

        # Migration from 5.6: rename state cancel to cancelled
        cursor.execute(*sql_table.update(
                [sql_table.state], ['cancelled'],
                where=sql_table.state == 'cancel'))

    @classmethod
    def order_effective_date(cls, tables):
        table, _ = tables[None]
        return [Coalesce(table.effective_date, table.planned_date)]

    @staticmethod
    def default_state():
        return 'draft'

    @classmethod
    def default_warehouse(cls):
        Location = Pool().get('stock.location')
        return Location.get_default_warehouse()

    @staticmethod
    def default_company():
        return Transaction().context.get('company')

    @fields.depends('customer')
    def on_change_customer(self):
        self.delivery_address = None
        if self.customer:
            self.delivery_address = self.customer.address_get(type='delivery')

    @fields.depends('customer')
    def on_change_with_customer_location(self, name=None):
        if self.customer:
            return self.customer.customer_location.id

    @classmethod
    def default_warehouse_storage(cls):
        warehouse = cls.default_warehouse()
        if warehouse:
            return cls(warehouse=warehouse).on_change_with_warehouse_storage()

    @fields.depends('warehouse')
    def on_change_with_warehouse_storage(self, name=None):
        if self.warehouse:
            return self.warehouse.storage_location.id

    @classmethod
    def default_warehouse_input(cls):
        warehouse = cls.default_warehouse()
        if warehouse:
            return cls(warehouse=warehouse).on_change_with_warehouse_input()

    @fields.depends('warehouse')
    def on_change_with_warehouse_input(self, name=None):
        if self.warehouse:
            return self.warehouse.input_location.id

    def get_incoming_moves(self, name):
        moves = []
        if self.warehouse_input == self.warehouse_storage:
            return [m.id for m in self.moves]
        for move in self.moves:
            if move.to_location == self.warehouse_input:
                moves.append(move.id)
        return moves

    @classmethod
    def set_incoming_moves(cls, shipments, name, value):
        if not value:
            return
        cls.write(shipments, {
                'moves': value,
                })

    def get_inventory_moves(self, name):
        moves = []
        for move in self.moves:
            if move.from_location == self.warehouse_input:
                moves.append(move.id)
        return moves

    @classmethod
    def set_inventory_moves(cls, shipments, name, value):
        if not value:
            return
        cls.write(shipments, {
                'moves': value,
                })

    def _get_move_planned_date(self):
        '''
        Return the planned date for incoming moves and inventory moves
        '''
        return self.planned_date, self.planned_date

    @classmethod
    def _set_move_planned_date(cls, shipments):
        '''
        Set planned date of moves for the shipments
        '''
        Move = Pool().get('stock.move')
        to_write = []
        for shipment in shipments:
            dates = shipment._get_move_planned_date()
            incoming_date, inventory_date = dates
            incoming_moves_to_write = [x for x in shipment.incoming_moves
                if (x.state not in ('assigned', 'done', 'cancelled')
                    and x.planned_date != incoming_date)]
            if incoming_moves_to_write:
                to_write.extend((incoming_moves_to_write, {
                            'planned_date': incoming_date,
                            }))
            inventory_moves_to_write = [x for x in shipment.inventory_moves
                if (x.state not in ('assigned', 'done', 'cancelled')
                    and x.planned_date != inventory_date)]
            if inventory_moves_to_write:
                to_write.extend((inventory_moves_to_write, {
                            'planned_date': inventory_date,
                            }))
        if to_write:
            Move.write(*to_write)

    def get_origins(self, name):
        return ', '.join(set(filter(None,
                    (m.origin_name for m in self.incoming_moves))))

    @classmethod
    def create(cls, vlist):
        pool = Pool()
        Config = pool.get('stock.configuration')

        vlist = [x.copy() for x in vlist]
        config = Config(1)
        default_company = cls.default_company()
        for values in vlist:
            if values.get('number') is None:
                values['number'] = config.get_multivalue(
                    'shipment_out_return_sequence',
                    company=values.get('company', default_company)).get()
        shipments = super(ShipmentOutReturn, cls).create(vlist)
        cls._set_move_planned_date(shipments)
        return shipments

    @classmethod
    def write(cls, *args):
        super(ShipmentOutReturn, cls).write(*args)
        cls._set_move_planned_date(sum(args[::2], []))

    @classmethod
    def copy(cls, shipments, default=None):
        if default is None:
            default = {}
        default = default.copy()
        default.setdefault('inventory_moves', None)
        default.setdefault('incoming_moves', None)
        default.setdefault('number', None)
        default.setdefault('received_by', None)
        default.setdefault('done_by', None)
        return super(ShipmentOutReturn, cls).copy(shipments, default=default)

    @classmethod
    def delete(cls, shipments):
        Move = Pool().get('stock.move')
        # Cance before delete
        cls.cancel(shipments)
        for shipment in shipments:
            if shipment.state != 'cancelled':
                raise AccessError(
                    gettext('stock.msg_shipment_delete_cancel',
                        shipment=shipment.rec_name))
        Move.delete([m for s in shipments for m in s.moves])
        super(ShipmentOutReturn, cls).delete(shipments)

    @classmethod
    @ModelView.button
    @Workflow.transition('draft')
    def draft(cls, shipments):
        Move = Pool().get('stock.move')
        Move.draft([m for s in shipments for m in s.incoming_moves
                if m.state != 'staging'])
        Move.delete([m for s in shipments for m in s.inventory_moves
                if m.state in ('draft', 'cancelled')])

    @classmethod
    @ModelView.button
    @Workflow.transition('received')
    @set_employee('received_by')
    def receive(cls, shipments):
        Move = Pool().get('stock.move')
        Move.do([m for s in shipments for m in s.incoming_moves])
        cls.create_inventory_moves(shipments)
        # Set received state to allow done transition
        cls.write(shipments, {'state': 'received'})
        to_do = [s for s in shipments
            if s.warehouse_storage == s.warehouse_input]
        if to_do:
            cls.done(to_do)

    @classmethod
    @ModelView.button
    @Workflow.transition('done')
    @set_employee('done_by')
    def done(cls, shipments):
        pool = Pool()
        Move = pool.get('stock.move')
        Date = pool.get('ir.date')
        Move.do([m for s in shipments for m in s.inventory_moves])
        cls.write([s for s in shipments if not s.effective_date], {
                'effective_date': Date.today(),
                })

    @classmethod
    @ModelView.button
    @Workflow.transition('cancelled')
    def cancel(cls, shipments):
        Move = Pool().get('stock.move')
        Move.cancel([m for s in shipments
                for m in s.incoming_moves + s.inventory_moves])

    def _get_inventory_move(self, incoming_move):
        'Return inventory move for the incoming move'
        pool = Pool()
        Move = pool.get('stock.move')
        Date = pool.get('ir.date')
        if incoming_move.quantity <= 0.0:
            return
        move = Move()
        move.product = incoming_move.product
        move.uom = incoming_move.uom
        move.quantity = incoming_move.quantity
        move.from_location = incoming_move.to_location
        move.to_location = self.warehouse_storage
        move.state = Move.default_state()
        move.planned_date = max(
            filter(None, [self._get_move_planned_date()[1], Date.today()]))
        move.company = incoming_move.company
        move.origin = incoming_move
        return move

    @classmethod
    def create_inventory_moves(cls, shipments):
        for shipment in shipments:
            if shipment.warehouse_storage == shipment.warehouse_input:
                # Do not create inventory moves
                continue
            # Use moves instead of inventory_moves because save reset before
            # adding new records and as set_inventory_moves is just a proxy to
            # moves, it will reset also the incoming_moves
            moves = list(shipment.moves)
            for incoming_move in shipment.incoming_moves:
                move = shipment._get_inventory_move(incoming_move)
                if move:
                    moves.append(move)
            shipment.moves = moves
        cls.save(shipments)


class ShipmentInternal(ShipmentAssignMixin, Workflow, ModelSQL, ModelView):
    "Internal Shipment"
    __name__ = 'stock.shipment.internal'
    _rec_name = 'number'
    _assign_moves_field = 'moves'
    effective_date = fields.Date('Effective Date',
        states={
            'readonly': Eval('state').in_(['cancelled', 'done']),
            },
        depends=['state'],
        help="When the shipment was actually completed.")
    planned_date = fields.Date('Planned Date',
        states={
            'readonly': ~Eval('state').in_(['request', 'draft']),
            }, depends=['state'],
        help="When the shipment is expected to be completed.")
    effective_start_date = fields.Date('Effective Start Date',
        states={
            'readonly': Eval('state').in_(['cancelled', 'shipped', 'done']),
            },
        depends=['state'],
        help="When the stock was actually sent.")
    planned_start_date = fields.Date('Planned Start Date',
        states={
            'readonly': ~Eval('state').in_(['request', 'draft']),
            'required': Bool(Eval('planned_date')),
            },
        depends=['state'],
        help="When the stock is expected to be sent.")
    company = fields.Many2One(
        'company.company', "Company", required=True,
        states={
            'readonly': ~Eval('state').in_(['request', 'draft']),
            },
        depends=['state'],
        help="The company the shipment is associated with.")
    number = fields.Char('Number', size=None, select=True, readonly=True,
        help="The main identifier for the shipment.")
    reference = fields.Char("Reference", size=None, select=True,
        states={
            'readonly': ~Eval('state').in_(['request', 'draft']),
            }, depends=['state'],
        help="The external identifiers for the shipment.")
    from_location = fields.Many2One('stock.location', "From Location",
        required=True, states={
            'readonly': (~Eval('state').in_(['request', 'draft'])
                | Eval('moves', [0])),
            },
        domain=[
            ('type', 'in', ['view', 'storage', 'lost_found']),
            ], depends=['state'],
        help="Where the stock is moved from.")
    to_location = fields.Many2One('stock.location', "To Location",
        required=True, states={
            'readonly': (~Eval('state').in_(['request', 'draft'])
                    | Eval('moves', [0])),
            }, domain=[
            ('type', 'in', ['view', 'storage', 'lost_found']),
            ], depends=['state'],
        help="Where the stock is moved to.")
    transit_location = fields.Function(fields.Many2One('stock.location',
            'Transit Location',
            help="Where the stock is located while it is in transit between "
            "the warehouses."),
        'on_change_with_transit_location')
    warehouse = fields.Function(
        fields.Many2One(
            'stock.location', "Warehouse",
            help="Where the stock is sent from."),
        'on_change_with_warehouse')
    moves = fields.One2Many('stock.move', 'shipment', 'Moves',
        states={
            'readonly': (Eval('state').in_(['cancelled', 'assigned', 'done'])
                | ~Eval('from_location') | ~Eval('to_location')),
            'invisible': (Bool(Eval('transit_location'))
                & ~Eval('state').in_(['request', 'draft'])),
            },
        domain=[
            If(Eval('state').in_(['request', 'draft']), [
                    ('from_location', '=', Eval('from_location')),
                    ('to_location', '=', Eval('to_location')),
                    ],
                If(~Eval('state').in_(['done', 'cancelled']),
                    If(~Eval('transit_location'),
                        [
                            ('from_location', 'child_of',
                                [Eval('from_location', -1)], 'parent'),
                            ('to_location', 'child_of',
                                [Eval('to_location', -1)], 'parent'),
                            ],
                        ['OR',
                            [
                                ('from_location', 'child_of',
                                    [Eval('from_location', -1)], 'parent'),
                                ('to_location', '=', Eval('transit_location')),
                                ],
                            [
                                ('from_location', '=',
                                    Eval('transit_location')),
                                ('to_location', 'child_of',
                                    [Eval('to_location', -1)], 'parent'),
                                ],
                            ]),
                    [])),
            ('company', '=', Eval('company')),
            ],
        depends=['state', 'from_location', 'to_location', 'transit_location',
            'company'],
        help="The moves that perform the shipment.")
    outgoing_moves = fields.Function(fields.One2Many('stock.move', 'shipment',
            'Outgoing Moves',
            domain=[
                If(Eval('state').in_(['request', 'draft']), [
                        ('from_location', 'child_of',
                            [Eval('from_location', -1)], 'parent'),
                        If(~Eval('transit_location'),
                            ('to_location', 'child_of',
                                [Eval('to_location', -1)], 'parent'),
                            ('to_location', '=', Eval('transit_location'))),
                        ],
                    []),
                ],
            states={
                'readonly': Eval('state').in_(
                    ['assigned', 'shipped', 'done', 'cancelled']),
                'invisible': (~Eval('transit_location')
                    | Eval('state').in_(['request', 'draft'])),
                },
            depends=['from_location', 'to_location', 'transit_location',
                'state'],
            help="The moves that send the stock out."),
        'get_outgoing_moves', setter='set_moves')
    incoming_moves = fields.Function(fields.One2Many('stock.move', 'shipment',
            'Incoming Moves',
            domain=[
                If(~Eval('state').in_(['done', 'cancelled']), [
                        If(~Eval('transit_location'),
                            ('from_location', 'child_of',
                                [Eval('from_location', -1)], 'parent'),
                            ('from_location', '=', Eval('transit_location'))),
                        ('to_location', 'child_of',
                            [Eval('to_location', -1)], 'parent'),
                        ],
                    []),
                ],
            states={
                'readonly': Eval('state').in_(['done', 'cancelled']),
                'invisible': (~Eval('transit_location')
                    | Eval('state').in_(['request', 'draft'])),
                },
            depends=['from_location', 'to_location', 'transit_location',
                'state'],
            help="The moves that receive the stock in."),
        'get_incoming_moves', setter='set_moves')
    assigned_by = employee_field("Received By")
    shipped_by = employee_field("Shipped By")
    done_by = employee_field("Done By")
    state = fields.Selection([
            ('request', 'Request'),
            ('draft', 'Draft'),
            ('cancelled', 'Cancelled'),
            ('waiting', 'Waiting'),
            ('assigned', 'Assigned'),
            ('shipped', 'Shipped'),
            ('done', 'Done'),
            ], 'State', readonly=True,
        help="The current state of the shipment.")

    @classmethod
    def __setup__(cls):
        super(ShipmentInternal, cls).__setup__()
        cls.create_date.select = True
        cls._order = [
            ('effective_date', 'ASC NULLS LAST'),
            ('id', 'ASC'),
            ]
        cls._transitions |= set((
                ('request', 'draft'),
                ('draft', 'waiting'),
                ('waiting', 'waiting'),
                ('waiting', 'assigned'),
                ('assigned', 'shipped'),
                ('assigned', 'done'),
                ('shipped', 'done'),
                ('waiting', 'draft'),
                ('assigned', 'waiting'),
                ('request', 'cancelled'),
                ('draft', 'cancelled'),
                ('waiting', 'cancelled'),
                ('assigned', 'cancelled'),
                ('cancelled', 'draft'),
                ))
        cls._buttons.update({
                'cancel': {
                    'invisible': Eval('state').in_(
                        ['cancelled', 'shipped', 'done']),
                    'depends': ['state'],
                    },
                'draft': {
                    'invisible': ~Eval('state').in_(
                        ['cancelled', 'request', 'waiting']),
                    'icon': If(Eval('state') == 'cancelled',
                        'tryton-undo',
                        If(Eval('state') == 'request',
                            'tryton-forward',
                            'tryton-back')),
                    'depends': ['state'],
                    },
                'wait': {
                    'invisible': ~Eval('state').in_(['assigned', 'waiting',
                            'draft']),
                    'icon': If(Eval('state') == 'assigned',
                        'tryton-back',
                        If(Eval('state') == 'waiting',
                            'tryton-clear',
                            'tryton-forward')),
                    'depends': ['state'],
                    },
                'ship': {
                    'invisible': ((Eval('state') != 'assigned')
                        | ~Eval('transit_location')),
                    'depends': ['state', 'transit_location'],
                    },
                'done': {
                    'invisible': If(
                        ~Eval('transit_location'),
                        Eval('state') != 'assigned',
                        Eval('state') != 'shipped'),
                    'depends': ['state', 'transit_location'],
                    },
                'assign_wizard': {
                    'invisible': Eval('state') != 'waiting',
                    'depends': ['state'],
                    },
                'assign_try': {},
                'assign_force': {},
                })

    @classmethod
    def __register__(cls, module_name):
        cursor = Transaction().connection.cursor()
        sql_table = cls.__table__()
        table = cls.__table_handler__(module_name)

        # Migration from 3.8:
        if table.column_exist('code'):
            table.column_rename('code', 'number')

        super(ShipmentInternal, cls).__register__(module_name)

        # Migration from 4.0: fill planned_start_date
        cursor = Transaction().connection.cursor()
        cursor.execute(*sql_table.update(
                [sql_table.planned_start_date],
                [sql_table.planned_date],
                where=(sql_table.planned_start_date == Null)
                & (sql_table.planned_date != Null)))

        # Migration from 5.6: rename state cancel to cancelled
        cursor.execute(*sql_table.update(
                [sql_table.state], ['cancelled'],
                where=sql_table.state == 'cancel'))

    @classmethod
    def order_effective_date(cls, tables):
        table, _ = tables[None]
        return [Coalesce(
                table.effective_start_date, table.effective_date,
                table.planned_start_date, table.planned_date)]

    @staticmethod
    def default_state():
        return 'draft'

    @staticmethod
    def default_company():
        return Transaction().context.get('company')

    @fields.depends('planned_date', 'planned_start_date', 'company')
    def on_change_with_transit_location(self, name=None):
        pool = Pool()
        Config = pool.get('stock.configuration')
        if self.planned_date != self.planned_start_date:
            return Config(1).get_multivalue(
                'shipment_internal_transit',
                company=self.company.id if self.company else None).id

    @fields.depends('from_location')
    def on_change_with_warehouse(self, name=None):
        if self.from_location and self.from_location.warehouse:
            return self.from_location.warehouse.id

    @fields.depends('planned_date', 'from_location', 'to_location')
    def on_change_with_planned_start_date(self, pattern=None):
        pool = Pool()
        LocationLeadTime = pool.get('stock.location.lead_time')
        if self.planned_date:
            if pattern is None:
                pattern = {}
            pattern.setdefault('warehouse_from',
                self.from_location.warehouse.id
                if self.from_location and self.from_location.warehouse
                else None)
            pattern.setdefault('warehouse_to',
                self.to_location.warehouse.id
                if self.to_location and self.to_location.warehouse
                else None)
            lead_time = LocationLeadTime.get_lead_time(pattern)
            if lead_time:
                return self.planned_date - lead_time
        return self.planned_date

    def get_outgoing_moves(self, name):
        if not self.transit_location:
            return [m.id for m in self.moves]
        moves = []
        for move in self.moves:
            if move.to_location == self.transit_location:
                moves.append(move.id)
        return moves

    def get_incoming_moves(self, name):
        if not self.transit_location:
            return [m.id for m in self.moves]
        moves = []
        for move in self.moves:
            if move.from_location == self.transit_location:
                moves.append(move.id)
        return moves

    @classmethod
    def set_moves(cls, shipments, name, value):
        if not value:
            return
        cls.write(shipments, {
                'moves': value,
                })

    @classmethod
    def create(cls, vlist):
        pool = Pool()
        Config = pool.get('stock.configuration')

        vlist = [x.copy() for x in vlist]
        config = Config(1)
        default_company = cls.default_company()
        for values in vlist:
            if values.get('number') is None:
                values['number'] = config.get_multivalue(
                    'shipment_internal_sequence',
                    company=values.get('company', default_company)).get()
        shipments = super().create(vlist)
        cls._set_move_planned_date(shipments)
        return shipments

    @classmethod
    def write(cls, *args):
        super().write(*args)
        cls._set_move_planned_date(sum(args[::2], []))

    @classmethod
    def delete(cls, shipments):
        Move = Pool().get('stock.move')
        # Cancel before delete
        cls.cancel(shipments)
        for shipment in shipments:
            if shipment.state != 'cancelled':
                raise AccessError(
                    gettext('stock.msg_shipment_delete_cancel',
                        shipment=shipment.rec_name))
        Move.delete([m for s in shipments for m in s.moves])
        super(ShipmentInternal, cls).delete(shipments)

    @classmethod
    def copy(cls, shipments, default=None):
        def shipment_field(data, name):
            model, shipment_id = data['shipment'].split(',', 1)
            assert model == cls.__name__
            shipment_id = int(shipment_id)
            shipment = id2shipments[shipment_id]
            return getattr(shipment, name)

        def outgoing_moves(data):
            shipment = id2shipments[data['id']]
            return shipment.outgoing_moves
        id2shipments = {s.id: s for s in shipments}

        if default is None:
            default = {}
        else:
            default = default.copy()
        default.setdefault('moves', outgoing_moves)
        default.setdefault('moves.from_location', partial(
                shipment_field, name='from_location'))
        default.setdefault('moves.to_location', partial(
                shipment_field, name='to_location'))
        default.setdefault('moves.planned_date', partial(
                shipment_field, name='planned_date'))
        default.setdefault('number', None)
        default.setdefault('assigned_by', None)
        default.setdefault('shipped_by', None)
        default.setdefault('done_by', None)
        return super().copy(shipments, default=default)

    def _sync_move_key(self, move):
        return (
            ('product', move.product),
            ('uom', move.uom),
            )

    def _sync_incoming_move(self, template=None):
        pool = Pool()
        Move = pool.get('stock.move')
        move = Move(
            from_location=self.transit_location,
            to_location=self.to_location,
            quantity=0,
            shipment=self,
            planned_date=self.planned_date,
            company=self.company,
            )
        if template:
            move.origin = template.origin
        if move.on_change_with_unit_price_required():
            if template:
                move.unit_price = template.unit_price
                move.currency = template.currency
            else:
                move.unit_price = 0
                move.currency = self.company.currency
        return move

    @classmethod
    def _sync_moves(cls, shipments):
        pool = Pool()
        Move = pool.get('stock.move')
        Uom = pool.get('product.uom')

        def active(move):
            return move.state != 'cancelled'

        moves = []
        for shipment in shipments:
            if not shipment.transit_location:
                continue

            incoming_moves = {m: m for m in shipment.incoming_moves}
            outgoing_qty = defaultdict(lambda: defaultdict(lambda: 0))
            for move in filter(active, shipment.incoming_moves):
                key = shipment._sync_move_key(move)
                outgoing_qty[move][key] = 0
            for move in filter(active, shipment.outgoing_moves):
                key = shipment._sync_move_key(move)
                incoming_move = incoming_moves.get(move.origin)
                qty_default_uom = Uom.compute_qty(
                    move.uom, move.quantity,
                    move.product.default_uom, round=False)
                outgoing_qty[incoming_move][key] += qty_default_uom

            for incoming_move in outgoing_qty:
                if incoming_move:
                    incoming_key = shipment._sync_move_key(incoming_move)
                for key, qty in outgoing_qty[incoming_move].items():
                    if incoming_move and key == incoming_key:
                        move = incoming_move
                    else:
                        move = shipment._sync_incoming_move(incoming_move)
                        for name, value in key:
                            setattr(move, name, value)
                    qty = Uom.compute_qty(
                        move.product.default_uom, qty,
                        move.uom)
                    if move.quantity != qty:
                        move.quantity = qty
                        moves.append(move)
        Move.save(moves)

    @classmethod
    def _set_transit(cls, shipments):
        pool = Pool()
        Move = pool.get('stock.move')

        to_write = []
        for shipment in shipments:
            if not shipment.transit_location:
                continue
            moves = [m for m in shipment.moves
                if m.state != 'done'
                and m.from_location != shipment.transit_location
                and m.to_location != shipment.transit_location]
            Move.copy(moves, default={
                    'to_location': shipment.transit_location.id,
                    'planned_date': shipment.planned_start_date,
                    'origin': lambda data: '%s,%s' % (
                        Move.__name__, data['id']),
                    })
            to_write.append(moves)
            to_write.append({
                    'from_location': shipment.transit_location.id,
                    'planned_date': shipment.planned_date,
                    })
        if to_write:
            Move.write(*to_write)

    @classmethod
    @ModelView.button
    @Workflow.transition('draft')
    def draft(cls, shipments):
        Move = Pool().get('stock.move')

        # First reset state to draft to allow update from and to location
        Move.draft([m for s in shipments for m in s.moves
                if m.state != 'staging'])
        Move.delete([m for s in shipments for m in s.moves
                if m.from_location == s.transit_location])
        for shipment in shipments:
            Move.write([m for m in shipment.moves
                    if m.state != 'done'], {
                    'from_location': shipment.from_location.id,
                    'to_location': shipment.to_location.id,
                    'planned_date': shipment.planned_date,
                    })

    @classmethod
    @ModelView.button
    @Workflow.transition('waiting')
    def wait(cls, shipments, moves=None):
        """
        If moves is set, only this subset is set to draft.
        """
        Move = Pool().get('stock.move')

        if moves is None:
            moves = sum((s.moves for s in shipments), ())
        else:
            assert all(m.shipment in shipments for m in moves)
        Move.draft(moves)

        moves = []
        for shipment in shipments:
            if shipment.transit_location:
                continue
            for move in shipment.moves:
                if move.state != 'done':
                    move.planned_date = shipment.planned_date
                    moves.append(move)
        Move.save(moves)

        cls._set_transit(shipments)
        cls._sync_moves(shipments)

    @classmethod
    @Workflow.transition('assigned')
    @set_employee('assigned_by')
    def assign(cls, shipments):
        pool = Pool()
        Move = pool.get('stock.move')
        Move.assign([m for s in shipments for m in s.assign_moves])

    @classmethod
    @ModelView.button
    @Workflow.transition('shipped')
    @set_employee('shipped_by')
    def ship(cls, shipments):
        pool = Pool()
        Move = pool.get('stock.move')
        Date = pool.get('ir.date')
        Move.do([m for s in shipments for m in s.outgoing_moves])
        cls._sync_moves(shipments)
        cls.write([s for s in shipments if not s.effective_start_date], {
                'effective_start_date': Date.today(),
                })

    @classmethod
    @ModelView.button
    @Workflow.transition('done')
    @set_employee('done_by')
    def done(cls, shipments):
        pool = Pool()
        Move = pool.get('stock.move')
        Date = pool.get('ir.date')
        Move.do([m for s in shipments for m in s.incoming_moves])
        cls.write([s for s in shipments if not s.effective_date], {
                'effective_date': Date.today(),
                })

    @classmethod
    @ModelView.button
    @Workflow.transition('cancelled')
    def cancel(cls, shipments):
        Move = Pool().get('stock.move')
        Move.cancel([m for s in shipments for m in s.moves])

    @classmethod
    @ModelView.button_action('stock.wizard_shipment_internal_assign')
    def assign_wizard(cls, shipments):
        pass

    @property
    def assign_moves(self):
        return self.outgoing_moves

    @dualmethod
    @ModelView.button
    def assign_try(cls, shipments):
        Move = Pool().get('stock.move')
        to_assign = [
            m for s in shipments for m in s.assign_moves
            if m.assignation_required]
        if not to_assign or Move.assign_try(to_assign):
            cls.assign(shipments)
            return True
        else:
            return False

    @property
    def _move_planned_date(self):
        '''
        Return the planned date for incoming moves and inventory_moves
        '''
        return self.planned_start_date, self.planned_date

    @classmethod
    def _set_move_planned_date(cls, shipments):
        '''
        Set planned date of moves for the shipments
        '''
        Move = Pool().get('stock.move')
        to_write = []
        for shipment in shipments:
            dates = shipment._move_planned_date
            outgoing_date, incoming_date = dates
            outgoing_moves = [m for m in shipment.outgoing_moves
                if (m.state not in ('assigned', 'done', 'cancelled')
                    and m.planned_date != outgoing_date)]
            if outgoing_moves:
                to_write.append(outgoing_moves)
                to_write.append({
                        'planned_date': outgoing_date,
                        })
            if shipment.transit_location:
                incoming_moves = [m for m in shipment.incoming_moves
                    if (m.state not in ('assigned', 'done', 'cancelled')
                        and m.planned_date != incoming_date)]
                if incoming_moves:
                    to_write.append(incoming_moves)
                    to_write.append({
                            'planned_date': incoming_date,
                            })
        if to_write:
            Move.write(*to_write)

    @classmethod
    def _get_reschedule_domain(cls, date):
        return [
            ('state', '=', 'waiting'),
            ('planned_date', '<', date),
            ]

    @classmethod
    def reschedule(cls, date=None):
        pool = Pool()
        Date = pool.get('ir.date')
        if date is None:
            date = Date.today()
        shipments = cls.search(cls._get_reschedule_domain(date))
        for shipment in shipments:
            shipment.planned_date = date
            shipment.planned_start_date = (
                shipment.on_change_with_planned_start_date())
        cls.save(shipments)


class Assign(Wizard):
    "Assign Shipment"
    __name__ = 'stock.shipment.assign'
    start = StateTransition()
    partial = StateView(
        'stock.shipment.assign.partial',
        'stock.shipment_assign_partial_view_form', [
            Button("Cancel", 'cancel', 'tryton-cancel'),
            Button("Wait", 'end', 'tryton-ok', True),
            Button("Ignore", 'ignore', 'tryton-forward'),
            Button("Force", 'force', 'tryton-forward',
                states={
                    'invisible': ~Id('stock',
                        'group_stock_force_assignment').in_(
                        Eval('context', {}).get('groups', [])),
                }),
            ])
    cancel = StateTransition()
    force = StateTransition()
    ignore = StateTransition()

    def transition_start(self):
        if self.record.assign_try():
            return 'end'
        else:
            return 'partial'

    def default_partial(self, fields):
        values = {}
        if 'moves' in fields:
            values['moves'] = [
                m.id for m in self.record.assign_moves
                if m.state in {'staging', 'draft'}]
        return values

    def transition_cancel(self):
        self.record.assign_reset()
        return 'end'

    def transition_force(self):
        self.record.assign_force()
        return 'end'

    def transition_ignore(self):
        self.record.assign_ignore()
        return 'end'


class AssignPartial(ModelView):
    "Assign Shipment"
    __name__ = 'stock.shipment.assign.partial'
    moves = fields.Many2Many(
        'stock.move', None, None, "Moves", readonly=True,
        help="The moves that were not assigned.")


class DeliveryNote(CompanyReport):
    'Delivery Note'
    __name__ = 'stock.shipment.out.delivery_note'

    @classmethod
    def execute(cls, ids, data):
        with Transaction().set_context(address_with_party=True):
            return super(DeliveryNote, cls).execute(ids, data)


class ShipmentReport(CompanyReport):

    @classmethod
    def execute(cls, ids, data):
        with Transaction().set_context(address_with_party=True):
            return super(ShipmentReport, cls).execute(ids, data)

    @classmethod
    def moves(cls, shipment):
        raise NotImplementedError

    @classmethod
    def get_context(cls, shipments, header, data):
        report_context = super().get_context(shipments, header, data)

        compare_context = cls.get_compare_context(shipments, data)
        sorted_moves = {}
        for shipment in shipments:
            sorted_moves[shipment.id] = sorted(
                cls.moves(shipment),
                key=functools.partial(cls.get_compare_key, compare_context))
        report_context['moves'] = sorted_moves

        return report_context

    @classmethod
    def get_compare_context(cls, shipments, data):
        from_location_ids = set()
        to_location_ids = set()
        for shipment in shipments:
            for move in cls.moves(shipment):
                from_location_ids.add(move.from_location.id)
                to_location_ids.add(move.to_location.id)

        return {
            'from_location_ids': list(from_location_ids),
            'to_location_ids': list(to_location_ids),
            }

    @staticmethod
    def get_compare_key(compare_context, move):
        from_location_ids = compare_context['from_location_ids']
        to_location_ids = compare_context['to_location_ids']
        return [from_location_ids.index(move.from_location.id),
                to_location_ids.index(move.to_location.id)]


class PickingList(ShipmentReport):
    'Picking List'
    __name__ = 'stock.shipment.out.picking_list'

    @classmethod
    def moves(cls, shipment):
        if shipment.warehouse_storage == shipment.warehouse_output:
            return shipment.outgoing_moves
        else:
            return shipment.inventory_moves


class SupplierRestockingList(ShipmentReport):
    'Supplier Restocking List'
    __name__ = 'stock.shipment.in.restocking_list'

    @classmethod
    def moves(cls, shipment):
        if shipment.warehouse_input == shipment.warehouse_storage:
            return shipment.incoming_moves
        else:
            return shipment.inventory_moves


class CustomerReturnRestockingList(ShipmentReport):
    'Customer Return Restocking List'
    __name__ = 'stock.shipment.out.return.restocking_list'

    @classmethod
    def moves(cls, shipment):
        if shipment.warehouse_input == shipment.warehouse_storage:
            return shipment.incoming_moves
        else:
            return shipment.inventory_moves


class InteralShipmentReport(ShipmentReport):
    'Interal Shipment Report'
    __name__ = 'stock.shipment.internal.report'

    @classmethod
    def moves(cls, shipment):
        if shipment.transit_location:
            if shipment.state == 'shipped':
                return shipment.incoming_moves
            else:
                return shipment.outgoing_moves
        else:
            return shipment.moves
