import EconomyModel.Sum

/-!
# Markets, shadow prices and money pumps

A game economy's *market* is everything a player can do to convert goods into other goods
without fighting: buying from and selling to vendors, crafting, refining, salvaging and
currency exchange. Each such action is linear and repeatable, so we model it as a fixed
vector of net changes (negative = consumed, positive = produced).

A **pump** is a combination of actions that, on net, consumes nothing and produces a positive
amount of some *protected* good (a currency, or an item that is worth something). Repeating a
pump yields unbounded wealth: it is the formal notion of a trading/crafting exploit.

The main theorem `Market.no_pump_of_prices` is the easy half of Stiemke's lemma (the
"fundamental theorem of asset pricing"): if there are non-negative *shadow prices*, strictly
positive on protected goods, under which no single action increases value, then no pump exists.
The other half (no pump ⇒ such prices exist) is what the Python linter's LP solver relies on to
always produce either a price certificate or a concrete pump; Lean then checks whichever one
was produced.
-/

namespace Econ

/-- A sparse bundle of goods: `(good index, signed quantity)` pairs. -/
abbrev Bundle := List (Nat × Int)

/-- Net quantity of good `g` in bundle `b` (repeated entries add up). -/
def Bundle.amount (b : Bundle) (g : Nat) : Int :=
  (b.map fun e => if e.1 = g then e.2 else 0).sum

/-- Value of bundle `b` at prices `p`. -/
def Bundle.value (p : Nat → Int) (b : Bundle) : Int :=
  (b.map fun e => p e.1 * e.2).sum

theorem Bundle.amount_cons (e : Nat × Int) (b : Bundle) (g : Nat) :
    Bundle.amount (e :: b) g = (if e.1 = g then e.2 else 0) + Bundle.amount b g := by
  simp [Bundle.amount]

theorem Bundle.value_cons (p : Nat → Int) (e : Nat × Int) (b : Bundle) :
    Bundle.value p (e :: b) = p e.1 * e.2 + Bundle.value p b := by
  simp [Bundle.value]

theorem Bundle.value_nil (p : Nat → Int) : Bundle.value p [] = 0 := by
  simp [Bundle.value]

/-- Pricing every good and summing is the same as pricing the bundle directly. -/
theorem Bundle.sumBelow_price_amount (p : Nat → Int) (n : Nat) :
    ∀ b : Bundle, (∀ e ∈ b, e.1 < n) →
      sumBelow n (fun g => p g * Bundle.amount b g) = Bundle.value p b := by
  intro b
  induction b with
  | nil => intro _; simp [Bundle.amount, Bundle.value, sumBelow_zero]
  | cons e b ih =>
    intro hb
    have he : e.1 < n := hb e (by simp)
    have hb' : ∀ x ∈ b, x.1 < n := fun x hx => hb x (by simp [hx])
    have step : ∀ g, p g * Bundle.amount (e :: b) g
        = (if e.1 = g then p e.1 * e.2 else 0) + p g * Bundle.amount b g := by
      intro g
      rw [Bundle.amount_cons, Int.mul_add]
      by_cases h : e.1 = g
      · subst h; simp
      · simp [h]
    rw [sumBelow_congr (fun g _ => step g), sumBelow_add, sumBelow_single, ih hb',
      Bundle.value_cons]
    simp [he]

theorem Bundle.amount_eq_zero_of_not_mem (b : Bundle) (g : Nat) (h : ∀ e ∈ b, e.1 ≠ g) :
    Bundle.amount b g = 0 := by
  induction b with
  | nil => simp [Bundle.amount]
  | cons e b ih =>
    rw [Bundle.amount_cons, ih (fun x hx => h x (by simp [hx]))]
    simp [h e (by simp)]

