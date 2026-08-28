"""Períodos, ejercicio fiscal y distribución temporal.

Regla del spec (doc 01 §23, doc 02 §14):
    La frecuencia determina la distribución interna.
    Un valor cargado con frecuencia menor se distribuye equitativamente
    entre los períodos mensuales que abarca. No existe distribución manual.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from enum import Enum
from typing import Iterator


class Frequency(str, Enum):
    MONTHLY = "MONTHLY"
    QUARTERLY = "QUARTERLY"
    SEMIANNUAL = "SEMIANNUAL"
    ANNUAL = "ANNUAL"

    @property
    def months(self) -> int:
        return {
            Frequency.MONTHLY: 1,
            Frequency.QUARTERLY: 3,
            Frequency.SEMIANNUAL: 6,
            Frequency.ANNUAL: 12,
        }[self]


@dataclass(frozen=True, order=True)
class Period:
    """Un mes calendario. Es el grano mínimo del sistema."""

    year: int
    month: int

    @staticmethod
    def parse(value: str) -> "Period":
        y, m = value.split("-")
        return Period(int(y), int(m))

    @staticmethod
    def of(d: date) -> "Period":
        return Period(d.year, d.month)

    @property
    def code(self) -> str:
        return f"{self.year:04d}-{self.month:02d}"

    @property
    def index(self) -> int:
        return self.year * 12 + (self.month - 1)

    @property
    def first_day(self) -> date:
        return date(self.year, self.month, 1)

    @property
    def last_day(self) -> date:
        if self.month == 12:
            return date(self.year, 12, 31)
        return date(self.year, self.month + 1, 1).replace(day=1) - _ONE_DAY

    def shift(self, n: int) -> "Period":
        i = self.index + n
        return Period(i // 12, i % 12 + 1)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.code


from datetime import timedelta as _timedelta  # noqa: E402

_ONE_DAY = _timedelta(days=1)


@dataclass(frozen=True)
class FiscalYear:
    """El ejercicio puede empezar y terminar en cualquier fecha válida (doc 01 §3)."""

    start: date
    end: date

    def __post_init__(self) -> None:
        if self.end <= self.start:
            raise ValueError("fiscal_year_end debe ser posterior a fiscal_year_start")

    @property
    def periods(self) -> list[Period]:
        out: list[Period] = []
        p = Period.of(self.start)
        last = Period.of(self.end)
        while p <= last:
            out.append(p)
            p = p.shift(1)
        return out

    @property
    def opening_balance_date(self) -> date:
        """Doc 02 §33: el balance inicial es el día anterior al inicio del ejercicio."""
        return self.start - _ONE_DAY
    def buckets(self, freq: Frequency) -> list[list[Period]]:
        """Agrupa los períodos del ejercicio según la frecuencia de carga.

        El último bucket puede quedar incompleto si el ejercicio no es múltiplo
        exacto de la frecuencia; se distribuye entre los meses que realmente existen.
        """
        ps = self.periods
        n = freq.months
        return [ps[i : i + n] for i in range(0, len(ps), n)]

    def bucket_for(self, p: Period, freq: Frequency) -> list[Period]:
        for b in self.buckets(freq):
            if p in b:
                return b
        raise ValueError(f"período {p} fuera del ejercicio")

    def bucket_head(self, p: Period, freq: Frequency) -> Period:
        """Período en el que se carga el valor para la frecuencia dada."""
        return self.bucket_for(p, freq)[0]

    def iter_buckets(self, freq: Frequency) -> Iterator[tuple[Period, list[Period]]]:
        for b in self.buckets(freq):
            yield b[0], b


def spread(amount: Decimal, months: list[Period]) -> dict[Period, Decimal]:
    """Distribución equitativa con corrección de redondeo en el último mes.

    La suma de los valores distribuidos es exactamente igual al monto original;
    no se pierden centavos por redondeo (importante para que el P&L cierre).
    """
    if not months:
        return {}
    n = len(months)
    each = (amount / n).quantize(Decimal("0.01"))
    out = {m: each for m in months}
    out[months[-1]] = amount - each * (n - 1)
    return out
