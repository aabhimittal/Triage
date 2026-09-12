#!/usr/bin/env bash
# Build a throwaway repo with a realistic PR and triage it end to end.
#
#   bash scripts/demo.sh [mixed|mature] [workdir]
#
# Two scenarios, because one number in isolation is misleading. TRIAGE's
# headline percentage is a property of the *test suite* it is pointed at, not
# of TRIAGE and not of the diff. The same tool reports 60% on a half-tested
# change and 90%+ on a disciplined one, and you have to see both to read either.
#
#   mixed   - one of every interesting case; the tool's behaviour under stress
#   mature  - a well-tested change; the actual value proposition, where the
#             residual set is small enough that reviewing it is cheap
#   history - two real bugs, each introduced in one commit and fixed in a
#             later one, for `triage bugs` to rediscover by blame. One is the
#             kind TRIAGE catches; one is the kind it provably cannot.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SCENARIO="${1:-mixed}"
WORK="${2:-$(mktemp -d)/demo}"
rm -rf "$WORK"; mkdir -p "$WORK/tests"
cd "$WORK"
git init -q . && git config user.email demo@example.com && git config user.name Demo

write_config() {
  cat > .triage.toml <<'TOML'
[triage]
test_command = ["pytest", "-q", "--no-header", "-p", "no:cacheprovider"]
source_roots = ["."]
test_paths = ["tests"]
mutation_budget_seconds = 120.0
max_mutants_per_hunk = 8
TOML
}

if [ "$SCENARIO" = "mixed" ]; then
  # ---------------------------------------------------------------- base ----
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
  write_config
  git add -A && git commit -qm "initial cart"
  git branch -M main

  # ------------------------------------------------------------------ PR ----
  git checkout -qb feature

  # 1. A refactor that holds: verified by equivalence.
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

  # 2. A refactor that does not hold: caught with a counterexample.
  python3 - <<'PY'
import pathlib
p = pathlib.Path("cart.py")
p.write_text(p.read_text().replace(
    "    if pct < 0.0 or pct > 100.0:",
    "    if pct < 0.0 or pct >= 100.0:"))
PY
  git commit -qam "refactor: tighten the discount guard"

  # 3-5. Weakly tested code, untested code, and properly tested code.
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


def average_price(prices: list[float]) -> float:
    """7. Fully covered, every mutant killed, and it dies on an empty cart.

    No test-adequacy signal can see this. Only running the code can.
    """
    return sum(prices) / len(prices)
PY
  cat >> tests/test_cart.py <<'PY'


def test_shipping_fee_returns_a_number():
    assert isinstance(shipping_fee(1.0, False), float)
    assert shipping_fee(5.0, True) > 0


def test_subtotal_is_callable():
    # 6. A new test that asserts nothing this PR changed. It passes just as
    #    happily against the old implementation, so it is not evidence.
    assert callable(subtotal)


def test_tax_exact_values():
    assert tax(100.0, 20.0) == 20.0
    assert tax(0.0, 20.0) == 0.0
    assert tax(19.99, 7.5) == 1.5


def test_tax_rounds_to_cents():
    assert tax(10.0, 3.333) == 0.33


def test_tax_rejects_negative_amounts():
    with pytest.raises(ValueError):
        tax(-0.01, 10.0)


def test_average_price():
    assert average_price([2.0, 4.0]) == 3.0
    assert average_price([5.0]) == 5.0
    assert average_price([1.0, 2.0, 6.0]) == 3.0
PY
  python3 - <<'PY'
import pathlib
p = pathlib.Path("tests/test_cart.py")
p.write_text(p.read_text().replace(
    "from cart import apply_discount, subtotal",
    "from cart import apply_discount, average_price, shipping_fee, subtotal, tax"))
PY
  printf '\nShipping is charged per order.\n' >> README.md
  git add -A && git commit -qm "feat: shipping fees, loyalty points and tax"

