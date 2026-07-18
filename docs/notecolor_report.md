# TA8GAME note-color (don/ka) reverse-engineering report

Binary: `C:/Users/User/Documents/PCSX2x6/memcards/NM00033_converted.ps2/TA8GAME.dec`
MIPS32 EE little-endian, flat image, FILE = VA − 0x100000.
Addresses are **file offsets** unless written `va=…`. Disassembly via capstone + manual EE-64 decode.

---

## TL;DR

The visible falling-note sprite (red "don" vs blue "ka") is produced by the **EnsoParts note-graphic
factory `0x140b30` (va 0x240b30)**. It is called with:

- `a1 = note type`  (the fumen type, obtained live from `note->vtable[10]()`, vtable+0x28), and
- `a3 = colorVariant` (obtained from `note->vtable[24]()`, vtable+0x60).

Inside `0x140b30`, `a1`(type) selects a per-type sprite container and **`a3`(colorVariant) indexes
the sprite-frame array** (`[container+0x30][variant*4]`, code at 0x140c08). So the **colour is chosen
by `variant = note->vtable[24]()`, not by the raw type alone.** The type only chooses which note
*kind* (small/big/roll) is built; whether that note's frame is the red or the blue frame is the
`vtable[24]` variant.

This is why **big-ka (type 4) is always correct but small-ka (type 2) can come out red**: the two
note kinds are built by different `makeGraphic` methods, and the small-note graphic's colour is
gated by the `vtable[24]` don/ka variant. If our fumen makes the note object resolve `vtable[24]`
to the "don" variant for a type-2 note, it is drawn with the red frame even though the type byte is 2.

The raw type enum (1=don, 2=ka, 3=bigdon, 4=bigka, …) is **correct** and matches retail; the bug is
in a *second* signal (the don/ka variant the note object computes at load), driven by fumen data
*outside* the 16-byte note record.

---

## (a) The routines and offsets

### Falling-note sprite factory — 0x140b30 (va 0x240b30)   [PRIMARY / this is the colour code]
Signature effectively `makeNoteSprite(mgr a0, int type a1, geom* a2, int variant a3)`.
```
140b34  slti $t6,$a3,0x4000 ; 140b3c addiu $t7,$a3,-0x4000 ; 140b50 movz $s2,$t7,$t6
        ; $s2 = variant  (strip the 0x4000 "big/scaled" flag bit)
140b60  $s0 = a1                          ; $s0 = note type
140b6c  beqz $a1 -> return                ; type 0 = no note
140b8c  lw $v0,0x28($vt); jalr            ; (type getter again on inner obj)
140b9c  jal 0x172b78                      ; build/get per-type sprite container
...
140c08  lw   $t7,0x30($v0)                ; container->frameArray
140c0c  sll  $t6,$s2,2                    ; variant*4
140c10  addu $t7,$t7,$t6
140c14  lw   $a0,($t7)                    ; frame = frameArray[variant]   <-- COLOUR PICK
140c28  jal 0x2259e0 ; 140bbc/140ca8 jal 0x226328(OnpuDaiDon ctor) ; builds the sprite object
```
`0x172b78` (va 0x172b78) is the per-type container constructor (loops 5× building sub-lists).

### The EnsoParts `makeGraphic` that calls it — 0x1343f8 (va 0x2343f8)
This is a **virtual method** in the note-component vtable at va 0x31a820 (the on-screen note class,
`EnsoParts…`, distinct from the hit-effect `Onpu*` classes below).
```
134488  lw  $t6,0x38($note) ; 13448c li $t7,4 ; 134490 bne $t6,4 -> small-note branch @0x134704
        ; field +0x38 == 4  => big-note (Dai) path (builds OnpuDaiDon graphic @0x1345f0)
0x134704 (small-note branch):
134734  lw $v0,0x60($vt); jalr           ; $s1 = variant = note->vtable[24]()   <-- colour source
134768  lw $v0,0x28($vt); jalr           ; $v0 = note type
134780  jal 0x240b30                      ; makeNoteSprite(a1=type, a3=variant=$s1)
```
So the note component asks the note for its **type** (vtable+0x28) and its **colour variant**
(vtable+0x60) and forwards both to `0x140b30`.

