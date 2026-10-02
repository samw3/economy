import EconomyModel.Conservation

/-!
# Fastest possible progression

A *valuation* is a vector of non-negative shadow prices under which no market action creates
value (no positivity needed). Value conservation then bounds how fast wealth can grow, and since
holding an item `x` (with nothing negative) means having worth at least `p x`, it bounds how many
loot events any player needs before they can own `x`, whatever they trade, craft or get lucky with.
-/

namespace Econ

/-- Boolean check of a valuation: well-formed market, `p ≥ 0`, no action gains value. -/
def Market.checkValuation (M : Market) (p : Nat → Int) : Bool :=
  M.wfB && (List.range M.goods).all (fun g => decide (0 ≤ p g)) &&
  M.actions.all (fun b => decide (Bundle.value p b ≤ 0))

theorem Market.checkValuation_spec (M : Market) (p : Nat → Int) (h : M.checkValuation p = true) :
    M.WellFormed ∧ (∀ g < M.goods, 0 ≤ p g) ∧ (∀ b ∈ M.actions, Bundle.value p b ≤ 0) := by
  simp only [Market.checkValuation, Bool.and_eq_true, List.all_eq_true, List.mem_range,
    decide_eq_true_eq] at h
  obtain ⟨⟨hwf, hp⟩, hval⟩ := h
  exact ⟨M.wfB_sound hwf, hp, hval⟩

/-- Non-negative holdings that contain an `x` are worth at least `p x`. -/
theorem Holdings.price_le_worth (n : Nat) (p : Nat → Int) (h : Holdings) (x : Nat) (hx : x < n)
    (hp : ∀ g < n, 0 ≤ p g) (hh : ∀ g < n, 0 ≤ h g) (h1 : 1 ≤ h x) :
    p x ≤ Holdings.worth n p h := by
  have hterm : p x ≤ p x * h x := by
    have := Int.mul_le_mul_of_nonneg_left h1 (hp x hx); simpa using this
  have := sumBelow_ge_term (n := n) (f := fun g => p g * h g)
    (fun i hi => Int.mul_nonneg (hp i hi) (hh i hi)) hx
  unfold Holdings.worth
  omega

/-- Income ceiling from a valuation (no positivity on protected goods required). -/
theorem Market.income_ceiling_of_valuation (M : Market) (p : Nat → Int) (caps : List Loot) (D : Int)
    (hval : M.checkValuation p = true) (hcaps : checkCaps M.goods p caps D = true) :
    ∀ (evs : List Event) (h : Holdings), (∀ e ∈ evs, e.InRange M.goods) →
      (∀ e ∈ evs, ∃ cap ∈ caps, e.gained.Within cap) →
      Holdings.worth M.goods p (M.run h evs)
        ≤ Holdings.worth M.goods p h + D * (evs.countP Event.isGain : Int) := by
  intro evs h hr hloot
  obtain ⟨hwf, hp, hv⟩ := M.checkValuation_spec p hval
  simp only [checkCaps, Bool.and_eq_true, decide_eq_true_eq, List.all_eq_true] at hcaps
  obtain ⟨_, hcaps⟩ := hcaps
  have hD : ∀ e ∈ evs, Bundle.value p e.gained.toBundle ≤ D := by
    intro e he
    obtain ⟨cap, hcap, hw⟩ := hloot e he
    have hc := hcaps cap hcap
    have hcapR : ∀ x ∈ cap, x.1 < M.goods := fun x hx => hc.1 x hx
    have heR : ∀ x ∈ e.gained, x.1 < M.goods := by
      cases e with
      | gain l => exact hr _ he
      | trade i => simp [Event.gained]
      | lose l => simp [Event.gained]
    have := Bundle.value_mono p M.goods e.gained.toBundle cap.toBundle
      (Loot.toBundle_inRange heR) (Loot.toBundle_inRange hcapR) hp (fun g _ => hw g)
    omega
  have h1 := M.worth_run p hwf hp hv evs h hr
  have h2 := sum_gained_le p D evs hD
  omega

/-- Boolean check that `N` faucet events are necessary to own an `x` starting from `h`. -/
def grindCheck (M : Market) (p : Nat → Int) (caps : List Loot) (D : Int) (x : Nat) (h : Holdings)
    (N : Nat) : Bool :=
  M.checkValuation p && checkCaps M.goods p caps D && decide (x < M.goods) &&
  decide (D * ((N : Int) - 1) < p x - Holdings.worth M.goods p h)

/-- **Fastest possible progression.** If `grindCheck` holds, then every sequence of trades,
crafts, losses and faucet events (each within some loot cap) that starts from holdings `h` and
ends owning at least one `x` (with no negative holdings) contains at least `N` faucet events. -/
theorem Market.grind_lower_bound (M : Market) (p : Nat → Int) (caps : List Loot) (D : Int)
    (x : Nat) (h : Holdings) (N : Nat) (hc : grindCheck M p caps D x h N = true) :
    ∀ evs : List Event, (∀ e ∈ evs, e.InRange M.goods) →
      (∀ e ∈ evs, ∃ cap ∈ caps, e.gained.Within cap) →
      (∀ g < M.goods, 0 ≤ M.run h evs g) → 1 ≤ M.run h evs x →
      N ≤ evs.countP Event.isGain := by
  intro evs hr hloot hnn hx1
  simp only [grindCheck, Bool.and_eq_true, decide_eq_true_eq] at hc
  obtain ⟨⟨⟨hval, hcaps⟩, hx⟩, hgap⟩ := hc
  have hD0 : 0 ≤ D := by
    simp only [checkCaps, Bool.and_eq_true, decide_eq_true_eq] at hcaps; exact hcaps.1
  obtain ⟨_, hp, _⟩ := M.checkValuation_spec p hval
  have hceil := M.income_ceiling_of_valuation p caps D hval hcaps evs h hr hloot
  have hown := Holdings.price_le_worth M.goods p (M.run h evs) x hx hp hnn hx1
  -- p x ≤ worth h + D * k  and  D * (N - 1) < p x - worth h  ⇒  N - 1 < k
  rcases Nat.lt_or_ge (evs.countP Event.isGain) N with hk | hk
  · exfalso
    have hk' : ((evs.countP Event.isGain : Nat) : Int) ≤ (N : Int) - 1 := by omega
    have := Int.mul_le_mul_of_nonneg_left hk' hD0
    omega
  · exact hk

end Econ
