# Copyright (c) 2015, Frappe Technologies Pvt. Ltd. and Contributors
# License: GNU General Public License v3. See license.txt


import frappe
from frappe.model.document import Document
from frappe.query_builder import Case
from frappe.query_builder.functions import Coalesce, Sum
from frappe.utils import flt


class Bin(Document):
	def before_save(self):
		if self.get("__islocal") or not self.stock_uom:
			self.stock_uom = frappe.get_cached_value('Item', self.item_code, 'stock_uom')
		self.set_projected_qty()

	def set_projected_qty(self):
		self.projected_qty = (flt(self.actual_qty) + flt(self.ordered_qty)
			+ flt(self.indented_qty) + flt(self.planned_qty) - flt(self.reserved_qty)
			- flt(self.reserved_qty_for_production) - flt(self.reserved_qty_for_sub_contract))

	def get_first_sle(self):
		sle = frappe.qb.DocType("Stock Ledger Entry")
		first_sle = (
				frappe.qb.from_(sle)
					.select("*")
					.where((sle.item_code == self.item_code) & (sle.warehouse == self.warehouse))
					.orderby(sle.posting_date, sle.posting_time, sle.creation)
					.limit(1)
				).run(as_dict=True)

		return first_sle and first_sle[0] or None

	def update_reserved_qty_for_production(self):
		'''Update qty reserved for production from Production Item tables
			in open work orders'''

		wo = frappe.qb.DocType("Work Order")
		wo_item = frappe.qb.DocType("Work Order Item")

		self.reserved_qty_for_production = (
				frappe.qb
					.from_(wo)
					.from_(wo_item)
					.select(Case()
							.when(wo.skip_transfer == 0, Sum(wo_item.required_qty - wo_item.transferred_qty))
							.else_(Sum(wo_item.required_qty - wo_item.consumed_qty))
						)
					.where(
						(wo_item.item_code == self.item_code)
						& (wo_item.parent == wo.name)
						& (wo.docstatus == 1)
						& (wo_item.source_warehouse == self.warehouse)
						& (wo.status.notin(["Stopped", "Completed"]))
						& ((wo_item.required_qty > wo_item.transferred_qty)
							| (wo_item.required_qty > wo_item.consumed_qty))
					)
		).run()[0][0] or 0.0

		self.set_projected_qty()

		self.db_set('reserved_qty_for_production', flt(self.reserved_qty_for_production))
		self.db_set('projected_qty', self.projected_qty)

	def update_reserved_qty_for_sub_contracting(self):
		#reserved qty

		po = frappe.qb.DocType("Purchase Order")
		supplied_item = frappe.qb.DocType("Purchase Order Item Supplied")

		reserved_qty_for_sub_contract = (
				frappe.qb
					.from_(po)
					.from_(supplied_item)
					.select(Sum(Coalesce(supplied_item.required_qty, 0)))
					.where(
						(supplied_item.rm_item_code == self.item_code)
						& (po.name == supplied_item.parent)
						& (po.docstatus == 1)
						& (po.is_subcontracted == "Yes")
						& (po.status != "Closed")
						& (po.per_received < 100)
						& (supplied_item.reserve_warehouse == self.warehouse)
					)
				).run()[0][0] or 0.0

		se = frappe.qb.DocType("Stock Entry")
		se_item = frappe.qb.DocType("Stock Entry Detail")

		materials_transferred = (
				frappe.qb
					.from_(se)
					.from_(se_item)
					.from_(po)
					.select(Sum(
						Case()
							.when(se.is_return == 1, se_item.transfer_qty * -1)
							.else_(se_item.transfer_qty)
						))
					.where(
						(se.docstatus == 1)
						& (se.purpose == "Send to Subcontractor")
						& (Coalesce(se.purchase_order, "") != "")
						& ((se_item.item_code == self.item_code)
							| (se_item.original_item == self.item_code))
						& (se.name == se_item.parent)
						& (po.name == se.purchase_order)
						& (po.docstatus == 1)
						& (po.is_subcontracted == "Yes")
						& (po.status != "Closed")
						& (po.per_received < 100)
					)
				).run()[0][0] or 0.0

		if reserved_qty_for_sub_contract > materials_transferred:
			reserved_qty_for_sub_contract = reserved_qty_for_sub_contract - materials_transferred
		else:
			reserved_qty_for_sub_contract = 0

		self.db_set('reserved_qty_for_sub_contract', reserved_qty_for_sub_contract)
		self.set_projected_qty()
		self.db_set('projected_qty', self.projected_qty)

def on_doctype_update():
	frappe.db.add_index("Bin", ["item_code", "warehouse"])


def update_stock(bin_name, args, allow_negative_stock=False, via_landed_cost_voucher=False):
	"""WARNING: This function is deprecated. Inline this function instead of using it."""
	from erpnext.stock.stock_ledger import repost_current_voucher

	update_qty(bin_name, args)
	repost_current_voucher(args, allow_negative_stock, via_landed_cost_voucher)

def get_bin_details(bin_name):
	return frappe.db.get_value('Bin', bin_name, ['actual_qty', 'ordered_qty',
	'reserved_qty', 'indented_qty', 'planned_qty', 'reserved_qty_for_production',
	'reserved_qty_for_sub_contract'], as_dict=1)

def update_qty(bin_name, args):
	bin_details = get_bin_details(bin_name)

	# update the stock values (for current quantities)
	if args.get("voucher_type")=="Stock Reconciliation":
		actual_qty = args.get('qty_after_transaction')
	else:
		actual_qty = bin_details.actual_qty + flt(args.get("actual_qty"))

	ordered_qty = flt(bin_details.ordered_qty) + flt(args.get("ordered_qty"))
	reserved_qty = flt(bin_details.reserved_qty) + flt(args.get("reserved_qty"))
	indented_qty = flt(bin_details.indented_qty) + flt(args.get("indented_qty"))
	planned_qty = flt(bin_details.planned_qty) + flt(args.get("planned_qty"))


	# compute projected qty
	projected_qty = (flt(actual_qty) + flt(ordered_qty)
		+ flt(indented_qty) + flt(planned_qty) - flt(reserved_qty)
		- flt(bin_details.reserved_qty_for_production) - flt(bin_details.reserved_qty_for_sub_contract))

	frappe.db.set_value('Bin', bin_name, {
		'actual_qty': actual_qty,
		'ordered_qty': ordered_qty,
		'reserved_qty': reserved_qty,
		'indented_qty': indented_qty,
		'planned_qty': planned_qty,
		'projected_qty': projected_qty
	})
