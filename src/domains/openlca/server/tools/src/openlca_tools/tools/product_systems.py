"""Product system creation tool for OpenLCA."""

import os


def create_product_system(process_name: str, config: dict | None = None) -> dict:
    """
    Create a product system from a reference process in OpenLCA.

    The created product system is stored in the session's ObjectRegistry
    and returned as a handle for use with impact assessment tools.

    Args:
        process_name: Name of the reference process.
        config: Optional configuration for product system creation.
                Supported keys:
                  - cutoff (float): Cutoff threshold (default 0.0).
                  - link_strategy (str): 'PREFER_UNIT_PROCESSES' or
                    'PREFER_SYSTEM_PROCESSES' (default 'PREFER_UNIT_PROCESSES').

    Returns:
        Dict with 'product_system' key containing the created product system
        (stored as a handle).
    """
    try:
        import olca_ipc as ipc
        import olca_schema as o

        host = os.getenv("OLCA_IPC_HOST", "openlca-ipc")
        client = ipc.Client(port=8080, host=host)

        # Find reference process
        processes = client.get_descriptors(o.Process)
        ref_process = next(
            (p for p in processes if p.name == process_name),
            None,
        )
        if ref_process is None:
            return {"product_system": {"error": f"Process '{process_name}' not found."}}

        # Build creation config
        cfg = config or {}
        creation_config = o.LinkingConfig(
            cutoff=cfg.get("cutoff", 0.0),
            prefer_unit_processes=cfg.get("link_strategy", "PREFER_UNIT_PROCESSES") == "PREFER_UNIT_PROCESSES",
        )

        # Create the product system
        product_system = client.create_product_system(
            ref_process.id,
            creation_config,
        )

        return {"product_system": product_system}

    except Exception as e:
        return {"product_system": {}, "error": str(e)}