elif [ "$SCENARIO" = "history" ]; then
  # ---------------------------------------------------------------- base ----
  cat > sched.py <<'PY'
"""A scheduling helper, at the base commit."""


def clamp(value: int, low: int, high: int) -> int:
    if value < low:
        return low
    if value > high:
        return high
    return value
PY
  cat > tests/test_sched.py <<'PY'
from sched import clamp


def test_clamp():
    assert clamp(5, 0, 10) == 5
    assert clamp(-1, 0, 10) == 0
    assert clamp(11, 0, 10) == 10
PY
  write_config
  git add -A && git commit -qm "initial scheduler"
  git branch -M main

  # --- BUG 1: untested code. TRIAGE should route this to a human. ----------
  cat >> sched.py <<'PY'


def slots_needed(items: int, per_slot: int) -> int:
    """How many slots to fit every item. Off by one when it divides evenly."""
    return items // per_slot + 1
PY
  git add -A && git commit -qm "feat: compute how many slots a batch needs"

  # --- BUG 2: thoroughly tested, and the tests encode the wrong rule. -------
  #     Mutation testing kills every mutant here, coverage is total, and the
  #     code still does the wrong thing. This is the class of defect no
  #     test-adequacy signal can see.
  cat >> sched.py <<'PY'


def overtime_pay(hours: int, rate: int) -> int:
    """Overtime is paid above 40 hours a week, at time and a half."""
    if hours <= 40:
        return hours * rate
    return 40 * rate + (hours - 40) * rate * 2
PY
  cat >> tests/test_sched.py <<'PY'


def test_overtime_pay():
    assert overtime_pay(40, 10) == 400
    assert overtime_pay(0, 10) == 0
    assert overtime_pay(41, 10) == 420
    assert overtime_pay(50, 10) == 600
PY
  python3 - <<'PY'
import pathlib
p = pathlib.Path("tests/test_sched.py")
p.write_text(p.read_text().replace(
    "from sched import clamp", "from sched import clamp, overtime_pay"))
PY
  git add -A && git commit -qm "feat: overtime pay"

  # --- the fixes, later ----------------------------------------------------
  python3 - <<'PY'
import pathlib
p = pathlib.Path("sched.py")
p.write_text(p.read_text().replace(
    "    return items // per_slot + 1",
    "    return -(-items // per_slot)"))
PY
  cat >> tests/test_sched.py <<'PY'


def test_slots_needed():
    from sched import slots_needed
    assert slots_needed(10, 5) == 2
    assert slots_needed(11, 5) == 3
    assert slots_needed(0, 5) == 0
PY
  git add -A && git commit -qm "fix: slots_needed was off by one on exact multiples"

  python3 - <<'PY'
import pathlib
p = pathlib.Path("sched.py")
p.write_text(p.read_text().replace(
    "    return 40 * rate + (hours - 40) * rate * 2",
    "    return 40 * rate + (hours - 40) * rate * 3 // 2"))
p = pathlib.Path("tests/test_sched.py")
p.write_text(p.read_text()
    .replace("assert overtime_pay(41, 10) == 420", "assert overtime_pay(41, 10) == 415")
    .replace("assert overtime_pay(50, 10) == 600", "assert overtime_pay(50, 10) == 550"))
PY
  git add -A && git commit -qm "fix: overtime was paid at double time, not time and a half"

  echo "=== demo repo ($SCENARIO): $WORK ==="
  cd "$WORK" && PYTHONPATH="$HERE" python3 -m triage.cli bugs --repo "$WORK" --max-bugs 4
  exit 0

else
  # ---------------------------------------------------------------- base ----
  cat > money.py <<'PY'
"""Money handling for a team that writes tests properly."""


def parse_cents(amount: str) -> int:
    if not amount:
        raise ValueError("empty amount")
    negative = amount.startswith("-")
    digits = amount[1:] if negative else amount
    if "." not in digits:
        cents = int(digits) * 100
    else:
        whole, _, frac = digits.partition(".")
        if len(frac) != 2:
            raise ValueError("expected two decimal places")
        cents = int(whole) * 100 + int(frac)
    return -cents if negative else cents


