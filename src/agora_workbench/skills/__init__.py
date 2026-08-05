"""Agent skills shipped with the workbench.

The skill trees under this package are data, not importable modules. Their
directory names follow the Agent Skills convention (hyphenated, e.g.
``agora-workbench``) and are therefore not valid Python identifiers; they are
shipped via ``package-data`` rather than as subpackages.

They live here rather than at the repository root so that ``pip install
agora-workbench`` puts them on disk, where
:func:`agora_workbench.deployment.cli.install_skill` can copy them into an
agent's skills directory.
"""
