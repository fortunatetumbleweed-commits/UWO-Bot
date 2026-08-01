# Heading CNN — Shape-Prior Training Design

**Status:** proposal · **Author:** iteration 2026-07-02 · **Supersedes:** none

## Motivation

The current heading CNN (v10-v14 line) is trained end-to-end to
regress `(sin θ, cos θ)` from an 80×80 mini-map crop.  When the ship
is heavily occluded (pirate boss covering ~80% at t82-t84,
"Bari Village" text overlay at t556), both v10 and v14 collapse
to the training-mean N/S axis regardless of the actual heading.

The pattern the model can't shortcut through: **it never explicitly
learns what the ship's shape is**.  A human viewing the same
occluded frame identifies the ship by recognising which visible
pixels belong to the ship's silhouette, mentally completing the
missing parts, then reading the direction from the completed shape.
Our CNN attempts all three in a single regression head.

Adding a **shape prior as a separate learnable skill**, plus rules
that connect shape to heading, should give the model a much stronger
inductive bias to fall back on when the input is ambiguous.

## Domain rules we can encode

Two rules from game/physics knowledge that the model currently has
to discover on its own:

1. **Bilateral symmetry.**  The ship sprite is mirror-symmetric
   across its bow-stern axis.  Any predicted ship shape that isn't
   symmetric across the predicted heading is wrong.
2. **Heading axis = stern midpoint → bow tip.**  The heading vector
   equals the principal axis of the ship's silhouette, oriented from
   stern toward bow.  The bow tip is pointier than the stern (which
   is flat).  This asymmetry is what disambiguates heading from
   heading + 180°.

Rule 1 is a symmetry.  Rule 2 is a geometric relationship between
two model outputs (shape + heading).  Both can be encoded as loss
terms.

## Proposed architecture

Same encoder as v14 (219k params, 4 conv blocks).  Replace the
single heading head with three heads:

```
encoder → 3 heads:
    ship_mask         : 1×80×80  (per-pixel ship / not-ship probability)
    heading           : 2       (sin θ, cos θ)
    bow_end_prob      : 1       (which end of the PCA axis is the bow)
```

Adds ~50k parameters (mostly in the segmentation decoder).  Inference
still returns heading only — the extra outputs are for the training
objective.

## Loss terms

Five gradient signals per training example:

| # | name          | formula                                                                 | teaches                                 |
|---|---------------|-------------------------------------------------------------------------|-----------------------------------------|
| 1 | `L_seg`       | BCE(mask, ground_truth_ship_mask)                                       | "these pixels belong to the ship"        |
| 2 | `L_heading`   | angular_err(predicted_heading, true_heading)                            | main task                                |
| 3 | `L_sym`       | mean(\|mask − flip(mask, axis=predicted_heading)\|)                      | bilateral symmetry                       |
| 4 | `L_pca_align` | \|angle(PCA(mask)) − predicted_heading\| mod 180°                        | heading ⟷ shape principal-axis link     |
| 5 | `L_bow`       | BCE(bow_end_prob, true_bow_end)                                         | asymmetry that fixes bow-vs-stern flip   |

Total: `L_total = L_heading + λ₁·L_seg + λ₂·L_sym + λ₃·L_pca_align + λ₄·L_bow`

Starting weights: `λ₁=1.0, λ₂=0.5, λ₃=0.3, λ₄=0.5` — tune during first
training run.

### Rule expression details

**`L_sym` — bilateral symmetry.**  For each sample, rotate the
predicted mask so the predicted heading axis is horizontal, mirror it
top-to-bottom, and compare to the original.  Differences penalize
asymmetric predictions.  Uses `torchvision.transforms.functional.rotate`
(differentiable) or a small `grid_sample`-based rotation.

**`L_pca_align` — heading ⟷ mask alignment.**  Compute the covariance
matrix of ship-mask pixel coordinates, take its principal eigenvector
(differentiable — batched 2×2 eigendecomposition has a closed form).
Project against the predicted heading vector.  Use mod-180° distance
so PCA-vs-heading only complains about axis mismatch, not bow-side.

**`L_bow` — bow disambiguation.**  Ground-truth `true_bow_end ∈
{0, 1}` marks which end of the PCA axis is the bow.  Available from
training labels — we know the target heading, so once PCA is
computed we know which end lies along it.

