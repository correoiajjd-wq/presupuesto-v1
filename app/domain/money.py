"""Money y motor de conversión de monedas.

Reglas del spec (doc 01 §4, doc 04 §13):
    - Todo valor financiero conserva importe + moneda + período.
    - La moneda de presentación funciona como moneda puente (ARS -> USD -> UYU).
    - El CFO carga el TC estimado para cada día del ejercicio.
    - Una versión aprobada congela sus tipos de cambio.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal, ROUND_HALF_UP
from enum import Enum

from .periods import Period

CENTS = Decimal("0.01")


def d(value) -> Decimal:
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def money_round(value: Decimal) -> Decimal:
    return value.quantize(CENTS, rounding=ROUND_HALF_UP)


@dataclass(frozen=True)
class Money:
    amount: Decimal
    currency: str

    @staticmethod
    def of(amount, currency: str) -> "Money":
        return Money(d(amount), currency.upper())

    def __add__(self, other: "Money") -> "Money":
        self._check(other)
        return Money(self.amount + other.amount, self.currency)

    def __sub__(self, other: "Money") -> "Money":
        self._check(other)
        return Money(self.amount - other.amount, self.currency)

    def __mul__(self, factor) -> "Money":
        return Money(self.amount * d(factor), self.currency)

    def _check(self, other: "Money") -> None:
        if self.currency != other.currency:
            raise ValueError(
                f"No se pueden operar monedas distintas sin convertir: "
                f"{self.currency} vs {other.currency}"
            )
class FXConvention(str, Enum):
    """Qué TC usar cuando un valor mensual se convierte."""

    AVERAGE = "AVERAGE"  # flujos (ventas, gastos, nómina): promedio del mes
    CLOSING = "CLOSING"  # stocks (inventario, balance): TC de cierre del período


class FXError(Exception):
    pass


class FXTable:
    """Tabla de tipos de cambio diarios contra la moneda de presentación.

    rate[currency][day] = cuántas unidades de la moneda de presentación
    equivale 1 unidad de `currency`.  Ej: presentación USD, UYU rate 0.0236
    significa 1 UYU = 0.0236 USD.  Se acepta también cargar el TC "directo"
    (1 USD = 42.35 UYU) mediante `add_inverse`.
    """

    def __init__(self, presentation_currency: str, enabled: list[str] | None = None):
        self.presentation = presentation_currency.upper()
        self.enabled = {c.upper() for c in (enabled or [])} | {self.presentation}
        self._rates: dict[str, dict[date, Decimal]] = {}

    # -- carga -------------------------------------------------------------
    def add(self, currency: str, day: date, rate_to_presentation) -> None:
        c = currency.upper()
        if c not in self.enabled:
            raise FXError(f"INVALID_CURRENCY: {c} no está habilitada")
        self._rates.setdefault(c, {})[day] = d(rate_to_presentation)

    def add_inverse(self, currency: str, day: date, units_per_presentation) -> None:
        """Carga el TC expresado como 'cuántas unidades de `currency` vale 1 de presentación'."""
        v = d(units_per_presentation)
        if v == 0:
            raise FXError("INVALID_FX_RATE: 0")
        self.add(currency, day, Decimal(1) / v)

    def add_flat(self, currency: str, start: date, end: date, rate_to_presentation) -> None:
        day = start
        while day <= end:
            self.add(currency, day, rate_to_presentation)
            day += timedelta(days=1)
    # -- consulta ----------------------------------------------------------
    def rate_on(self, currency: str, day: date) -> Decimal:
        c = currency.upper()
        if c == self.presentation:
            return Decimal(1)
        table = self._rates.get(c)
        if not table:
            raise FXError(f"MISSING_FX_RATE: no hay TC cargado para {c}")
        if day in table:
            return table[day]
        earlier = [x for x in table if x <= day]
        if not earlier:
            raise FXError(f"MISSING_FX_RATE: {c} en {day}")
        return table[max(earlier)]

    def rate_for_period(
        self, currency: str, period: Period, convention: FXConvention = FXConvention.AVERAGE
    ) -> Decimal:
        c = currency.upper()
        if c == self.presentation:
            return Decimal(1)
        if convention is FXConvention.CLOSING:
            return self.rate_on(c, period.last_day)
        days = [x for x in self._rates.get(c, {}) if Period.of(x) == period]
        if not days:
            return self.rate_on(c, period.last_day)
        total = sum((self._rates[c][x] for x in days), Decimal(0))
        return total / Decimal(len(days))

    # -- conversión --------------------------------------------------------
    def to_presentation(
        self, m: Money, period: Period, convention: FXConvention = FXConvention.AVERAGE
    ) -> Money:
        rate = self.rate_for_period(m.currency, period, convention)
        return Money(m.amount * rate, self.presentation)

    def convert(
        self,
        m: Money,
        target: str,
        period: Period,
        convention: FXConvention = FXConvention.AVERAGE,
    ) -> Money:
        """Conversión usando la moneda de presentación como puente."""
        target = target.upper()
        if m.currency == target:
            return m
        bridged = self.to_presentation(m, period, convention)
        if target == self.presentation:
            return bridged
        rate = self.rate_for_period(target, period, convention)
        if rate == 0:
            raise FXError(f"INVALID_FX_RATE: {target} = 0")
        return Money(bridged.amount / rate, target)

    def coverage_gaps(self, currencies: list[str], start: date, end: date) -> list[str]:
        """Valida que exista TC para cada día del ejercicio (doc 02 §30)."""
        gaps: list[str] = []
        for c in currencies:
            cu = c.upper()
            if cu == self.presentation:
                continue
            table = self._rates.get(cu, {})
            missing = 0
            day = start
            while day <= end:
                if day not in table:
                    missing += 1
                day += timedelta(days=1)
            if missing:
                gaps.append(f"{cu}: faltan {missing} días de TC")
        return gaps