/-- Value is monotone in quantities when prices are non-negative. -/
theorem Bundle.value_mono (p : Nat → Int) (n : Nat) (b b' : Bundle)
    (hb : ∀ e ∈ b, e.1 < n) (hb' : ∀ e ∈ b', e.1 < n) (hp : ∀ g < n, 0 ≤ p g)
    (hle : ∀ g < n, Bundle.amount b g ≤ Bundle.amount b' g) :
    Bundle.value p b ≤ Bundle.value p b' := by
  rw [← Bundle.sumBelow_price_amount p n b hb, ← Bundle.sumBelow_price_amount p n b' hb']
  exact sumBelow_le (fun g hg => Int.mul_le_mul_of_nonneg_left (hle g hg) (hp g hg))

/-- A market: goods `0 … goods-1` and a list of repeatable actions, each given by the net
change in goods it causes (negative = consumed, positive = produced). -/
structure Market where
  goods : Nat
  actions : List Bundle

/-- A combination of actions: `(repetitions, action index)` pairs. -/
abbrev Combo := List (Nat × Nat)

/-- Action `i` (or the empty bundle if `i` is out of range). -/
def Market.row (M : Market) (i : Nat) : Bundle := M.actions.getD i []

/-- Net change of good `g` after performing combination `c`. -/
def Market.net (M : Market) (c : Combo) (g : Nat) : Int :=
  (c.map fun x => (x.1 : Int) * Bundle.amount (M.row x.2) g).sum

/-- Every action only mentions goods `< M.goods`. -/
def Market.WellFormed (M : Market) : Prop := ∀ b ∈ M.actions, ∀ e ∈ b, e.1 < M.goods

/-- `c` is a *pump* for the protected goods `S`: on net it consumes nothing and produces a
positive amount of some protected good. Repeating it yields unbounded protected goods. -/
def Market.IsPump (M : Market) (S : Nat → Bool) (c : Combo) : Prop :=
  (∀ g, 0 ≤ M.net c g) ∧ ∃ g, S g = true ∧ 0 < M.net c g

/-- The market admits no pump at all. -/
def Market.NoPump (M : Market) (S : Nat → Bool) : Prop := ∀ c, ¬ M.IsPump S c

theorem Market.row_mem_or_nil (M : Market) (i : Nat) :
    M.row i ∈ M.actions ∨ M.row i = [] := by
  unfold Market.row
  rw [List.getD_eq_getElem?_getD]
  cases h : M.actions[i]? with
  | none => right; rfl
  | some b => left; simpa using List.mem_of_getElem? h

theorem Market.row_wf (M : Market) (hwf : M.WellFormed) (i : Nat) :
    ∀ e ∈ M.row i, e.1 < M.goods := by
  rcases M.row_mem_or_nil i with h | h
  · exact hwf _ h
  · rw [h]; simp

theorem Market.row_value_nonpos (M : Market) (p : Nat → Int)
    (hval : ∀ b ∈ M.actions, Bundle.value p b ≤ 0) (i : Nat) :
    Bundle.value p (M.row i) ≤ 0 := by
  rcases M.row_mem_or_nil i with h | h
  · exact hval _ h
  · rw [h]; simp [Bundle.value]

theorem Market.net_cons (M : Market) (x : Nat × Nat) (c : Combo) (g : Nat) :
    M.net (x :: c) g = (x.1 : Int) * Bundle.amount (M.row x.2) g + M.net c g := by
  simp [Market.net]

/-- Shadow value of a combination's net effect = weighted sum of the actions' values. -/
theorem Market.sumBelow_price_net (M : Market) (hwf : M.WellFormed) (p : Nat → Int) :
    ∀ c : Combo, sumBelow M.goods (fun g => p g * M.net c g)
      = (c.map fun x => (x.1 : Int) * Bundle.value p (M.row x.2)).sum := by
  intro c
  induction c with
  | nil => simp [Market.net, sumBelow_zero]
  | cons x c ih =>
    have step : ∀ g, p g * M.net (x :: c) g
        = (x.1 : Int) * (p g * Bundle.amount (M.row x.2) g) + p g * M.net c g := by
      intro g; rw [Market.net_cons, Int.mul_add]; congr 1
      rw [← Int.mul_assoc, Int.mul_comm (p g), Int.mul_assoc]
    rw [sumBelow_congr (fun g _ => step g), sumBelow_add, sumBelow_mul_left, ih,
      Bundle.sumBelow_price_amount p _ _ (M.row_wf hwf x.2)]
    simp

theorem Market.net_eq_zero_of_ge (M : Market) (hwf : M.WellFormed) (c : Combo) (g : Nat)
    (hg : M.goods ≤ g) : M.net c g = 0 := by
  induction c with
  | nil => simp [Market.net]
  | cons x c ih =>
    rw [Market.net_cons, ih, Bundle.amount_eq_zero_of_not_mem]
    · simp
    · intro e he; have := M.row_wf hwf x.2 e he; omega

/-- **No-pump theorem** (the easy half of Stiemke's lemma). If some non-negative shadow price
vector is strictly positive on the protected goods and no action increases shadow value, then
no combination of actions — repeated any number of times, in any order — is a pump. -/
theorem Market.no_pump_of_prices (M : Market) (S : Nat → Bool) (p : Nat → Int)
    (hwf : M.WellFormed)
    (hnn : ∀ g < M.goods, 0 ≤ p g)
    (hS : ∀ g < M.goods, S g = true → 0 < p g)
    (hval : ∀ b ∈ M.actions, Bundle.value p b ≤ 0) :
    M.NoPump S := by
  intro c ⟨hge, g, hSg, hpos⟩
  have hg : g < M.goods := by
    rcases Nat.lt_or_ge g M.goods with h | h
    · exact h
    · have := M.net_eq_zero_of_ge hwf c g h; omega
  have hW : 0 < sumBelow M.goods (fun g => p g * M.net c g) := by
    apply sumBelow_pos (j := g) _ hg
    · exact Int.mul_pos (hS g hg hSg) hpos
    · intro i hi; exact Int.mul_nonneg (hnn i hi) (hge i)
  rw [M.sumBelow_price_net hwf p c] at hW
  have hle : ∀ c : Combo, (c.map fun x => (x.1 : Int) * Bundle.value p (M.row x.2)).sum ≤ 0 := by
    intro c
    induction c with
    | nil => simp
    | cons x c ih =>
      simp only [List.map_cons, List.sum_cons]
      have h1 : (x.1 : Int) * Bundle.value p (M.row x.2) ≤ 0 :=
        Int.mul_nonpos_of_nonneg_of_nonpos (by omega) (M.row_value_nonpos p hval x.2)
      omega
  have := hle c
  omega

/-! ## Decision procedures (proof by reflection)

The generated per-economy modules discharge the hypotheses above with `decide +kernel` on
these boolean checkers, so every concrete claim is checked by Lean's kernel. -/

/-- Boolean version of `Market.WellFormed`. -/
def Market.wfB (M : Market) : Bool :=
  M.actions.all fun b => b.all fun e => decide (e.1 < M.goods)

theorem Market.wfB_sound (M : Market) (h : M.wfB = true) : M.WellFormed := by
  intro b hb e he
  simp only [Market.wfB, List.all_eq_true, decide_eq_true_eq] at h
  exact h b hb e he

/-- Check a shadow-price certificate: prices are non-negative, positive on protected goods, and
no action increases value. -/
def Market.checkPrices (M : Market) (S : Nat → Bool) (p : Nat → Int) : Bool :=
  M.wfB &&
  (List.range M.goods).all (fun g => decide (0 ≤ p g) && (!S g || decide (0 < p g))) &&
  M.actions.all (fun b => decide (Bundle.value p b ≤ 0))

theorem Market.checkPrices_spec (M : Market) (S : Nat → Bool) (p : Nat → Int)
    (h : M.checkPrices S p = true) :
    M.WellFormed ∧ (∀ g < M.goods, 0 ≤ p g) ∧ (∀ g < M.goods, S g = true → 0 < p g) ∧
      (∀ b ∈ M.actions, Bundle.value p b ≤ 0) := by
  simp only [Market.checkPrices, Bool.and_eq_true, List.all_eq_true, List.mem_range,
    decide_eq_true_eq, Bool.or_eq_true, Bool.not_eq_true'] at h
  obtain ⟨⟨hwf, hp⟩, hval⟩ := h
  refine ⟨M.wfB_sound hwf, fun g hg => (hp g hg).1, ?_, hval⟩
  intro g hg hSg
  rcases (hp g hg).2 with h | h
  · simp [hSg] at h
  · exact h

theorem Market.checkPrices_sound (M : Market) (S : Nat → Bool) (p : Nat → Int)
    (h : M.checkPrices S p = true) : M.NoPump S := by
  obtain ⟨hwf, hnn, hS, hval⟩ := M.checkPrices_spec S p h
  exact M.no_pump_of_prices S p hwf hnn hS hval

/-- Check that a combination is a pump. -/
def Market.checkPump (M : Market) (S : Nat → Bool) (c : Combo) : Bool :=
  M.wfB &&
  (List.range M.goods).all (fun g => decide (0 ≤ M.net c g)) &&
  (List.range M.goods).any (fun g => S g && decide (0 < M.net c g))

theorem Market.checkPump_sound (M : Market) (S : Nat → Bool) (c : Combo)
    (h : M.checkPump S c = true) : M.IsPump S c := by
  simp only [Market.checkPump, Bool.and_eq_true, List.all_eq_true, List.any_eq_true,
    List.mem_range, decide_eq_true_eq] at h
  obtain ⟨⟨hwf, hnn⟩, g, _, hSg, hpos⟩ := h
  refine ⟨?_, g, hSg, hpos⟩
  intro g'
  rcases Nat.lt_or_ge g' M.goods with h' | h'
  · exact hnn g' h'
  · rw [M.net_eq_zero_of_ge (M.wfB_sound hwf) c g' h']; omega

/-- A pump refutes `NoPump`. -/
theorem Market.not_noPump_of_pump (M : Market) (S : Nat → Bool) (c : Combo)
    (h : M.IsPump S c) : ¬ M.NoPump S := fun hn => hn c h

end Econ
