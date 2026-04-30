"""Scenario comparison tool for OpenLCA."""

import os


def compare_scenarios(product_systems: list, impact_method: str) -> dict:
    """
    Compare the environmental impact of multiple product systems.

    Runs impact assessments for each product system using the specified
    impact method and returns a structured comparison.

    Args:
        product_systems: List of product system names to compare.
        impact_method: Name of the LCIA method to use for all comparisons.

    Returns:
        Dict with 'comparison' key containing impact results per product system.
    """
    try:
        import olca_ipc as ipc
        import olca_schema as o

        host = os.getenv("OLCA_IPC_HOST", "openlca-ipc")
        client = ipc.Client(port=8080, host=host)

        # Find the impact method
        methods = client.get_all(o.ImpactMethod)
        target_method = next(
            (m for m in methods if m.name == impact_method),
            None,
        )
        if target_method is None:
            return {"comparison": {"error": f"Impact method '{impact_method}' not found."}}

        # Retrieve all product systems once
        all_systems = client.get_all(o.ProductSystem)
        system_map = {ps.name: ps for ps in all_systems}

        comparison = {}
        for ps_name in product_systems:
            system = system_map.get(ps_name)
            if system is None:
                comparison[ps_name] = {"error": f"Product system '{ps_name}' not found."}
                continue

            setup = o.CalculationSetup(
                target=o.Ref(ref_type=o.RefType.ProductSystem, id=system.id),
                impact_method=o.Ref(ref_type=o.RefType.ImpactMethod, id=target_method.id),
                calculation_type=o.CalculationType.SIMPLE_CALCULATION,
                amount=1.0,
            )
            result = client.calculate(setup)
            result.wait_until_ready()

            impacts = {}
            for impact in result.get_total_impacts():
                category_name = impact.impact_category.name if impact.impact_category else "unknown"
                impacts[category_name] = {
                    "amount": impact.amount,
                    "unit": impact.impact_category.ref_unit if impact.impact_category else "",
                }

            result.dispose()
            comparison[ps_name] = impacts

        return {"comparison": comparison}

    except Exception as e:
        return {"comparison": {}, "error": str(e)}
