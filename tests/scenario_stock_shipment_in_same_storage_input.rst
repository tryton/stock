=================================================
Stock Shipment In Same Storage and Input Scenario
=================================================

Imports::

    >>> from decimal import Decimal
    >>> from proteus import Model
    >>> from trytond.tests.tools import activate_modules
    >>> from trytond.modules.company.tests.tools import create_company, \
    ...     get_company

Activate modules::

    >>> config = activate_modules('stock')

Create company::

    >>> _ = create_company()
    >>> company = get_company()

Create supplier::

    >>> Party = Model.get('party.party')
    >>> supplier = Party(name='Supplier')
    >>> supplier.save()

Create product::

    >>> ProductUom = Model.get('product.uom')
    >>> ProductTemplate = Model.get('product.template')
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
    >>> warehouse_loc, = Location.find([('code', '=', 'WH')])
    >>> supplier_loc, = Location.find([('code', '=', 'SUP')])
    >>> storage_loc, = Location.find([('code', '=', 'STO')])
    >>> storage1 = Location(name="Storage 1", parent=storage_loc)
    >>> storage1.save()

Use storage location as input location::

    >>> warehouse_loc.input_location = storage_loc
    >>> warehouse_loc.save()

Create Shipment In::

    >>> ShipmentIn = Model.get('stock.shipment.in')
    >>> shipment_in = ShipmentIn()
    >>> shipment_in.supplier = supplier
    >>> shipment_in.warehouse = warehouse_loc
    >>> shipment_in.company = company
    >>> move = shipment_in.incoming_moves.new()
    >>> move.product = product
    >>> move.uom = unit
    >>> move.quantity = 1
    >>> move.unit_price = Decimal('5')
    >>> move.currency = company.currency
    >>> move.from_location = supplier_loc
    >>> move.to_location = storage1
    >>> shipment_in.save()

    >>> len(shipment_in.incoming_moves)
    1
    >>> len(shipment_in.inventory_moves)
    0

Incoming moves are done when receiving the shipment::

    >>> shipment_in.click('receive')
    >>> shipment_in.state
    'done'
    >>> move, = shipment_in.incoming_moves
    >>> move.state
    'done'
    >>> len(shipment_in.inventory_moves)
    0
