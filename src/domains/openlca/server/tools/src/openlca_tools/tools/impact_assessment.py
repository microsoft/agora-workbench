"""Impact assessment tool for OpenLCA."""

import os


def run_impact_assessment(product_system_name: str, impact_method: str) -> dict:
    """
    Run a life cycle impact assessment for a product system.

    Connects to the OpenLCA IPC server and calculates the environmental
    impact of the specified product system using the given impact method.

    Args:
        product_system_name: Name of the product system to assess.
        impact_method: Name of the LCIA method (e.g., 'ReCiPe 2016 Midpoint (H)').

    Returns:
        Dict with 'results' key containing impact category scores.
    """
    try:
        import olca_ipc as ipc
        import olca_schema as o

        host = os.getenv("OLCA_IPC_HOST", "openlca-ipc")
        client = ipc.Client(port=8080, host=host)

        # Find the product system by name
        product_systems = client.get_all(o.ProductSystem)
        target_system = next(
            (ps for ps in product_systems if ps.name == product_system_name),
            None,
        )
        if target_system is None:
            return {"results": {"error": f"Product system '{product_system_name}' not found."}}

        # Find the impact method by name
        methods = client.get_all(o.ImpactMethod)
        target_method = next(
            (m for m in methods if m.name == impact_method),
            None,
        )
        if target_method is None:
            return {"results": {"error": f"Impact method '{impact_method}' not found."}}

        # Set up and run the calculation
        setup = o.CalculationSetup(
            target=o.Ref(ref_type=o.RefType.ProductSystem, id=target_system.id),
            impact_method=o.Ref(ref_type=o.RefType.ImpactMethod, id=target_method.id),
            calculation_type=o.CalculationType.SIMPLE_CALCULATION,
            amount=1.0,
        )
        result = client.calculate(setup)
        result.wait_until_ready()

        # Extract impact results
        impact_results = {}
        for impact in result.get_total_impacts():
            category_name = impact.impact_category.name if impact.impact_category else "unknown"
            impact_results[category_name] = {
                "amount": impact.amount,
                "unit": impact.impact_category.ref_unit if impact.impact_category else "",
            }

        result.dispose()
        return {"results": impact_results}

    except Exception as e:
        return {"results": {}, "error": str(e)}
