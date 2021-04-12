# This file is part of Tryton.  The COPYRIGHT file at the top level of
# this repository contains the full copyright notices and license terms.
from trytond.pool import PoolMeta


class Cron(metaclass=PoolMeta):
    __name__ = 'ir.cron'

    @classmethod
    def __setup__(cls):
        super().__setup__()
        cls.method.selection.extend([
                ('product.product|recompute_cost_price_from_moves',
                    "Recompute Cost Price from Moves"),
                ('stock.shipment.out|reschedule',
                    "Reschedule Customer Shipments"),
                ('stock.shipment.in.return|reschedule',
                    "Reschedule Supplier Return Shipments"),
                ('stock.shipment.internal|reschedule',
                    "Reschedule Internal Shipments"),
                ('stock.shipment.out|assign_cron',
                    "Assign Customer Shipments"),
                ('stock.shipment.internal|assign_cron',
                    "Assign Internal Shipments"),
                ])
