---
name: vitrimer
description: Design and characterize recyclable vitrimer polymers using AI-guided generative design (HierVAE) for candidate discovery and molecular dynamics simulation (LAMMPS + PCFF) for physics-based Tg validation.
---

# Vitrimer Polymer Design & Characterization

Parent skill for all vitrimer polymer workflows. Two complementary sub-skill
trees are available across the vitrimer_vae and vitrimer_tg_sim servers:

## Sub-Skills

- **vitrimer-inverse-design** *(vitrimer_vae server)* — AI-guided inverse design with HierVAE
  - molecule-generation — Sample novel acid/epoxide pairs from the VAE
  - property-prediction — Predict Tg via the VAE property head
  - latent-space-exploration — Search neighbors and interpolate in latent space
  - optimization — Bayesian optimization targeting specific Tg values
  - calibration — GP-based calibration of MD Tg to experimental scale

- **vitrimer-tg-estimation** *(vitrimer_tg_sim server)* — Full MD pipeline for estimating Tg from monomer SMILES
  - build-box — Construct simulation box with EMC + PCFF
  - equilibration — LAMMPS equilibration protocol
  - production — Parallel cooling replicas
  - tg-analysis — Bilinear fitting of density–temperature profiles
