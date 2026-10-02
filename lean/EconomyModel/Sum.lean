/-!
# Finite sums over `0 … n-1`

`sumBelow n f = f 0 + … + f (n-1)`, defined by structural recursion so that the kernel can
evaluate it quickly inside `decide +kernel` proofs. We only need a handful of lemmas, so we
prove them directly instead of depending on Mathlib.
-/

namespace Econ

/-- `sumBelow n f = f 0 + f 1 + … + f (n-1)`. -/
def sumBelow : Nat → (Nat → Int) → Int
  | 0, _ => 0
  | n + 1, f => sumBelow n f + f n

theorem sumBelow_add (n : Nat) (f g : Nat → Int) :
    sumBelow n (fun i => f i + g i) = sumBelow n f + sumBelow n g := by
  induction n with
  | zero => simp [sumBelow]
  | succ n ih => simp only [sumBelow, ih]; omega

theorem sumBelow_mul_left (n : Nat) (c : Int) (f : Nat → Int) :
    sumBelow n (fun i => c * f i) = c * sumBelow n f := by
  induction n with
  | zero => simp [sumBelow]
  | succ n ih => simp only [sumBelow, ih, Int.mul_add]

theorem sumBelow_congr {n : Nat} {f g : Nat → Int} (h : ∀ i < n, f i = g i) :
    sumBelow n f = sumBelow n g := by
  induction n with
  | zero => rfl
  | succ n ih =>
    simp only [sumBelow]
    rw [ih (fun i hi => h i (by omega)), h n (by omega)]

theorem sumBelow_zero (n : Nat) : sumBelow n (fun _ => 0) = 0 := by
  induction n with
  | zero => rfl
  | succ n ih => simp [sumBelow, ih]

/-- Summing an indicator picks out a single term. -/
theorem sumBelow_single (n k : Nat) (v : Int) :
    sumBelow n (fun i => if k = i then v else 0) = if k < n then v else 0 := by
  induction n with
  | zero => simp [sumBelow]
  | succ n ih =>
    simp only [sumBelow, ih]
    by_cases hk : k = n
    · subst hk; simp
    · have : (k < n + 1) = (k < n) := by apply propext; omega
      simp [hk, this]

theorem sumBelow_nonneg {n : Nat} {f : Nat → Int} (h : ∀ i < n, 0 ≤ f i) :
    0 ≤ sumBelow n f := by
  induction n with
  | zero => simp [sumBelow]
  | succ n ih =>
    simp only [sumBelow]
    have := ih (fun i hi => h i (by omega)); have := h n (by omega); omega

theorem sumBelow_le {n : Nat} {f g : Nat → Int} (h : ∀ i < n, f i ≤ g i) :
    sumBelow n f ≤ sumBelow n g := by
  induction n with
  | zero => simp [sumBelow]
  | succ n ih =>
    simp only [sumBelow]
    have := ih (fun i hi => h i (by omega)); have := h n (by omega); omega

/-- A sum of non-negative terms with one positive term is positive. -/
theorem sumBelow_pos {n : Nat} {f : Nat → Int} (h : ∀ i < n, 0 ≤ f i) {j : Nat} (hj : j < n)
    (hpos : 0 < f j) : 0 < sumBelow n f := by
  induction n with
  | zero => omega
  | succ n ih =>
    simp only [sumBelow]
    by_cases hjn : j = n
    · subst hjn
      have := sumBelow_nonneg (n := j) (f := f) (fun i hi => h i (by omega)); omega
    · have := ih (fun i hi => h i (by omega)) (by omega); have := h n (by omega); omega

/-- A sum of non-negative terms is at least any one of its terms. -/
theorem sumBelow_ge_term {n : Nat} {f : Nat → Int} (h : ∀ i < n, 0 ≤ f i) {j : Nat} (hj : j < n) :
    f j ≤ sumBelow n f := by
  induction n with
  | zero => omega
  | succ n ih =>
    simp only [sumBelow]
    by_cases hjn : j = n
    · subst hjn
      have := sumBelow_nonneg (n := j) (f := f) (fun i hi => h i (by omega)); omega
    · have := ih (fun i hi => h i (by omega)) (by omega); have := h n (by omega); omega

end Econ