def format_cents(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"
PY
  cat > tests/test_money.py <<'PY'
import pytest

from money import format_cents, parse_cents


def test_parse_whole_amounts():
    assert parse_cents("5") == 500
    assert parse_cents("0") == 0
    assert parse_cents("-3") == -300


def test_parse_decimal_amounts():
    assert parse_cents("5.00") == 500
    assert parse_cents("0.07") == 7
    assert parse_cents("-1.25") == -125


def test_parse_rejects_bad_input():
    with pytest.raises(ValueError):
        parse_cents("")
    with pytest.raises(ValueError):
        parse_cents("1.5")


def test_format():
    assert format_cents(0) == "0.00"
    assert format_cents(7) == "0.07"
    assert format_cents(-125) == "-1.25"
PY
  cat > README.md <<'MD'
# money
Integer-cent money handling.
MD
  write_config
  git add -A && git commit -qm "initial money"
  git branch -M main

  # ------------------------------------------------------------------ PR ----
  git checkout -qb feature
  python3 - <<'PY'
import pathlib
p = pathlib.Path("money.py")
p.write_text(p.read_text().replace(
'''    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"''',
'''    sign = "-" if cents < 0 else ""
    whole, remainder = divmod(abs(cents), 100)
    return f"{sign}{whole}.{remainder:02d}"'''))
PY
  git commit -qam "refactor: use divmod in format_cents"

  cat >> money.py <<'PY'


def split_evenly(cents: int, ways: int) -> list[int]:
    """Split an amount so the parts sum exactly back to the whole."""
    if cents < 0:
        raise ValueError("amount must not be negative")
    if ways <= 0:
        raise ValueError("ways must be positive")
    base, remainder = divmod(cents, ways)
    return [base + (1 if i < remainder else 0) for i in range(ways)]


def add_tax(cents: int, rate_basis_points: int) -> int:
    """Tax in basis points, rounded half up to the nearest cent."""
    if rate_basis_points < 0:
        raise ValueError("negative tax rate")
    return cents + (cents * rate_basis_points + 5000) // 10000
PY
  cat >> tests/test_money.py <<'PY'


def test_split_evenly_is_exact():
    assert split_evenly(100, 4) == [25, 25, 25, 25]
    assert split_evenly(100, 3) == [34, 33, 33]
    assert sum(split_evenly(101, 3)) == 101
    assert split_evenly(0, 2) == [0, 0]


def test_split_evenly_rejects_bad_input():
    with pytest.raises(ValueError):
        split_evenly(-1, 3)
    with pytest.raises(ValueError):
        split_evenly(100, 0)


def test_add_tax_rounds_half_up():
    assert add_tax(1000, 2000) == 1200
    assert add_tax(1, 5000) == 2
    assert add_tax(0, 2000) == 0
    assert add_tax(333, 1000) == 366
    assert add_tax(4999, 1) == 4999


def test_add_tax_accepts_a_zero_rate():
    assert add_tax(100, 0) == 100


def test_add_tax_rejects_negative_rates():
    with pytest.raises(ValueError):
        add_tax(100, -1)
PY
  python3 - <<'PY'
import pathlib
p = pathlib.Path("tests/test_money.py")
p.write_text(p.read_text().replace(
    "from money import format_cents, parse_cents",
    "from money import add_tax, format_cents, parse_cents, split_evenly"))
PY
  printf '\nAmounts split evenly always sum back to the original.\n' >> README.md
  git add -A && git commit -qm "feat: even splitting and tax"
fi

echo "=== demo repo ($SCENARIO): $WORK ==="
cd "$WORK" && PYTHONPATH="$HERE" python3 -m triage.cli run --repo "$WORK" --base main --format "${FORMAT:-text}"