## Ground-truth availability

All five losses have supervision signal from the existing pipeline:

- `L_seg`: synthesis knows the ship mask before occluder is applied.
   Hard-case sprites don't have exact masks but the green-blob
   detection is a reasonable pseudo-label.
- `L_heading`: existing target.
- `L_sym`: self-supervised — needs no external label, only the
   model's own outputs.
- `L_pca_align`: self-supervised — comparison between two model outputs.
- `L_bow`: derivable from `true_heading` and the ground-truth mask.

## Alternative approaches considered

**Reconstruction / denoising decoder.**  Take occluded input,
reconstruct the un-occluded ship RGB.  More powerful than a
segmentation mask but requires a full decoder (~300k params) and
more delicate training.  Deferred as a v16 candidate if v15
(multi-head) doesn't reach the target.

**Canonical template + contrastive matching.**  Build an embedding
library from clean ships at each 5° rotation; force occluded/clean
embeddings to match via contrastive loss; inference via nearest
neighbour lookup.  Elegant analogue to human "match to memory," but
gets brittle if the embedding space isn't tight.

**Purely architectural symmetry.**  Predict only half the ship
mask + a rotation; reflect + combine to output the full mask.
Guarantees symmetry by construction but locks us into a specific
decoder shape.  We prefer the loss-based symmetry (`L_sym`) because
it's a soft constraint the model can violate during early training
and correct later.

## Complementary training tricks

Layer on top of any of the above:

- **Curriculum.**  Pretrain a few epochs on clean-strategy samples
   only ("learn the shape first"), then gradually add occlusion.
   Reduces the "learn everything at once" overload at epoch 0.
- **Canonical reference input channel.**  Append a fixed rendered
   ship at heading 0° as a 4th input channel.  Gives the model
   explicit access to "here is what a bow-up ship looks like" so it
   can compare against the input.
- **Occluder-invariance consistency loss.**  For a given ship at a
   given heading, ensure two different synthetic occlusions produce
   the same heading output.

## Implementation checklist

1. Add segmentation + bow heads to `brain/ai_nav/learned/heading_cnn.py`.
2. Add `L_sym`, `L_pca_align`, `L_bow` loss terms to
   `tools/train_heading_cnn.py`.
3. Extract ground-truth ship masks in `heading_synthesis.py` before
   applying occlusion; return alongside the synthesized crop.
4. Extend `SynthExample` dataclass with `ship_mask` and `bow_end` fields.
5. Wire multi-target loss into the training loop.
6. Add ablation flag so we can train v15 with each loss individually
   as well as the full stack, to attribute wins.
7. Regression eval against the 17 saved hard cases.

## Success criteria for v15

- **Per-tick hard-case error avg** < 60° (v13 was 72°, v14 target ~65°).
- **`clean` p90** stays within 1° of v12's 6.16°.  Multi-head training
   usually improves the clean baseline via the extra signal, but
   worth checking.
- **Category B (agree-but-safety-net-fires)** count on a fresh voyage
   drops meaningfully — this is the class of failure where a shape
   prior most directly helps.
- **Bilateral symmetry check** on inference frames: predicted masks
   should be symmetric across predicted heading axis to within ~5%
   pixel difference on clean-ship ticks.  This is a sanity check
   that the losses did their job.

## v16 candidate — classification head with circular soft labels

A separate architectural change that can be shipped independently of
v15 (shape prior) — replace the regression head with a 360-way
classification head.  Directly attacks the "collapse to a single wrong
angle" failure mode we see on heavily occluded frames.

### Motivation

Regression forces the model to output one point in continuous heading
space.  On truly ambiguous frames (e.g. t83, t84 where the ship could
plausibly be heading east or west), the model averages the two
possibilities and lands *between* them — producing a confident,
specific-looking output that's actually wrong.  Classification lets
the model output a *distribution* over headings, capturing bimodality
honestly ("60% east, 40% west") which downstream logic can consume.

### Proposal

Replace the 2-output `(sin θ, cos θ)` head with a 360-output logits
vector — one bin per degree.

- **Head size**: 128 × 360 = ~46k params (vs current 256).  Noise in a
   219k-param model.
