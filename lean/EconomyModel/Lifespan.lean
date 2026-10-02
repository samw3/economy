/-!
# Lifespan per spawn

Encounters happen at a fixed rate. For a player with a fixed effective level, each encounter at
a location is an independent trial: the encountered enemy is drawn from the location's
encounter table and the player loses (dies and respawns) with probability `q`. The number of
encounters `T` in one life, counting the fatal one, is geometric:

* `P(T > k) = (1 - q)^k`                          (`survival`)
* `E[min(T, N)] = Σ_{k<N} P(T > k)`                (`truncatedLifespan`)
* `E[T] = 1 / q`                                   (`lifespan`)

We prove the finite identity `q · E[min(T, N)] = 1 - (1 - q)^N`, which shows that the truncated
expectation is always below `1/q` and converges to it geometrically, and that lifespans are
monotone: raising win probabilities (e.g. by levelling up or better gear) never shortens a life.
-/

namespace Econ

/-- `geomSum r N = r^0 + r^1 + … + r^(N-1)`. -/
def geomSum (r : Rat) : Nat → Rat
  | 0 => 0
  | n + 1 => geomSum r n + r ^ n

/-- Probability of surviving the first `k` encounters. -/
def survival (q : Rat) (k : Nat) : Rat := (1 - q) ^ k

/-- `E[min(T, N)]`: expected encounters in a life, if we stop watching after `N`. -/
def truncatedLifespan (q : Rat) (N : Nat) : Rat := geomSum (1 - q) N

/-- `E[T] = 1/q`: expected encounters per life, counting the fatal one. -/
def lifespan (q : Rat) : Rat := 1 / q

theorem truncatedLifespan_succ (q : Rat) (N : Nat) :
    truncatedLifespan q (N + 1) = truncatedLifespan q N + survival q N := rfl

theorem truncatedLifespan_mul (q : Rat) (N : Nat) :
    q * truncatedLifespan q N = 1 - survival q N := by
  unfold truncatedLifespan survival
  induction N with
  | zero => simp [geomSum]; grind
  | succ N ih =>
    simp only [geomSum, Rat.pow_succ]
    grind

theorem one_div_mul_cancel' {q : Rat} (hq : q ≠ 0) : q * (1 / q) = 1 := by
  rw [Rat.div_def, Rat.one_mul]; exact Rat.mul_inv_cancel q hq

