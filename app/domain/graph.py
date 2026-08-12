"""Dependency Graph + evaluación topológica + impact analysis.

Doc 01 §43 y doc 03 §41/§43: cada cálculo conoce sus dependencias, se
recalcula sólo lo afectado y se puede explicar de dónde salió un número.

Un nodo es la unidad mínima de cálculo:  MÉTRICA | ÁMBITO | PERÍODO
Ej:  EBITDA|BR:BR-01|2027-03
"""
from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Callable, Iterable, Optional


class CycleError(Exception):
    pass


@dataclass
class Node:
    key: str
    deps: tuple[str, ...]
    fn: Callable[[dict[str, Optional[Decimal]]], Optional[Decimal]]
    kind: str = "CALCULATED"   # INPUT | CALCULATED
    meta: dict = field(default_factory=dict)


class DependencyGraph:
    def __init__(self) -> None:
        self.nodes: dict[str, Node] = {}
        self._dependents: dict[str, set[str]] = defaultdict(set)
        self._order: Optional[list[str]] = None

    # -- construcción ------------------------------------------------------
    def add(self, node: Node) -> None:
        if node.key in self.nodes:
            raise ValueError(f"nodo duplicado: {node.key}")
        self.nodes[node.key] = node
        for d in node.deps:
            self._dependents[d].add(node.key)
        self._order = None

    def constant(self, key: str, value: Optional[Decimal], kind: str = "INPUT", **meta) -> None:
        self.add(Node(key, (), lambda _v, value=value: value, kind=kind, meta=meta))

    def calc(self, key: str, deps: Iterable[str], fn, **meta) -> None:
        self.add(Node(key, tuple(deps), fn, kind="CALCULATED", meta=meta))

    def has(self, key: str) -> bool:
        return key in self.nodes

    # -- orden topológico --------------------------------------------------
    def topological_order(self) -> list[str]:
        if self._order is not None:
            return self._order
        indeg = {k: 0 for k in self.nodes}
        for k, n in self.nodes.items():
            for d in n.deps:
                if d in self.nodes:
                    indeg[k] += 1
        q = deque(sorted(k for k, v in indeg.items() if v == 0))
        order: list[str] = []
        while q:
            k = q.popleft()
            order.append(k)
            for dep in sorted(self._dependents.get(k, ())):
                indeg[dep] -= 1
                if indeg[dep] == 0:
                    q.append(dep)
        if len(order) != len(self.nodes):
            stuck = [k for k in self.nodes if k not in set(order)]
            raise CycleError(f"ciclo de dependencias detectado: {stuck[:5]}")
        self._order = order
        return order

    # -- evaluación --------------------------------------------------------
    def evaluate(self, only: Optional[set[str]] = None,
                 previous: Optional[dict[str, Optional[Decimal]]] = None
                 ) -> dict[str, Optional[Decimal]]:
        """Evalúa el grafo. Con `only` recalcula sólo esos nodos (recálculo incremental)."""
        values: dict[str, Optional[Decimal]] = dict(previous or {})
        for key in self.topological_order():
            if only is not None and key not in only:
                continue
            node = self.nodes[key]
            values[key] = node.fn(values)
        return values

    # -- impacto -----------------------------------------------------------
    def impacted_by(self, changed: Iterable[str]) -> set[str]:
        """Clausura hacia adelante: qué nodos hay que recalcular (doc 04 §48)."""
        out: set[str] = set()
        stack = list(changed)
        while stack:
            k = stack.pop()
            for dep in self._dependents.get(k, ()):
                if dep not in out:
                    out.add(dep)
                    stack.append(dep)
        return out | {c for c in changed if c in self.nodes}

    def explain(self, key: str, values: dict[str, Optional[Decimal]], depth: int = 2) -> dict:
        """Devuelve el árbol de dependencias con valores: por qué un número es lo que es."""
        if key not in self.nodes:
            return {"key": key, "value": None, "missing": True}
        node = self.nodes[key]
        out = {
            "key": key,
            "value": values.get(key),
            "kind": node.kind,
            "meta": node.meta,
        }
        if depth > 0 and node.deps:
            out["depends_on"] = [self.explain(d, values, depth - 1) for d in node.deps]
        elif node.deps:
            out["depends_on_keys"] = list(node.deps)
        return out

    def stats(self) -> dict:
        return {
            "nodes": len(self.nodes),
            "inputs": sum(1 for n in self.nodes.values() if n.kind == "INPUT"),
            "calculated": sum(1 for n in self.nodes.values() if n.kind == "CALCULATED"),
            "edges": sum(len(n.deps) for n in self.nodes.values()),
        }


# -- helpers de claves ------------------------------------------------------
def nk(metric: str, scope: str, period) -> str:
    return f"{metric}|{scope}|{period}"


def parse(key: str) -> tuple[str, str, str]:
    metric, scope, period = key.split("|", 2)
    return metric, scope, period


def sum_of(keys: Iterable[str]):
    """Suma tratando faltantes como no aportantes, pero devolviendo None si no hay ninguno."""
    keys = list(keys)

    def _fn(v: dict[str, Optional[Decimal]]) -> Optional[Decimal]:
        total = Decimal(0)
        found = False
        for k in keys:
            x = v.get(k)
            if x is not None:
                total += x
                found = True
        return total if found or not keys else None

    return _fn