### The sprite-name atlas table — 0x191748 (va 0x291748)
Array of C-string pointers loaded by the atlas builder at 0x18568 (function 0x18328). The base group
(10 entries) is the canonical `frameIndex -> sprite`:
```
0:don 1:do 2:ko 3:katsu 4:ka 5:renda 6:don_dai 7:katsu_dai 8:renda_dai 9:geki_renda
     ^red ^red ^BLUE ^BLUE ^BLUE          ^red-big  ^BLUE-big
```
`don`/`do` = red; `ko`/`katsu`/`ka` = blue; `_dai` = big variants. Following groups are skins
(`imo…`, `nyoro/te…`) and the hit-effect group at 0x191838 (`don_hit`,`katsu_hit`,…).

### Note visual classes (RTTI) — va 0x31b930..0x31b9e0 (names 0x345c58..0x345db0)
`Onpu, OnpuImo, OnpuWKatsu(big ka), OnpuWDon(big don), OnpuDaiRenda, OnpuDaiKatsu, OnpuDaiDon,
OnpuRenda, OnpuKa, OnpuKatsu, OnpuKo, OnpuDo, OnpuDon`. Vtables:
```
OnpuDon 0x31b878  OnpuDo 0x31b838  OnpuKo 0x31b7f8  OnpuKa 0x31b778  OnpuKatsu 0x31b7b8
OnpuDaiDon 0x31b6f8  OnpuDaiKatsu 0x31b6b8  OnpuWDon 0x31b5f8  OnpuWKatsu 0x31b5b8
OnpuRenda 0x31b738  OnpuDaiRenda 0x31b678  OnpuImo 0x31b578  Onpu(base) 0x31b8b8
```
(These `Onpu*` are the hit-splash effect objects; see the SECONDARY path below.)

### SECONDARY path — hit-effect spawner (note update loop) — 0x22f68 (va 0x122f68)
Per-frame loop over the note list; on judge/spawn it dispatches on type through a jump table and
spawns hit effects. Relevant because it shows the same don/ka machinery:
```
0x2390c  jal 0x1246a0                     ; per-type timing/sound handler (2nd table @0x2352b8)
0x23920  sltiu $t4,$s0,0x11               ; $s0 = type < 17
0x23948  jump table @0x235094 (va 0x335094) -> per-type handler
```
Per-type spawn jump table (0x235094): type1->0x2399c; types 2,5,10,15->0x23c90;
types 3,6,11,16->0x23cb8; types 4,7->0x23b20; 8,12->0x23b90; 9->0x23bb0; 13->0x23c08; 14->0x23c4c.
All of these ultimately reach the shared effect code at **0x2399c**, which does:
```
0239b0  lw $v0,0x10($vt); jalr            ; donka = note->vtable[4]()   (base returns -1)
0239c0  xori $t7,$v0,2                     ; test donka == 2  (the "ka" code)
0239cc  movz $s1,$t5,$t7                   ; donka override
023a48  index = donka*15 + type
023a60  base 0x291ea0 (va 0x291ea0)        ; REMAP table
023a68  frameIndex = REMAP[index]
```
This confirms the game's internal "ka" don/ka code is **2**, tested with `xori …,2`.

---

## (b) EXACTLY how colour is decided (visible note)

```
type    = note->vtable[10]()   ; vtable+0x28  -> fumen type 1..16 (chooses note KIND)
variant = note->vtable[24]()   ; vtable+0x60  -> don/ka colour variant (chooses RED vs BLUE frame)
makeNoteSprite(type, variant)  ; 0x140b30
     -> container = perTypeContainer(type)      ; 0x172b78
     -> frame     = container->frameArray[variant]   ; 0x140c08  (RED if don-variant, BLUE if ka-variant)
```
Big notes take a separate branch in the component's `makeGraphic` (0x1343f8, gated by note field
`+0x38 == 4`) that builds the big (`_dai`) graphic; that branch is driven by the type directly, which
is why big-ka stays blue regardless. Small notes go through the `variant`-indexed branch (0x134704),
so their colour is decided by `vtable[24]`.