theorem le_one_div_of_mul_le {q s : Rat} (hq : 0 < q) (h : q * s ≤ 1) : s ≤ 1 / q := by
  apply Rat.le_of_mul_le_mul_left _ hq
  rw [one_div_mul_cancel' (by grind)]; exact h

theorem one_div_nonneg' {q : Rat} (hq : 0 ≤ q) : 0 ≤ 1 / q := by
  rw [Rat.div_def, Rat.one_mul]
  rcases Rat.le_iff_lt_or_eq.mp hq with h | h
  · exact Rat.le_of_lt (Rat.inv_pos.mpr h)
  · rw [← h]; simp

/-- The truncated expectation never exceeds `1/q`. -/
theorem truncated_le_lifespan {q : Rat} (hq : 0 < q) (hq1 : q ≤ 1) (N : Nat) :
    truncatedLifespan q N ≤ lifespan q := by
  have h1 := truncatedLifespan_mul q N
  have h2 : 0 ≤ survival q N := Rat.pow_nonneg (by grind)
  exact le_one_div_of_mul_le hq (by grind)

/-- …and the gap is exactly `P(T > N) / q`, which vanishes geometrically. -/
theorem lifespan_sub_truncated {q : Rat} (hq : 0 < q) (N : Nat) :
    lifespan q - truncatedLifespan q N = survival q N / q := by
  have h1 := truncatedLifespan_mul q N
  have hqq : q * (1 / q) = 1 := one_div_mul_cancel' (by grind)
  unfold lifespan
  rw [Rat.div_def (survival q N) q]
  rw [Rat.div_def] at hqq ⊢
  grind

/-- A higher death probability means a shorter expected life. -/
theorem lifespan_anti {a b : Rat} (ha : 0 < a) (hab : a ≤ b) : lifespan b ≤ lifespan a := by
  apply le_one_div_of_mul_le ha
  have hb : 0 < b := by grind
  have hbb : b * (1 / b) = 1 := one_div_mul_cancel' (by grind)
  have h0 : 0 ≤ 1 / b := one_div_nonneg' (Rat.le_of_lt hb)
  have := Rat.mul_le_mul_of_nonneg_right hab h0
  unfold lifespan
  grind

/-! ## Encounter tables -/

/-- Encounter table of a location for one player loadout: `(weight, win probability)` for
every enemy variant (enemy × enemy level) that can appear. -/
abbrev EncounterTable := List (Nat × Rat)

/-- Total encounter weight. -/
def EncounterTable.weight (t : EncounterTable) : Nat := (t.map (·.1)).sum

/-- Weighted probability mass of losing. -/
def EncounterTable.lossMass (t : EncounterTable) : Rat :=
  (t.map fun e => (e.1 : Rat) * (1 - e.2)).sum

/-- Probability of dying in one encounter. -/
def EncounterTable.deathProb (t : EncounterTable) : Rat := t.lossMass / (t.weight : Rat)

/-- `t'.dominates t`: same enemy variants and weights, every win probability at least as high. -/
def EncounterTable.dominates : EncounterTable → EncounterTable → Bool
  | [], [] => true
  | a :: t', b :: t => a.1 == b.1 && decide (b.2 ≤ a.2) && EncounterTable.dominates t' t
  | _, _ => false

theorem EncounterTable.dominates_weight :
    ∀ t' t : EncounterTable, t'.dominates t = true → t'.weight = t.weight := by
  intro t'
  induction t' with
  | nil => intro t h; cases t <;> simp_all [EncounterTable.dominates]
  | cons a t' ih =>
    intro t h
    cases t with
    | nil => simp [EncounterTable.dominates] at h
    | cons b t =>
      simp only [EncounterTable.dominates, Bool.and_eq_true, beq_iff_eq, decide_eq_true_eq] at h
      have := ih t h.2
      simp only [EncounterTable.weight, List.map_cons, List.sum_cons] at this ⊢
      rw [h.1.1, this]

theorem EncounterTable.dominates_lossMass :
    ∀ t' t : EncounterTable, t'.dominates t = true → t'.lossMass ≤ t.lossMass := by
  intro t'
  induction t' with
  | nil => intro t h; cases t <;> simp_all [EncounterTable.dominates, EncounterTable.lossMass]
  | cons a t' ih =>
    intro t h
    cases t with
    | nil => simp [EncounterTable.dominates] at h
    | cons b t =>
      simp only [EncounterTable.dominates, Bool.and_eq_true, beq_iff_eq, decide_eq_true_eq] at h
      have hrest := ih t h.2
      simp only [EncounterTable.lossMass, List.map_cons, List.sum_cons] at hrest ⊢
      have hw : (0 : Rat) ≤ (a.1 : Rat) := by
        have := Rat.natCast_nonneg (a := a.1); exact this
      have hterm : (a.1 : Rat) * (1 - a.2) ≤ (b.1 : Rat) * (1 - b.2) := by
        rw [h.1.1]; rw [h.1.1] at hw
        exact Rat.mul_le_mul_of_nonneg_left (by grind) hw
      grind

theorem div_le_div_of_le_right' {a b c : Rat} (hab : a ≤ b) (hc : 0 ≤ c) : a / c ≤ b / c := by
  rw [Rat.div_def, Rat.div_def]
  apply Rat.mul_le_mul_of_nonneg_right hab
  have := one_div_nonneg' hc
  rwa [Rat.div_def, Rat.one_mul] at this

/-- Dominating tables have lower death probability. -/
theorem EncounterTable.dominates_deathProb (t' t : EncounterTable)
    (h : t'.dominates t = true) : t'.deathProb ≤ t.deathProb := by
  unfold EncounterTable.deathProb
  rw [EncounterTable.dominates_weight t' t h]
  exact div_le_div_of_le_right' (EncounterTable.dominates_lossMass t' t h) Rat.natCast_nonneg

/-- **Monotone lifespans.** If `t'` dominates `t` (e.g. `t'` is the same location one level
higher and the battle function is monotone), the expected life is at least as long. -/
theorem EncounterTable.dominates_lifespan (t' t : EncounterTable)
    (h : t'.dominates t = true) (hpos : 0 < t'.deathProb) :
    lifespan t.deathProb ≤ lifespan t'.deathProb :=
  lifespan_anti hpos (EncounterTable.dominates_deathProb t' t h)

/-- A chain of tables, each dominating the previous one. -/
def EncounterTable.chainB : List EncounterTable → Bool
  | t :: t' :: rest => t'.dominates t && EncounterTable.chainB (t' :: rest)
  | _ => true

end Econ