- **Peak angular resolution**: 1° — much finer than voyage steering
   needs (~5-10°).
- **Chinese-HWR analogy**: 360 classes is trivial next to 5000+ hand-
   written character classes; class count isn't a concern.

### Solving angular topology

Naive cross-entropy treats class 0° and class 359° as unrelated.  Two
fixes commonly used in orientation-estimation literature:

- **Circular Gaussian soft labels (recommended).**  For target
   heading θ_true, the training target is a distribution:
   `p(θ) ∝ exp(-cos_distance(θ, θ_true)² / (2σ²))`
   with σ ≈ 3-5°.  Standard trick from BiternionNet /
   angle-classification papers.  Wraparound is respected because
   cos_distance handles it naturally.
- **Von Mises loss.**  Fit a Von Mises distribution over the 360 bins
   parameterised by (mean μ, concentration κ).  More elegant but adds
   optimizer complexity.

### Inference

- **Point estimate**: argmax of the softmax output, OR weighted
   circular mean of the top-K bins for smoother predictions.
- **Uncertainty**: entropy of the softmax distribution.  A near-
   uniform output means "I don't know" — much more honest signal for
   the safety net than the current separate `conf` scalar.
- **Multimodal detection**: if there are two peaks > threshold, emit
   both.  Downstream layer picks the one closer to motion bearing /
   PCA / prior heading.

### Wins specific to our failure modes

- **t83/t84 antipode collapse.**  A model that outputs high
   probability at both 30° and 210° gives the safety net enough
   information to pick correctly; a regression that outputs 120° (the
   midpoint) does not.
- **Ambiguous partial occlusion (t229-t235).**  A wide unimodal
   distribution ≠ a narrow peak ≠ a bimodal spread.  Currently we
   can't distinguish these cases; classification would.
- **Well-calibrated uncertainty.**  Entropy is a much more reliable
   confidence signal than the trained `conf` scalar, which we've
   observed drifts (e.g. 0.86 confidence on t84 while completely
   wrong).

### Composes with v15

The heading head is the only piece that changes; the multi-head
architecture from v15 (segmentation + heading + bow) becomes:

```
encoder → 3 heads:
    ship_mask         : 1×80×80
    heading_logits    : 360   ← softmax over 1° bins
    bow_end_prob      : 1
```

`L_pca_align` needs a re-formulation because heading is no longer a
single angle — use the expected heading (circular mean of the softmax
distribution) as the differentiable proxy, or compare PCA angle to
the softmax's peak bin.

### Success criteria for v16

- **All v15 criteria**, plus:
- **Bimodal outputs on t83/t84**: manually verify the softmax
   distribution has two significant peaks (both > 15% probability)
   on antipode-ambiguous frames.
- **Entropy correlates with actual error**: on a held-out set,
   sort ticks by entropy; the high-entropy quintile should have
   meaningfully higher heading error than the low-entropy quintile.
   Currently our `conf` scalar has near-zero correlation with error.
- **Safety-net simplification**: with honest bimodal output, the
   current `pca_tiebreak` + `physics_reject` rules can be replaced
   with a single "pick the mode closer to motion bearing" step.

### When to ship

- **v15 first, then v16** if the shape-prior gives a big enough clean-
   metric boost to build on.
- **v16 alone** if v15 doesn't move hard cases much — classification
   may be the more direct fix for our specific failure distribution.
- **v15 + v16 together** as v17 if both help independently.

## Open questions

- **PCA differentiability.**  We need to backprop through
   `PCA(predicted_mask)`.  The 2×2 case has a closed-form
   eigendecomposition; check that PyTorch handles the numeric edge
   cases (near-degenerate mask, single pixel) without NaN gradients.
- **Symmetry loss coordinate frame.**  Rotating the mask each step
   is ~2ms on 80×80 with `grid_sample`.  For batch 64 that's ~128ms
   of extra compute per step — acceptable but worth profiling.
- **Bow-end supervision quality.**  Depending on how well the
   ground-truth mask captures the bow point, `L_bow` supervision
   might be noisier than expected.  Consider deriving `true_bow_end`
   analytically from `true_heading + centroid + mask` at training
   time rather than storing it.
