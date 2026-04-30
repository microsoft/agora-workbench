"""Database and process listing tools for OpenLCA."""

import os


def list_databases() -> dict:
    """
    List all databases available on the connected OpenLCA IPC server.

    Returns:
        Dict with 'databases' key containing a list of database names.
    """
    try:
        import olca_ipc as ipc

        host = os.getenv("OLCA_IPC_HOST", "openlca-ipc")
        client = ipc.Client(port=8080, host=host)

        databases = client.get_databases()
        db_names = [db if isinstance(db, str) else str(db) for db in databases]
        return {"databases": db_names}

    except Exception as e:
        return {"databases": [], "error": str(e)}


def list_processes(process_filter: str = "") -> dict:
    """
    List processes in the active OpenLCA database.

    Optionally filter by a search string (case-insensitive substring match
    on process name).

    Args:
        process_filter: Optional filter string applied to process names.

    Returns:
        Dict with 'processes' key containing a list of process descriptors.
    """
    try:
        import olca_ipc as ipc
        import olca_schema as o

        host = os.getenv("OLCA_IPC_HOST", "openlca-ipc")
        client = ipc.Client(port=8080, host=host)

        descriptors = client.get_descriptors(o.Process)
        process_list = []
        filter_lower = process_filter.lower() if process_filter else ""

        for desc in descriptors:
            name = desc.name or ""
            if filter_lower and filter_lower not in name.lower():
                continue
            process_list.append(
                {
                    "id": desc.id,
                    "name": name,
                    "category": desc.category or "",
                }
            )

        return {"processes": process_list}

    except Exception as e:
        return {"processes": [], "error": str(e)}