REMAP table (secondary hit-effect path), file 0x191ea0 / va 0x291ea0, indexed `donka*15 + type`,
values are frameIndex (0..9) or -1:
```
donka=0: [2]=0 [5]=2(ko) [7]=4(ka) [9]=6(don_dai) [10..11]=7(katsu_dai) ...
donka=1: [15+2]=1(do) [15+5]=3(katsu) [15+7]=5(renda) ...
donka=2: [30+2]=1(do) [30+3]=2(ko) [30+4]=1 [30+5]=3(katsu) ...
```

## (c) What the converter should emit

- Keep emitting **type 2** for small-ka — that is correct and matches retail (retail also stores
  small-ka as type 2; the frequency profile with type 2 most-common is consistent).
- The colour is NOT in `record[0]` — it is the **don/ka variant** the game reads via `vtable[24]`
  (and `vtable[4]` for the effect path). That variant is computed when the note object is built by
  the fumen loader, from the note type **plus surrounding fumen structure**. With byte-identical note
  records but wrong colour, the divergence must be in the **fumen header / branch / section fields**
  our converter writes (or a note sub-flag left at default), which make the loader classify type-2
  notes as the "don" variant.
- Actionable check: byte-diff a retail `.sht` **header + section/branch table** against ours (the
  note array already matches). The loader branches on manager fields `[mgr+0x30]`, `[mgr+0x3c]`
  (e.g. `[mgr+0x3c]==1/2` at 0x23040) and builds the note object via factory `0x125778` (called at
  0x230ec with `[mgr+0x30]`,`[mgr+0x3c]` as args). One of those header/section values is what sets
  the don/ka variant; fix it so type-2 notes resolve to the "ka" variant (internal code 2).

## (d) Tables dumped
- Falling-note factory `0x140b30`; per-type container `0x172b78`; component `makeGraphic` `0x1343f8`
  (big-note gate `[+0x38]==4`, small-note branch `0x134704`).
- Sprite-name atlas group, file 0x191748 / va 0x291748: base order
  `don, do, ko, katsu, ka, renda, don_dai, katsu_dai, renda_dai, geki_renda`.
- Hit-effect per-type jump table, file 0x235094 / va 0x335094 (17 entries listed above).
- Timing/sound per-type jump table, file 0x2352b8 / va 0x3352b8.
- Don/ka REMAP (hit-effect), file 0x191ea0 / va 0x291ea0, indexed `donka*15 + type`.
- The internal "ka" don/ka code is **2** (proved by `xori $v0,2` at 0x239c0).

## Confidence / limits
Fully resolved and cross-checked: the visible-note colour is `frameArray[variant]` in factory
0x140b30, with `variant = note->vtable[24]()` and `type = note->vtable[10]()`; big notes take a
separate type-driven branch (hence big-ka always blue), small notes take the variant-driven branch
(hence small-ka colour depends on the don/ka variant). The sprite-name index table and the
hit-effect REMAP corroborate don=0/do=1/ko=2/katsu=3/ka=4 and the "ka" code = 2.

Not pinned down purely statically: the exact fumen field that makes the note object's `vtable[24]`
don/ka variant come out "don" for our type-2 notes. That requires diffing our `.sht` header/section
structure against a retail one (the note array is already identical). The concrete code to inspect is
the loader branch at 0x23040 (`[mgr+0x3c]`) and the note factory 0x125778 (args `[mgr+0x30]`,
`[mgr+0x3c]`).

---

## LOADER TRACE / FIX

### Decisive new finding: colour is HARDCODED per Onpu C++ subclass (not read from any record field)

Each `Onpu*` note class overrides a "don/ka code" getter at **vtable[4] (offset 0x10)**. These are
one-instruction constant returns (file offsets in the 0x1446xx bank):

