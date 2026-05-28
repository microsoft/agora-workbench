"""Chemistry domain state vocabulary.

Defines the canonical state tokens for the chemistry tool graph.
Each token represents a meaningful intermediate artifact that downstream
tools can consume.

Note: This module lives under ``domain_examples.chemistry`` rather than
``domains.chemistry``, so it is **not** auto-discovered by the default
``StateGraph`` loader (which searches ``domains/*/states.py``).  For
production deployments, either move the domain under ``domains/`` or
extend ``StateGraph._load_domain_states`` to search additional paths.
"""

from enum import Enum


class ChemistryState(str, Enum):
    """State tokens for the chemistry domain tool graph.

    The graph flows:

        parse_molecule ─────► MOLECULE_PARSED
                                    │
                    ┌───────────────┼───────────────────┐
                    ▼               ▼                   ▼
        enumerate_functional   compute_descriptors  compute_fingerprints
            _groups             │                    │
                │               ▼                    ├──────────────┐
                ▼         DESCRIPTORS_COMPUTED       ▼              ▼
        GROUPS_IDENTIFIED       │          FINGERPRINTS_COMPUTED    │
                                ▼                    │              │
                        filter_drug_candidates       ▼              ▼
                                │          find_similar_molecules  cluster_molecules
                                ▼                    │              │
                        CANDIDATES_FILTERED          ▼              ▼
                                            SIMILARITY_COMPUTED  MOLECULES_CLUSTERED
    """

    MOLECULE_PARSED = "chemistry.molecule_parsed"
    GROUPS_IDENTIFIED = "chemistry.groups_identified"
    DESCRIPTORS_COMPUTED = "chemistry.descriptors_computed"
    CANDIDATES_FILTERED = "chemistry.candidates_filtered"
    FINGERPRINTS_COMPUTED = "chemistry.fingerprints_computed"
    SIMILARITY_COMPUTED = "chemistry.similarity_computed"
    MOLECULES_CLUSTERED = "chemistry.molecules_clustered"


STATE_AFFORDANCES = {
    ChemistryState.MOLECULE_PARSED: [
        "validate a SMILES string",
        "get the canonical form of a molecule",
        "identify a molecule from SMILES",
    ],
    ChemistryState.GROUPS_IDENTIFIED: [
        "identify functional groups in a molecule",
        "find hydroxyl, carboxyl, amine, or other groups",
    ],
    ChemistryState.DESCRIPTORS_COMPUTED: [
        "compute molecular properties",
        "calculate LogP, TPSA, or molecular weight",
        "evaluate drug-likeness",
    ],
    ChemistryState.CANDIDATES_FILTERED: [
        "screen compounds for drug-likeness",
        "filter molecules by Lipinski or Veber rules",
        "identify drug candidates",
    ],
    ChemistryState.FINGERPRINTS_COMPUTED: [
        "generate molecular fingerprints",
        "compute Morgan or MACCS fingerprints",
        "prepare molecules for similarity or clustering",
    ],
    ChemistryState.SIMILARITY_COMPUTED: [
        "compare molecular similarity",
        "rank molecules by Tanimoto similarity",
        "virtual screening by similarity",
    ],
    ChemistryState.MOLECULES_CLUSTERED: [
        "cluster a molecular library",
        "group similar molecules together",
        "chemical series analysis",
    ],
}
