import EconomyModel.Market

/-!
# Value conservation and the income ceiling

Shadow prices that certify "no pump" are a *conserved valuation*: no trade, craft or exchange
can raise a player's shadow worth. Worth only enters the economy through faucets (loot drops,
respawn grants) and leaves through sinks (breakage, death penalties).

`Market.worth_run` makes this precise for any interleaving of trades, gains and losses, and
`Market.income_ceiling` turns it into a hard cap on how fast wealth can grow: if every faucet
event is bounded by one of a list of loot caps (e.g. "everything an enemy can drop at once"),
then after `k` faucet events a player's shadow worth has grown by at most `k · D`.
-/

namespace Econ

/-- An unsigned bundle (loot, losses): `(good, quantity)` pairs. -/
abbrev Loot := List (Nat × Nat)

def Loot.toBundle (l : Loot) : Bundle := l.map fun e => (e.1, (e.2 : Int))
def Loot.negBundle (l : Loot) : Bundle := l.map fun e => (e.1, -(e.2 : Int))

theorem Loot.toBundle_inRange {n : Nat} {l : Loot} (h : ∀ e ∈ l, e.1 < n) :
    ∀ e ∈ l.toBundle, e.1 < n := by
  intro e he
  simp only [Loot.toBundle, List.mem_map] at he
  obtain ⟨x, hx, rfl⟩ := he
  exact h x hx

theorem Loot.negBundle_inRange {n : Nat} {l : Loot} (h : ∀ e ∈ l, e.1 < n) :
    ∀ e ∈ l.negBundle, e.1 < n := by
  intro e he
  simp only [Loot.negBundle, List.mem_map] at he
  obtain ⟨x, hx, rfl⟩ := he
  exact h x hx

theorem Loot.value_negBundle_nonpos (p : Nat → Int) (n : Nat) :
    ∀ l : Loot, (∀ e ∈ l, e.1 < n) → (∀ g < n, 0 ≤ p g) → Bundle.value p l.negBundle ≤ 0 := by
  intro l
  induction l with
  | nil => intro _ _; simp [Loot.negBundle, Bundle.value]
  | cons x l ih =>
    intro hl hp
    have hx : 0 ≤ p x.1 := hp x.1 (hl x (by simp))
    have hrest := ih (fun e he => hl e (by simp [he])) hp
    simp only [Loot.negBundle, List.map_cons] at hrest ⊢
    rw [Bundle.value_cons]
    dsimp only
    have : p x.1 * -(x.2 : Int) ≤ 0 :=
      Int.mul_nonpos_of_nonneg_of_nonpos hx (by omega)
    omega

theorem Loot.amount_toBundle_nonneg (l : Loot) (g : Nat) : 0 ≤ Bundle.amount l.toBundle g := by
  induction l with
  | nil => simp [Loot.toBundle, Bundle.amount]
  | cons x l ih =>
    simp only [Loot.toBundle, List.map_cons] at ih ⊢
    rw [Bundle.amount_cons]
    split <;> omega

/-- `l` is within `cap`: no good appears in larger quantity than in the cap. -/
def Loot.Within (l cap : Loot) : Prop :=
  ∀ g, Bundle.amount l.toBundle g ≤ Bundle.amount cap.toBundle g

theorem Loot.nil_within (cap : Loot) : Loot.Within [] cap := by
  intro g
  have := Loot.amount_toBundle_nonneg cap g
  simpa [Loot.toBundle, Bundle.amount] using this

/-- A player's holdings: a quantity of every good. -/
abbrev Holdings := Nat → Int

def Holdings.add (h : Holdings) (b : Bundle) : Holdings := fun g => h g + Bundle.amount b g

/-- Shadow worth of holdings over goods `0 … n-1`. -/
def Holdings.worth (n : Nat) (p : Nat → Int) (h : Holdings) : Int :=
  sumBelow n (fun g => p g * h g)

theorem Holdings.worth_add (n : Nat) (p : Nat → Int) (h : Holdings) (b : Bundle)
    (hb : ∀ e ∈ b, e.1 < n) :
    Holdings.worth n p (h.add b) = Holdings.worth n p h + Bundle.value p b := by
  unfold Holdings.worth Holdings.add
  have step : ∀ g, p g * (h g + Bundle.amount b g) = p g * h g + p g * Bundle.amount b g :=
    fun g => Int.mul_add _ _ _
  rw [sumBelow_congr (fun g _ => step g), sumBelow_add, Bundle.sumBelow_price_amount p n b hb]

/-- Everything that can happen to a player's holdings. -/
inductive Event where
  /-- perform market action `i` once (buy, sell, craft, refine, exchange) -/
  | trade (i : Nat)
  /-- a faucet: loot drops, respawn grants, rewards -/
  | gain (l : Loot)
  /-- a sink: equipment breaking, death penalties, consumables used up -/
  | lose (l : Loot)

def Event.delta (M : Market) : Event → Bundle
  | .trade i => M.row i
  | .gain l => l.toBundle
  | .lose l => l.negBundle

/-- The faucet part of an event (empty unless it is a gain). -/
def Event.gained : Event → Loot
  | .gain l => l
  | _ => []

def Event.isGain : Event → Bool
  | .gain _ => true
  | _ => false

