================================
Stock Shipment Internal Scenario
================================

Imports::

    >>> import datetime
    >>> from dateutil.relativedelta import relativedelta
    >>> from decimal import Decimal
    >>> from proteus import Model, Wizard
    >>> from trytond.tests.tools import activate_modules, set_user
    >>> from trytond.modules.company.tests.tools import create_company, \
    ...     get_company
    >>> today = datetime.date.today()
    >>> yesterday = today - relativedelta(days=1)
    >>> tomorrow = today + relativedelta(days=1)

Activate modules::

    >>> config = activate_modules('stock')

Create company::

    >>> _ = create_company()
    >>> company = get_company()

Create product::

    >>> ProductUom = Model.get('product.uom')
    >>> ProductTemplate = Model.get('product.template')
    >>> Product = Model.get('product.product')
    >>> unit, = ProductUom.find([('name', '=', 'Unit')])
    >>> template = ProductTemplate()
    >>> template.name = 'Product'
    >>> template.default_uom = unit
    >>> template.type = 'goods'
    >>> template.list_price = Decimal('20')
    >>> template.save()
    >>> product, = template.products

Get stock locations::

    >>> Location = Model.get('stock.location')
    >>> lost_found_loc, = Location.find([('type', '=', 'lost_found')])
    >>> storage_loc, = Location.find([('code', '=', 'STO')])
    >>> internal_loc = Location(
    ...     name="Internal", type='storage', parent=storage_loc.parent)
    >>> internal_loc.save()

Create stock user::

    >>> User = Model.get('res.user')
    >>> Group = Model.get('res.group')
    >>> Party = Model.get('party.party')
    >>> Employee = Model.get('company.employee')
    >>> stock_user = User()
    >>> stock_user.name = 'Stock'
    >>> stock_user.login = 'stock'
    >>> stock_group, = Group.find([('name', '=', 'Stock')])
    >>> stock_user.groups.append(stock_group)
    >>> employee_party = Party(name="Employee")
    >>> employee_party.save()
    >>> employee = Employee(party=employee_party)
    >>> employee.save()
    >>> stock_user.employees.append(employee)
    >>> stock_user.employee = employee
    >>> stock_user.save()

Create Internal Shipment::

    >>> set_user(stock_user)
    >>> Shipment = Model.get('stock.shipment.internal')
    >>> StockMove = Model.get('stock.move')
    >>> shipment = Shipment()
    >>> shipment.planned_date = today
    >>> shipment.from_location = internal_loc
    >>> shipment.to_location = storage_loc
    >>> move = shipment.moves.new()
    >>> move.product = product
    >>> move.quantity = 1
    >>> move.from_location = internal_loc
    >>> move.to_location = storage_loc
    >>> move.currency = company.currency
    >>> shipment.save()
    >>> shipment.assigned_by
    >>> shipment.done_by

    >>> shipment.click('wait')
    >>> shipment.state
    'waiting'
    >>> shipment.click('assign_try')
    False
    >>> shipment.assigned_by
    >>> shipment.done_by

Create Internal Shipment from lost_found location::

    >>> lost_found_shipment = Shipment()
    >>> lost_found_shipment.planned_date = today
    >>> lost_found_shipment.company = company
    >>> lost_found_shipment.from_location = lost_found_loc
    >>> lost_found_shipment.to_location = internal_loc
    >>> move = StockMove()
    >>> move = lost_found_shipment.moves.new()
    >>> move.product = product
    >>> move.uom = unit
    >>> move.quantity = 2
    >>> move.from_location = lost_found_loc
    >>> move.to_location = internal_loc
    >>> move.currency = company.currency
    >>> lost_found_shipment.click('wait')
    >>> lost_found_shipment.click('assign_try')
    True
    >>> lost_found_shipment.state
    'assigned'
    >>> lost_found_shipment.click('done')
    >>> lost_found_shipment.state
    'done'

Check that now we can finish the older shipment::

    >>> shipment.click('assign_try')
    True
    >>> shipment.assigned_by == employee
    True
    >>> shipment.done_by

    >>> shipment.click('done')
    >>> shipment.state
    'done'
    >>> shipment.done_by == employee
    True

Duplicate Internal Shipment::

    >>> shipment_copy, = shipment.duplicate()
    >>> len(shipment_copy.moves)
    1

Reschedule shipment::

    >>> shipment_copy.planned_date = yesterday
    >>> shipment_copy.click('wait')
    >>> set_user(1)
    >>> Cron = Model.get('ir.cron')
    >>> cron = Cron(method='stock.shipment.internal|reschedule')
    >>> cron.interval_number = 1
    >>> cron.interval_type = 'months'
    >>> cron.click('run_once')
    >>> shipment_copy.reload()
    >>> shipment_copy.planned_date == today
    True
