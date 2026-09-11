#!/usr/bin/env bash
# Build a throwaway repo with a realistic PR and triage it end to end.
#
# The PR deliberately contains one of each interesting case:
#   1. a well-tested behavioural change   -> VERIFIED
#   2. a declared refactor that holds     -> VERIFIED (by equivalence)
#   3. a declared refactor that does not  -> RESIDUAL (equivalence counterexample)
#   4. covered code with weak assertions  -> RESIDUAL (mutants survive)
#   5. code with no tests at all          -> RESIDUAL (uncovered)
#   6. a README edit                      -> EXEMPT
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
WORK="${1:-$(mktemp -d)/cart}"
rm -rf "$WORK"; mkdir -p "$WORK/tests"
cd "$WORK"
git init -q . && git config user.email demo@example.com && git config user.name Demo

cat > cart.py <<'PY'
"""A tiny shopping cart, at the base commit."""


def subtotal(prices: list[float]) -> float:
    total = 0.0
    for price in prices:
        total = total + price
    return total


def apply_discount(total: float, pct: float) -> float:
    if pct < 0.0 or pct > 100.0:
        raise ValueError("percentage out of range")
    return total * (1.0 - pct / 100.0)
PY

cat > tests/test_cart.py <<'PY'
import pytest

from cart import apply_discount, subtotal


def test_subtotal():
    assert subtotal([1.0, 2.0, 3.5]) == 6.5
    assert subtotal([]) == 0.0


def test_apply_discount():
    assert apply_discount(200.0, 10.0) == 180.0
    assert apply_discount(50.0, 0.0) == 50.0


def test_apply_discount_rejects_bad_pct():
    with pytest.raises(ValueError):
        apply_discount(10.0, -1.0)
    with pytest.raises(ValueError):
        apply_discount(10.0, 101.0)
PY

cat > README.md <<'MD'
# cart
A tiny shopping cart.
MD
cat > .triage.toml <<'TOML'
[triage]
test_command = ["pytest", "-q", "--no-header", "-p", "no:cacheprovider"]
source_roots = ["."]
test_paths = ["tests"]
mutation_budget_seconds = 120.0
max_mutants_per_hunk = 8
TOML
git add -A && git commit -qm "initial cart"
git branch -M main

# ---- the pull request ------------------------------------------------------
git checkout -qb feature

python3 - <<'PY'
import pathlib
p = pathlib.Path("cart.py")
p.write_text(p.read_text().replace(
'''def subtotal(prices: list[float]) -> float:
    total = 0.0
    for price in prices:
        total = total + price
    return total''',
'''def subtotal(prices: list[float]) -> float:
    return sum(prices)'''))
PY
git commit -qam "refactor: use the builtin sum in subtotal"

python3 - <<'PY'
import pathlib
p = pathlib.Path("cart.py")
p.write_text(p.read_text().replace(
'''    if pct < 0.0 or pct > 100.0:''',
'''    if pct < 0.0 or pct >= 100.0:'''))
PY
git commit -qam "refactor: tighten the discount guard"

cat >> cart.py <<'PY'


def shipping_fee(weight_kg: float, express: bool) -> float:
    """Covered by a test, but nothing asserts on the numbers."""
    base = 4.99 if weight_kg < 2.0 else 9.99
    if express:
        base = base * 2.0
    return base


def loyalty_points(total: float, tier: int) -> int:
    """No test touches this at all."""
    multiplier = 1 + tier
    return int(total * multiplier // 10)


def tax(amount: float, rate_pct: float) -> float:
    """Tested properly: exact values, the boundary, and the error path."""
    if amount < 0.0:
        raise ValueError("amount must not be negative")
    return round(amount * rate_pct / 100.0, 2)
PY
cat >> tests/test_cart.py <<'PY'


def test_shipping_fee_returns_a_number():
    assert isinstance(shipping_fee(1.0, False), float)
    assert shipping_fee(5.0, True) > 0


def test_tax_exact_values():
    assert tax(100.0, 20.0) == 20.0
    assert tax(0.0, 20.0) == 0.0
    assert tax(19.99, 7.5) == 1.5


def test_tax_rounds_to_cents():
    assert tax(10.0, 3.333) == 0.33


def test_tax_rejects_negative_amounts():
    with pytest.raises(ValueError):
        tax(-0.01, 10.0)
PY
python3 - <<'PY'
import pathlib
p = pathlib.Path("tests/test_cart.py")
p.write_text(p.read_text().replace(
    "from cart import apply_discount, subtotal",
    "from cart import apply_discount, shipping_fee, subtotal, tax"))
PY
printf '\nShipping is charged per order.\n' >> README.md
git add -A && git commit -qm "feat: shipping fees and loyalty points"

echo "=== demo repo: $WORK ==="
cd "$WORK" && PYTHONPATH="$HERE" python3 -m triage.cli run --repo "$WORK" --base main --format "${FORMAT:-text}"