/-- All goods mentioned by gains/losses are goods of the market. -/
def Event.InRange (n : Nat) : Event → Prop
  | .trade _ => True
  | .gain l => ∀ e ∈ l, e.1 < n
  | .lose l => ∀ e ∈ l, e.1 < n

/-- Apply a sequence of events to holdings. -/
def Market.run (M : Market) (h : Holdings) : List Event → Holdings
  | [] => h
  | e :: es => M.run (h.add (e.delta M)) es

theorem Event.delta_inRange (M : Market) (hwf : M.WellFormed) (e : Event)
    (he : e.InRange M.goods) : ∀ x ∈ e.delta M, x.1 < M.goods := by
  cases e with
  | trade i => exact M.row_wf hwf i
  | gain l => exact Loot.toBundle_inRange he
  | lose l => exact Loot.negBundle_inRange he

/-- **Conservation.** Under certified shadow prices, shadow worth after any sequence of events
is at most the starting worth plus the worth of everything gained from faucets. Trading,
crafting and exchanging can never create value. -/
theorem Market.worth_run (M : Market) (p : Nat → Int) (hwf : M.WellFormed)
    (hp : ∀ g < M.goods, 0 ≤ p g) (hval : ∀ b ∈ M.actions, Bundle.value p b ≤ 0) :
    ∀ (evs : List Event) (h : Holdings), (∀ e ∈ evs, e.InRange M.goods) →
      Holdings.worth M.goods p (M.run h evs)
        ≤ Holdings.worth M.goods p h + (evs.map fun e => Bundle.value p e.gained.toBundle).sum := by
  intro evs
  induction evs with
  | nil => intro h _; simp [Market.run]
  | cons e es ih =>
    intro h hr
    simp only [Market.run, List.map_cons, List.sum_cons]
    have hes : ∀ x ∈ es, x.InRange M.goods := fun x hx => hr x (by simp [hx])
    have he : e.InRange M.goods := hr e (by simp)
    have hrun := ih (h.add (e.delta M)) hes
    rw [Holdings.worth_add _ _ _ _ (e.delta_inRange M hwf he)] at hrun
    have hle : Bundle.value p (e.delta M) ≤ Bundle.value p e.gained.toBundle := by
      cases e with
      | trade i =>
        simp only [Event.delta, Event.gained, Loot.toBundle, List.map_nil, Bundle.value_nil]
        exact M.row_value_nonpos p hval i
      | gain l => simp [Event.delta, Event.gained]
      | lose l =>
        simp only [Event.delta, Event.gained, Loot.toBundle, List.map_nil, Bundle.value_nil]
        exact Loot.value_negBundle_nonpos p M.goods l he hp
    omega

theorem sum_gained_le (p : Nat → Int) (D : Int) :
    ∀ evs : List Event, (∀ e ∈ evs, Bundle.value p e.gained.toBundle ≤ D) →
      (evs.map fun e => Bundle.value p e.gained.toBundle).sum ≤ D * (evs.countP Event.isGain : Int) := by
  intro evs
  induction evs with
  | nil => intro _; simp
  | cons e es ih =>
    intro hD
    have hrest := ih (fun x hx => hD x (by simp [hx]))
    have he := hD e (by simp)
    simp only [List.map_cons, List.sum_cons, List.countP_cons]
    cases e with
    | gain l =>
      simp only [Event.isGain, ite_true, Int.natCast_add, Int.natCast_one, Int.mul_add,
        Int.mul_one]
      omega
    | trade i =>
      have h0 : Bundle.value p (Event.trade i).gained.toBundle = 0 := by
        simp [Event.gained, Loot.toBundle, Bundle.value]
      rw [h0]
      simpa [Event.isGain] using hrest
    | lose l =>
      have h0 : Bundle.value p (Event.lose l).gained.toBundle = 0 := by
        simp [Event.gained, Loot.toBundle, Bundle.value]
      rw [h0]
      simpa [Event.isGain] using hrest

/-- Boolean check for loot caps: every cap mentions only real goods and is worth at most `D`. -/
def checkCaps (n : Nat) (p : Nat → Int) (caps : List Loot) (D : Int) : Bool :=
  decide (0 ≤ D) &&
  caps.all (fun c => c.all (fun e => decide (e.1 < n)) && decide (Bundle.value p c.toBundle ≤ D))

/-- **Income ceiling.** If the shadow prices `p` certify the market and every faucet event is
within one of the loot `caps` (each worth at most `D`), then after any sequence of events with
`k` faucet events, shadow worth has grown by at most `k · D`. -/
theorem Market.income_ceiling (M : Market) (S : Nat → Bool) (p : Nat → Int) (caps : List Loot)
    (D : Int) (hcert : M.checkPrices S p = true) (hcaps : checkCaps M.goods p caps D = true) :
    ∀ (evs : List Event) (h : Holdings), (∀ e ∈ evs, e.InRange M.goods) →
      (∀ e ∈ evs, ∃ cap ∈ caps, e.gained.Within cap) →
      Holdings.worth M.goods p (M.run h evs)
        ≤ Holdings.worth M.goods p h + D * (evs.countP Event.isGain : Int) := by
  intro evs h hr hloot
  obtain ⟨hwf, hp, _, hval⟩ := M.checkPrices_spec S p hcert
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
  have h1 := M.worth_run p hwf hp hval evs h hr
  have h2 := sum_gained_le p D evs hD
  omega

end Econ
