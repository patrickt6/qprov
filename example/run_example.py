"""End-to-end demo: wrap a q-real computation, record real claims, export
to LaTeX. Produces:
  - example/.qprov/    store with one row per (constant, N) computation
  - example/claims.tex with 7 \\fact{...} macros, each footnoted with its
    provenance computation id

Run from the example/ directory:
    python run_example.py
"""
from __future__ import annotations

import sys
from pathlib import Path

EXAMPLE_DIR = Path(__file__).resolve().parent
STORE_DIR = EXAMPLE_DIR / ".qprov"

# Ensure imports resolve when run from anywhere
sys.path.insert(0, str(EXAMPLE_DIR))

import qprov

qprov.set_store_root(STORE_DIR)

import q_real_python as M
from q_real_demo import q_real_truncated, q_rational_series


def main() -> None:
    print(f"qprov demo running. Store: {STORE_DIR}")

    # 1. compute q-real series for several reference irrationals
    pi_coeffs = q_real_truncated("pi", 100)
    sqrt2_coeffs = q_real_truncated("sqrt(2)", 100)
    phi_coeffs = q_real_truncated("(1+sqrt(5))/2", 30)
    e_coeffs = q_real_truncated("E", 80)

    # 2. and one rational q-number for sanity
    rat_3_2_coeffs = q_rational_series(3, 2, 20)

    # 3. fetch the corresponding computation rows by their input args
    runs = qprov.find(function="q_real_truncated")
    by_input = {tuple(qprov.get_store().read_payload(r.id)["args"]): r.id for r in runs}
    pi_id = by_input[("pi", 100)]
    sqrt2_id = by_input[("sqrt(2)", 100)]
    phi_id = by_input[("(1+sqrt(5))/2", 30)]
    e_id = by_input[("E", 80)]
    rat_id = qprov.find(function="q_rational_series")[0].id

    # 4. record real claims about the computations
    pi_first_nonzero_after_3 = M.first_nonzero_coefficient_index(pi_coeffs[3:]) + 3
    pi_max = M.coefficient_max_abs(pi_coeffs[:50])
    sqrt2_first_neg = M.first_negative_coefficient_index(sqrt2_coeffs)
    sqrt2_max = M.coefficient_max_abs(sqrt2_coeffs[:50])
    phi_max = M.coefficient_max_abs(phi_coeffs[:25])
    e_zeros_in_first_50 = M.number_of_zeros(e_coeffs[:50])
    rat_first_negative = M.first_negative_coefficient_index(rat_3_2_coeffs)

    qprov.claim(
        f"For $[\\pi]_q$, the first nonzero Taylor coefficient at $q^k$ for $k > 2$ "
        f"appears at $k = {pi_first_nonzero_after_3}$, with value "
        f"$c_{{{pi_first_nonzero_after_3}}}([\\pi]_q) = {pi_coeffs[pi_first_nonzero_after_3]}$.",
        computation_id=pi_id,
        value_numeric=pi_first_nonzero_after_3,
        notes="Derived from q_real_truncated('pi', 100) via MGO Prop 1.1.",
    )

    qprov.claim(
        f"The maximum absolute Taylor coefficient of $[\\pi]_q$ over the first 50 "
        f"powers is $\\max_{{k < 50}} |c_k([\\pi]_q)| = {pi_max}$.",
        computation_id=pi_id,
        value_numeric=pi_max,
        notes="Sanity-checks the MGO bound on coefficient growth.",
    )

    qprov.claim(
        f"For $[\\sqrt{{2}}]_q$, the first negative Taylor coefficient appears at "
        f"$k = {sqrt2_first_neg}$ with value $c_{{{sqrt2_first_neg}}}([\\sqrt{{2}}]_q) "
        f"= {sqrt2_coeffs[sqrt2_first_neg]}$.",
        computation_id=sqrt2_id,
        value_numeric=sqrt2_first_neg,
        notes="Demonstrates that q-real coefficients are not eventually monotone.",
    )

    qprov.claim(
        f"For $[\\sqrt{{2}}]_q$, the maximum absolute coefficient over the first 50 "
        f"powers is $\\max_{{k < 50}} |c_k([\\sqrt{{2}}]_q)| = {sqrt2_max}$.",
        computation_id=sqrt2_id,
        value_numeric=sqrt2_max,
    )

    qprov.claim(
        f"For the golden ratio $\\varphi$, the maximum absolute coefficient of "
        f"$[\\varphi]_q$ over the first 25 powers is "
        f"$\\max_{{k < 25}} |c_k([\\varphi]_q)| = {phi_max}$, "
        f"which exhibits the expected exponential growth at rate $\\varphi$.",
        computation_id=phi_id,
        value_numeric=phi_max,
    )

    qprov.claim(
        f"For Euler's $e$, the number of zero Taylor coefficients of $[e]_q$ "
        f"among the first 50 powers is ${e_zeros_in_first_50}$.",
        computation_id=e_id,
        value_numeric=e_zeros_in_first_50,
    )

    qprov.claim(
        f"For the rational $[3/2]_q = \\frac{{1+q+q^2}}{{1+q}}$, the first negative "
        f"Taylor coefficient appears at $k = {rat_first_negative}$, consistent with "
        f"the geometric expansion of $1/(1+q)$.",
        computation_id=rat_id,
        value_numeric=rat_first_negative,
    )

    # 5. export to LaTeX
    out_path = EXAMPLE_DIR / "claims.tex"
    text = qprov.export_latex(output=str(out_path))
    n_facts = text.count(r"\fact{")
    print(f"wrote {out_path} ({n_facts} \\fact macros)")
    print()
    print("Recorded computations:")
    for c in qprov.find(limit=20):
        print(f"  {c.id[:12]}  {c.function_name}  tags={c.tags}")
    print()
    print("Try:")
    print(f"  qprov --store {STORE_DIR} list")
    print(f"  qprov --store {STORE_DIR} show {pi_id[:12]} --payload")
    print(f"  qprov --store {STORE_DIR} verify {pi_id}")


if __name__ == "__main__":
    main()