| class    | vtable[4] fn (file) | returns | meaning |
|----------|--------------------|---------|---------|
| OnpuDon  | 0x144688 | **0** | DON / red |
| OnpuDo   | 0x144690 | **0** | DON / red |
| OnpuKo   | 0x144698 | **0** | DON / red |
| OnpuKatsu| 0x1446a0 | **1** | KA / blue |
| OnpuKa   | 0x1446a8 | **1** | KA / blue |
| OnpuDaiDon | 0x1446c0 | 0 | big DON / red |
| OnpuDaiKatsu | 0x1446d0 | 1 | big KA / blue |
| OnpuWDon | 0x144700 | 0 | red |
| OnpuWKatsu | 0x144710 | 1 | blue |
| OnpuRenda / OnpuDaiRenda / OnpuImo | 0x1446b0 / 0x1446e0 / 0x144720 | 2 | roll |

Verified: the per-class methods differ ONLY in their vtable pointer and these hardcoded getters
(slot [3]/0xc init code is byte-identical across don/do/ko/ka/katsu; the only differences are
slots [0],[1],[3],[4],[9],[10] which are per-class thunks + the constant getters). There is **no
code path that reads a note record field to decide don vs ka** — the colour is 100% a function of
*which class the note object is instantiated as*.

Consequence: the render factory `0x140b30` picks the sprite frame with this same class-derived code
(`variant = note->vtable[…]()` → red frame for code 0, blue frame for code 1). So:

> A type-2 note renders RED **iff** the loader built it as `OnpuDon`/`OnpuDo`/`OnpuKo` (code 0)
> instead of `OnpuKa`/`OnpuKatsu` (code 1).

### Loader trace — what the loader reads

- Note/scroll object init `0x121dc0` (called via factory `0x125778` at loader site `0x230ec`):
  - stores `note+0x08 = record ptr` (a2 = `$fp`), `note+0x10 = [mgr+0x30]`, `note+0x14 = [mgr+0x3c]`,
    `note+0x0c = [mgr]`.
  - computes an index `([mgr+0x30]*3 + [mgr+0x3c]) * 4` into the **track record** and reads a
    **float at +0x28** (scroll-speed region). The `*3` stride = the 3-int subtrack stride.
  - This path is **scroll timing only**; the float it reads is a scrollSpeed, not a note colour.
- In the per-frame note loop `0x22f68`, `[mgr+0x3c]` is a **branch state** written with 0/1/2
  (`sw …,0x3c(mgr)` at 0x23094=2, 0x23f1c=0) and tested `== 1 / == 2` at 0x23040 — this is the
  **fumen branch / diverge (bunki) selector** (normal/pro/master path), and `[mgr+0x30]` is the
  active track/line index used to index subtrack arrays.

### Ruling out the pointGain / subtrack hypotheses

- `[mgr+0x30]` and `[mgr+0x3c]` are the **branch-line index** and **branch state**; together they
  index the track's per-subtrack **scrollSpeed / bunki** data (stride 3 ints) for timing. They are
  NOT consulted by the don/ka classifier, which is a hardcoded per-class constant.
- The subtrack's 3rd int (**pointGain**) is used only for scoring (点/point gain per note); nothing
  in the colour path reads it. Our constant `~601` vs retail's decreasing `420,410,405…` will
  affect **score**, not note colour. So pointGain is **not** the cause of the red-ka bug.

### Where the type→class decision is (and the honest limit)

The don/ka *class* is selected when the note-graphic layers are built by the note-component method
`0x107490` (a vtable method at va 0x317810 of the note-graphic manager whose vtable begins at
va 0x317804). That method constructs the full layer set (`OnpuDon, OnpuDo, OnpuKo, OnpuKa,
OnpuRenda, …` — via ctors 0x225a18/0x225ad8/0x225cb0/0x225ed8/0x225fe8) and the visible layer is
selected by the note's type. The concrete `switch(type)`/index that maps **record[0] → which
Onpu class is shown** lives in this large prototype/layer system; I confirmed the colour SOURCE
(class-hardcoded code) and the layer set, but did not isolate the single type→layer index
instruction purely statically (the system clones/selects layers through several indirections and
IDA has no functions defined to follow it cleanly).

### What this means for the converter

- Colour is NOT in the 16-byte note record's later fields and NOT in pointGain/subtracks. It is a
  function of `record[0]` (type) → Onpu class, decided at load.
