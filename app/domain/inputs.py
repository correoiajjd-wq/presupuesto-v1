"""Inputs: los valores que efectivamente cargan las personas.

Doc 03 §8/§40: INPUT y CALCULATED nunca se mezclan. Un valor calculado
no se guarda como input ni se puede editar.
"""
from __future__ import annotations

from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Concept(str, Enum):
    SALES_QTY = "SALES_QTY"                  # modalidad por unidades
    SALES_AMOUNT = "SALES_AMOUNT"            # modalidad por monto
    EXPENSE_AMOUNT = "EXPENSE_AMOUNT"
    INITIAL_HEADCOUNT = "INITIAL_HEADCOUNT"
    HEADCOUNT_CHANGE = "HEADCOUNT_CHANGE"
    COMMISSION_AMOUNT = "COMMISSION_AMOUNT"
    CAPEX_AMOUNT = "CAPEX_AMOUNT"
    OPENING_STOCK = "OPENING_STOCK"
    PURCHASES = "PURCHASES"
    BALANCE_OPENING = "BALANCE_OPENING"
    BALANCE_PROJECTED = "BALANCE_PROJECTED"


#: A qué "concepto de carga" pertenece cada input, para workflow y obligatoriedad.
CONCEPT_GROUP: dict[Concept, str] = {
    Concept.SALES_QTY: "SALES",
    Concept.SALES_AMOUNT: "SALES",
    Concept.EXPENSE_AMOUNT: "EXPENSES",
    Concept.INITIAL_HEADCOUNT: "PAYROLL_HEADCOUNT",
    Concept.HEADCOUNT_CHANGE: "PAYROLL_HEADCOUNT",
    Concept.COMMISSION_AMOUNT: "PAYROLL_HEADCOUNT",
    Concept.CAPEX_AMOUNT: "CAPEX",
    Concept.OPENING_STOCK: "OPENING_STOCK",
    Concept.PURCHASES: "PURCHASES",
    Concept.BALANCE_OPENING: "BALANCE",
    Concept.BALANCE_PROJECTED: "BALANCE",
}


class ChangeType(str, Enum):
    HIRED = "HIRED"
    TERMINATED = "TERMINATED"


class InputStatus(str, Enum):
    DRAFT = "DRAFT"
    SUBMITTED = "SUBMITTED"
    IN_REVIEW = "IN_REVIEW"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class InputSource(str, Enum):
    MANUAL = "MANUAL"
    IMPORT = "IMPORT"
    SCENARIO = "SCENARIO"


class InputValue(BaseModel):
    """Un valor cargado. Inmutable en la práctica: se versiona, no se pisa."""

    concept: Concept
    period: Optional[str] = None          # 'YYYY-MM' — período de CARGA (cabeza del bucket)
    value: Decimal = Decimal(0)
    currency: Optional[str] = None

    business_unit_id: Optional[str] = None
    branch_id: Optional[str] = None
    operation_id: Optional[str] = None      # combinación unidad x sucursal
    support_unit_id: Optional[str] = None
    cost_center_id: Optional[str] = None
    product_id: Optional[str] = None
    family_id: Optional[str] = None
    expense_id: Optional[str] = None
    area_id: Optional[str] = None
    capex_category_id: Optional[str] = None
    balance_item_id: Optional[str] = None

    change_type: Optional[ChangeType] = None
    effective_date: Optional[date] = None

    status: InputStatus = InputStatus.DRAFT
    source: InputSource = InputSource.MANUAL
    loaded_by: Optional[str] = None
    comment: Optional[str] = None

    @property
    def group(self) -> str:
        return CONCEPT_GROUP[self.concept]

    @property
    def scope_key(self) -> str:
        if self.operation_id:
            return f"OP:{self.operation_id}"
        if self.branch_id:
            return f"BR:{self.branch_id}"
        if self.business_unit_id:
            return f"BU:{self.business_unit_id}"
        if self.cost_center_id:
            return f"CC:{self.cost_center_id}"
        if self.support_unit_id:
            return f"SU:{self.support_unit_id}"
        return "CO"

    def identity(self) -> str:
        """Clave funcional: dos inputs con la misma identidad son el mismo dato."""
        parts = [
            self.concept.value,
            self.scope_key,
            self.product_id or "",
            self.family_id or "",
            self.expense_id or "",
            self.area_id or "",
            self.capex_category_id or "",
            self.balance_item_id or "",
            self.period or "",
            self.effective_date.isoformat() if self.effective_date else "",
            self.change_type.value if self.change_type else "",
        ]
        return "/".join(parts)


class InputSet(BaseModel):
    """Colección de inputs de una versión."""

    values: list[InputValue] = Field(default_factory=list)

    def add(self, iv: InputValue) -> "InputSet":
        self.values.append(iv)
        return self

    def of(self, *concepts: Concept) -> list[InputValue]:
        cs = set(concepts)
        return [v for v in self.values if v.concept in cs]

    def upsert(self, iv: InputValue) -> None:
        key = iv.identity()
        for i, existing in enumerate(self.values):
            if existing.identity() == key:
                self.values[i] = iv
                return
        self.values.append(iv)