- Because our type-2 note bytes are byte-identical to retail yet build the DON class, the loader is
  reaching the "don" layer for type 2. Since the note array matches, the input that changes the
  type→class outcome must come from the **surrounding fumen structure the loader consults while
  iterating notes** — the **track / subtrack / branch layout** (the fields feeding `[mgr+0x30]`
  and `[mgr+0x3c]`, i.e. the subtrack `noteIndexSt`/`noteCount` ranges and the branch/bunki
  configuration), or the **header `noteOffset`/`noteCount`** framing.

Concrete converter actions (in priority order):
1. **Audit subtrack `noteIndexSt` / `noteCount` ranges.** If a small note's global index does not
   fall inside the intended subtrack (because our subtrack ranges or ordering differ from retail),
   the loader can associate it with the wrong line/state and build the wrong layer. Make our
   subtrack ranges exactly cover the note array the way retail does.
2. **Match the branch/bunki fields** (`bunkis[6]`, `trackLine`) to retail. A wrong branch state
   (`[mgr+0x3c]`) or line index (`[mgr+0x30]`) changes which subtrack/scroll data a note uses and
   can steer layer selection.
3. **Verify header framing** (`trackCount`, `noteOffset`, `noteCount`): an off-by-one or wrong
   `noteOffset` shifts which record the loader treats as note N, which would silently mis-class.
4. Keep `type=2` for small-ka (correct). Do NOT try to encode colour in note fields — the engine
   ignores them for colour.

The single most productive test: **byte-diff a retail `.sht` against ours for the 136-byte track
records and the header** (not the note array, which already matches). The first differing field in
the track/subtrack/branch layout that changes note→subtrack association is the fix. pointGain can be
excluded from that diff for the colour question (it only affects score).


---

## TYPE->CLASS TABLE

### Key result: 0x107490 is NOT a type->class switch -- it is a LAYER-SET template builder

I fully disassembled 0x107490 (va 0x207490) and its sibling 0x107fa8 (va 0x207fa8). Both call the
Onpu constructors UNCONDITIONALLY, in a fixed sequence (no switch(type), no branch on any note field
before the ctor calls). The full ctor call list inside the 0x107490-family function:

```
0x107804 NEW OnpuKo      0x107e94 NEW OnpuDo
0x107980 NEW OnpuKa      0x1081f8 NEW OnpuDon
0x1079a8 NEW OnpuKo      0x108270 NEW OnpuDo
0x1079c0 NEW OnpuRenda   0x10831c NEW OnpuKo
0x1079fc NEW OnpuKo      0x108498 NEW OnpuKa
0x107a14 NEW OnpuRenda   0x1084c0 NEW OnpuKo
                         0x1084d8 NEW OnpuRenda
                         0x108514 NEW OnpuKo
                         0x10852c NEW OnpuRenda
                         0x1089ac / 0x108b64 NEW OnpuDo ; 0x109080 NEW OnpuDon
```

Both builders instantiate the SAME class set {OnpuDon, OnpuDo, OnpuKo, OnpuKa, OnpuRenda} with no
type gating. CONCLUSION: one note-graphic component holds ALL small-note sprite layers
(don + do + ko + ka + katsu + renda) simultaneously; the visible one is chosen later, at render, by
the note's don/ka code -- NOT by instantiating a single class from the type here.

Confirmed there is no clean type->single-class switch anywhere: grouping every Onpu-ctor call by
containing function shows only (a) the two layer-set builders above, and (b) single-class specialized
builders for effects/big notes (0x109020->Don, 0x126648->DaiKatsu, 0x1343f8->DaiDon, 0x140b30->Dai
wrapper), none of which switch on the record type.

### How the visible layer (colour) is actually chosen

At render, the note-graphic factory 0x140b30 does:
```
type      = noteObj->vtable[0x28]()          ; the note type
container = 0x172b78(this, type)             ; per-type sprite container
variant   = <a3 from caller>                 ; = noteCmd->vtable[0x60]() (0x4000 flag stripped)
frame     = container->frameArray[variant]   ; 0x140c08 : the shown sprite
```
and the note's own class hardcodes the don/ka code at vtable[4] / offset 0x10:
```
OnpuDon/OnpuDo/OnpuKo -> 0 (red)    OnpuKa/OnpuKatsu -> 1 (blue)    renda/imo -> 2
```
So colour is a property of WHICH Onpu class the note object is (equivalently, which layer index is
selected), driven by the note's type via the render-time container/variant lookup -- there is no
load-time "new OnpuKa vs new OnpuDon" branch to point at.

### Answer to the specific questions

- Does the selection read ONLY record[0] (type)? The visible-layer selection is a function of the
  note's type (via vtable[0x28] -> container) and its variant (via vtable[0x60]). Neither getter, nor
  the layer builder 0x107490, reads pointGain or the subtrack scoring field. The scroll/timing object
  0x121dc0 uses [mgr+0x30] (branch-line) and [mgr+0x3c] (branch state) to index scroll data -- that is
  timing, not colour.
- Type 2 -> OnpuKa or OnpuDon? In a correctly-framed chart, type 2 selects the KA / BLUE layer
  (retail stores small-ka as type 2 and it renders blue; type 2 is the most frequent note). Every
  small-note component contains a dedicated OnpuKa/OnpuKatsu (code 1, blue) layer and a dedicated
  OnpuKo/OnpuDo/OnpuDon (code 0, red) layer; type 2 is the value that maps to the blue layer.

### Reconciliation with the bug -- which of (a)/(b)

Because (i) colour is purely the class/layer's hardcoded code, (ii) the layer set is pre-built for
every note, (iii) nothing in the colour path reads pointGain/subtrack scoring, and (iv) retail type-2
records (byte-identical to ours) render blue -- the red-ka bug is CASE (a): the type the engine reads
for our notes is not being read as 2 at the point of layer selection. The note lands on the code-0
(don/red) layer because its EFFECTIVE type/variant at render is wrong, even though record[0]==2 on
disk.

Realistic causes, all in the FRAMING/ASSOCIATION of notes, not the note bytes:
1. noteOffset / noteCount header framing -- if noteOffset is off (wrong base, or counted in the wrong
   units: record-index vs byte-offset), the loader reads a DIFFERENT dword as "type" for each note
   (e.g. our position field or a neighbouring record), so type-2 notes decode as some other value
   that maps to the red layer. This is the single most likely culprit given "identical note bytes,
   wrong colour."
2. Subtrack noteIndexSt / noteCount ranges -- the note->subtrack association selects which
   line/variant is active; if our ranges do not partition the note array exactly as retail's, a note
   can be pulled into the wrong line and pick the wrong layer.
3. Branch/bunki fields (bunkis[6], trackLine) feeding [mgr+0x30] / [mgr+0x3c].

pointGain is EXCLUDED as a colour cause (scoring only).

### Recommended fix / next diagnostic (decisive)

Byte-diff a retail .sht against ours for the HEADER (trackCount, noteOffset, noteCount, pad) and the
136-byte TRACK RECORDS (especially each subtrack's noteIndexSt/noteCount), NOT the 16-byte note array
(already identical). Verify specifically:
- noteOffset points at the true first note record and is in the same units as retail (byte offset vs
  record index) -- an off-by-one or unit mismatch shifts every note's decoded "type".
- noteCount matches retail's.
- Every subtrack's [noteIndexSt, noteIndexSt+noteCount) exactly covers its notes with no gaps/overlap.
Fix the first field that differs; that makes the engine read type==2 at layer-selection time and
select the blue OnpuKa layer.

### Honest limit

Proved: colour = class-hardcoded code; 0x107490 builds all layers unconditionally (not a type switch);
no static switch(type)->single Onpu ctor exists; the render path selects the layer by type+variant and
reads neither pointGain nor subtrack scoring. I could not, from static bytes alone, single-step the
exact indirection that turns record[0] into the render-time container/variant index (the system clones
layers and dispatches through several vtable indirections, and IDA has no functions defined to follow
it live). The framing/noteOffset hypothesis (case a) is the strongest remaining explanation and is
directly testable by the header/track-record byte-diff above.
