"""
Live integration tests for DWSIM tools via the MCP interface.

These tests require the dwsim-server to be running on localhost:8004.

Setup:
1. Ensure you're logged in with Azure CLI: az login

2. Set required environment variables (in .env file or shell):
   export ENTRA_CLIENT_ID=<your-app-client-id>
   export ENTRA_TENANT_ID=<your-tenant-id>
   export OBO_SIMULATION_MODE=true

3. Start the server:
   cd code_execution/docker && docker compose up dwsim-server --build

4. Run tests:
   pytest domains/tests/test_dwsim_tools.py -v -m live
"""

import json
import logging

import pytest

# Suppress httpx logging during tests
logging.getLogger("httpx").setLevel(logging.WARNING)

DWSIM_MCP_URL = "http://localhost:8004/mcp"
DWSIM_HEALTH_URL = "http://localhost:8004"


def parse_tool_result(result):
    """Parse MCP tool call result and extract the response data."""
    assert len(result.content) > 0
    return json.loads(result.content[0].text)


# =========================================================================
# Server connectivity
# =========================================================================


@pytest.mark.live
@pytest.mark.asyncio
class TestDWSIMServerLive:
    """Basic server connectivity and health checks."""

    async def test_server_health(self):
        """Health endpoint should report healthy."""
        import requests

        resp = requests.get(f"{DWSIM_HEALTH_URL}/health", timeout=10)
        assert resp.status_code == 200

        data = resp.json()
        assert data["status"] == "healthy"
        assert data["environment"] == "dwsim"

    async def test_mcp_requires_auth(self):
        """MCP endpoint should reject unauthenticated requests."""
        import requests

        resp = requests.post(f"{DWSIM_MCP_URL}", json={}, timeout=10)
        assert resp.status_code == 401

    async def test_list_tools(self, authenticated_client_session):
        """Server should advertise all DWSIM tools."""
        async with authenticated_client_session(url=DWSIM_MCP_URL) as session:
            await session.initialize()
            tools_result = await session.list_tools()

        tool_names = sorted(t.name for t in tools_result.tools)

        # Spot-check a representative subset
        for expected in [
            "create_flowsheet",
            "solve_flowsheet",
            "add_material_stream",
            "add_heater",
            "get_stream_results",
            "run_sensitivity_analysis",
        ]:
            assert expected in tool_names, f"{expected} missing from {tool_names}"


# =========================================================================
# Flowsheet lifecycle
# =========================================================================


@pytest.mark.live
@pytest.mark.asyncio
class TestFlowsheetLifecycle:
    """Test creating, solving, saving, and loading flowsheets."""

    async def test_create_flowsheet(self, authenticated_client_session):
        """create_flowsheet should return a handle."""
        async with authenticated_client_session(url=DWSIM_MCP_URL) as session:
            await session.initialize()

            result = await session.call_tool(
                "create_flowsheet",
                arguments={
                    "compounds": "Water,Ethanol",
                    "property_package": "Peng-Robinson",
                },
            )

        data = parse_tool_result(result)
        assert data["success"] is True, f"create_flowsheet failed: {data.get('error')}"
        assert "handles" in data
        assert "flowsheet" in data["handles"]
        assert data["handles"]["flowsheet"]["handle"].startswith("h_")

    async def test_create_flowsheet_bad_package(self, authenticated_client_session):
        """Unknown property package should return an error, not crash."""
        async with authenticated_client_session(url=DWSIM_MCP_URL) as session:
            await session.initialize()

            result = await session.call_tool(
                "create_flowsheet",
                arguments={
                    "compounds": "Water",
                    "property_package": "NonexistentPackage",
                },
            )

        data = parse_tool_result(result)
        assert data["success"] is False
        assert "NonexistentPackage" in (data.get("error") or "")

    async def test_solve_empty_flowsheet(self, authenticated_client_session):
        """Solving an empty flowsheet should succeed (trivially converged)."""
        async with authenticated_client_session(url=DWSIM_MCP_URL) as session:
            await session.initialize()

            # Create
            create = await session.call_tool(
                "create_flowsheet",
                arguments={
                    "compounds": "Water",
                    "property_package": "Steam Tables (IAPWS-IF97)",
                },
            )
            create_data = parse_tool_result(create)
            assert create_data["success"] is True
            handle = create_data["handles"]["flowsheet"]["handle"]

            # Solve
            solve = await session.call_tool(
                "solve_flowsheet",
                arguments={"flowsheet": handle},
            )
            solve_data = parse_tool_result(solve)
            assert solve_data["success"] is True


# =========================================================================
# Streams
# =========================================================================


@pytest.mark.live
@pytest.mark.asyncio
class TestStreams:
    """Test adding material and energy streams."""

    async def test_add_material_stream(self, authenticated_client_session):
        """Add a material stream and confirm success."""
        async with authenticated_client_session(url=DWSIM_MCP_URL) as session:
            await session.initialize()

            create = await session.call_tool(
                "create_flowsheet",
                arguments={
                    "compounds": "Water,Ethanol",
                    "property_package": "NRTL",
                },
            )
            fs = parse_tool_result(create)["handles"]["flowsheet"]["handle"]

            result = await session.call_tool(
                "add_material_stream",
                arguments={
                    "flowsheet": fs,
                    "name": "FEED",
                    "temperature": 350.0,
                    "pressure": 101325.0,
                    "compound_mole_fractions": json.dumps({"Water": 0.6, "Ethanol": 0.4}),
                    "total_molar_flow": 100.0,
                },
            )
            data = parse_tool_result(result)
            assert data["success"] is True, f"add_material_stream failed: {data.get('error')}"
            assert data["stream_name"] == "FEED"

    async def test_add_energy_stream(self, authenticated_client_session):
        """Add an energy stream and confirm success."""
        async with authenticated_client_session(url=DWSIM_MCP_URL) as session:
            await session.initialize()

            create = await session.call_tool(
                "create_flowsheet",
                arguments={
                    "compounds": "Water",
                    "property_package": "Steam Tables (IAPWS-IF97)",
                },
            )
            fs = parse_tool_result(create)["handles"]["flowsheet"]["handle"]

            result = await session.call_tool(
                "add_energy_stream",
                arguments={"flowsheet": fs, "name": "Q-HEAT"},
            )
            data = parse_tool_result(result)
            assert data["success"] is True
            assert data["stream_name"] == "Q-HEAT"


# =========================================================================
# Unit operations
# =========================================================================


@pytest.mark.live
@pytest.mark.asyncio
class TestUnitOperations:
    """Test adding various unit operations to a flowsheet."""

    async def _make_flowsheet(self, session, compounds="Water,Ethanol", pp="NRTL"):
        """Helper — create flowsheet and return handle string."""
        create = await session.call_tool(
            "create_flowsheet",
            arguments={"compounds": compounds, "property_package": pp},
        )
        return parse_tool_result(create)["handles"]["flowsheet"]["handle"]

    async def test_add_heater(self, authenticated_client_session):
        async with authenticated_client_session(url=DWSIM_MCP_URL) as session:
            await session.initialize()
            fs = await self._make_flowsheet(session)

            # Need inlet & outlet streams first
            await session.call_tool(
                "add_material_stream",
                arguments={
                    "flowsheet": fs,
                    "name": "S-IN",
                    "temperature": 300.0,
                    "pressure": 101325.0,
                    "compound_mole_fractions": json.dumps({"Water": 0.5, "Ethanol": 0.5}),
                    "total_molar_flow": 50.0,
                },
            )
            await session.call_tool(
                "add_material_stream",
                arguments={
                    "flowsheet": fs,
                    "name": "S-OUT",
                    "temperature": 300.0,
                    "pressure": 101325.0,
                    "compound_mole_fractions": json.dumps({"Water": 0.5, "Ethanol": 0.5}),
                    "total_molar_flow": 50.0,
                },
            )
            await session.call_tool(
                "add_energy_stream",
                arguments={
                    "flowsheet": fs,
                    "name": "Q-1",
                },
            )

            result = await session.call_tool(
                "add_heater",
                arguments={
                    "flowsheet": fs,
                    "name": "HTR-1",
                    "inlet_stream_name": "S-IN",
                    "outlet_stream_name": "S-OUT",
                    "energy_stream_name": "Q-1",
                    "outlet_temperature": 400.0,
                    "pressure_drop": 0.0,
                },
            )
            data = parse_tool_result(result)
            assert data["success"] is True, f"add_heater failed: {data.get('error')}"
            assert data["unit_name"] == "HTR-1"

    async def test_add_cooler(self, authenticated_client_session):
        async with authenticated_client_session(url=DWSIM_MCP_URL) as session:
            await session.initialize()
            fs = await self._make_flowsheet(session)

            await session.call_tool(
                "add_material_stream",
                arguments={
                    "flowsheet": fs,
                    "name": "HOT-IN",
                    "temperature": 400.0,
                    "pressure": 101325.0,
                    "compound_mole_fractions": json.dumps({"Water": 1.0}),
                    "total_molar_flow": 50.0,
                },
            )
            await session.call_tool(
                "add_material_stream",
                arguments={
                    "flowsheet": fs,
                    "name": "COLD-OUT",
                    "temperature": 400.0,
                    "pressure": 101325.0,
                    "compound_mole_fractions": json.dumps({"Water": 1.0}),
                    "total_molar_flow": 50.0,
                },
            )
            await session.call_tool(
                "add_energy_stream",
                arguments={
                    "flowsheet": fs,
                    "name": "Q-CLR",
                },
            )

            result = await session.call_tool(
                "add_cooler",
                arguments={
                    "flowsheet": fs,
                    "name": "CLR-1",
                    "inlet_stream_name": "HOT-IN",
                    "outlet_stream_name": "COLD-OUT",
                    "energy_stream_name": "Q-CLR",
                    "outlet_temperature": 320.0,
                    "pressure_drop": 0.0,
                },
            )
            data = parse_tool_result(result)
            assert data["success"] is True, f"add_cooler failed: {data.get('error')}"

    async def test_add_mixer(self, authenticated_client_session):
        async with authenticated_client_session(url=DWSIM_MCP_URL) as session:
            await session.initialize()
            fs = await self._make_flowsheet(session)

            for name in ["IN-1", "IN-2", "MIX-OUT"]:
                await session.call_tool(
                    "add_material_stream",
                    arguments={
                        "flowsheet": fs,
                        "name": name,
                        "temperature": 300.0,
                        "pressure": 101325.0,
                        "compound_mole_fractions": json.dumps({"Water": 0.5, "Ethanol": 0.5}),
                        "total_molar_flow": 25.0,
                    },
                )

            result = await session.call_tool(
                "add_mixer",
                arguments={
                    "flowsheet": fs,
                    "name": "MIX-1",
                    "inlet_stream_names": "IN-1,IN-2",
                    "outlet_stream_name": "MIX-OUT",
                },
            )
            data = parse_tool_result(result)
            assert data["success"] is True, f"add_mixer failed: {data.get('error')}"

    async def test_add_valve(self, authenticated_client_session):
        async with authenticated_client_session(url=DWSIM_MCP_URL) as session:
            await session.initialize()
            fs = await self._make_flowsheet(session, compounds="Water", pp="Steam Tables (IAPWS-IF97)")

            for name in ["HP-IN", "LP-OUT"]:
                await session.call_tool(
                    "add_material_stream",
                    arguments={
                        "flowsheet": fs,
                        "name": name,
                        "temperature": 400.0,
                        "pressure": 500000.0,
                        "compound_mole_fractions": json.dumps({"Water": 1.0}),
                        "total_molar_flow": 10.0,
                    },
                )

            result = await session.call_tool(
                "add_valve",
                arguments={
                    "flowsheet": fs,
                    "name": "VLV-1",
                    "inlet_stream_name": "HP-IN",
                    "outlet_stream_name": "LP-OUT",
                    "outlet_pressure": 101325.0,
                },
            )
            data = parse_tool_result(result)
            assert data["success"] is True, f"add_valve failed: {data.get('error')}"

    async def test_add_separator(self, authenticated_client_session):
        async with authenticated_client_session(url=DWSIM_MCP_URL) as session:
            await session.initialize()
            fs = await self._make_flowsheet(session)

            for name in ["SEP-FEED", "SEP-VAP", "SEP-LIQ"]:
                await session.call_tool(
                    "add_material_stream",
                    arguments={
                        "flowsheet": fs,
                        "name": name,
                        "temperature": 350.0,
                        "pressure": 101325.0,
                        "compound_mole_fractions": json.dumps({"Water": 0.7, "Ethanol": 0.3}),
                        "total_molar_flow": 100.0,
                    },
                )

            result = await session.call_tool(
                "add_separator",
                arguments={
                    "flowsheet": fs,
                    "name": "FLASH-1",
                    "inlet_stream_name": "SEP-FEED",
                    "vapor_outlet_name": "SEP-VAP",
                    "liquid_outlet_name": "SEP-LIQ",
                    "temperature": 350.0,
                    "pressure": 101325.0,
                },
            )
            data = parse_tool_result(result)
            assert data["success"] is True, f"add_separator failed: {data.get('error')}"

    async def test_add_pump(self, authenticated_client_session):
        async with authenticated_client_session(url=DWSIM_MCP_URL) as session:
            await session.initialize()
            fs = await self._make_flowsheet(session, compounds="Water", pp="Steam Tables (IAPWS-IF97)")

            await session.call_tool(
                "add_material_stream",
                arguments={
                    "flowsheet": fs,
                    "name": "P-IN",
                    "temperature": 300.0,
                    "pressure": 101325.0,
                    "compound_mole_fractions": json.dumps({"Water": 1.0}),
                    "total_molar_flow": 50.0,
                },
            )
            await session.call_tool(
                "add_material_stream",
                arguments={
                    "flowsheet": fs,
                    "name": "P-OUT",
                    "temperature": 300.0,
                    "pressure": 101325.0,
                    "compound_mole_fractions": json.dumps({"Water": 1.0}),
                    "total_molar_flow": 50.0,
                },
            )
            await session.call_tool(
                "add_energy_stream",
                arguments={
                    "flowsheet": fs,
                    "name": "W-PUMP",
                },
            )

            result = await session.call_tool(
                "add_pump",
                arguments={
                    "flowsheet": fs,
                    "name": "PMP-1",
                    "inlet_stream_name": "P-IN",
                    "outlet_stream_name": "P-OUT",
                    "energy_stream_name": "W-PUMP",
                    "outlet_pressure": 500000.0,
                },
            )
            data = parse_tool_result(result)
            assert data["success"] is True, f"add_pump failed: {data.get('error')}"


# =========================================================================
# Results extraction
# =========================================================================


@pytest.mark.live
@pytest.mark.asyncio
class TestResults:
    """Test reading results from a solved flowsheet."""

    async def test_get_flowsheet_summary_empty(self, authenticated_client_session):
        """Summary of an empty flowsheet should succeed with no objects."""
        async with authenticated_client_session(url=DWSIM_MCP_URL) as session:
            await session.initialize()

            create = await session.call_tool(
                "create_flowsheet",
                arguments={
                    "compounds": "Water",
                    "property_package": "Steam Tables (IAPWS-IF97)",
                },
            )
            fs = parse_tool_result(create)["handles"]["flowsheet"]["handle"]

            result = await session.call_tool(
                "get_flowsheet_summary",
                arguments={"flowsheet": fs},
            )
            data = parse_tool_result(result)
            assert data["success"] is True
            assert data["convergence_status"] == "converged"

    async def test_get_stream_results_not_found(self, authenticated_client_session):
        """Querying a non-existent stream should return an error, not crash."""
        async with authenticated_client_session(url=DWSIM_MCP_URL) as session:
            await session.initialize()

            create = await session.call_tool(
                "create_flowsheet",
                arguments={
                    "compounds": "Water",
                    "property_package": "Steam Tables (IAPWS-IF97)",
                },
            )
            fs = parse_tool_result(create)["handles"]["flowsheet"]["handle"]

            result = await session.call_tool(
                "get_stream_results",
                arguments={"flowsheet": fs, "stream_name": "NONEXISTENT"},
            )
            data = parse_tool_result(result)
            assert data["success"] is False
            assert "not found" in (data.get("error") or "").lower()

    async def test_get_unit_operation_results_not_found(self, authenticated_client_session):
        """Querying a non-existent unit should return an error, not crash."""
        async with authenticated_client_session(url=DWSIM_MCP_URL) as session:
            await session.initialize()

            create = await session.call_tool(
                "create_flowsheet",
                arguments={
                    "compounds": "Water",
                    "property_package": "Steam Tables (IAPWS-IF97)",
                },
            )
            fs = parse_tool_result(create)["handles"]["flowsheet"]["handle"]

            result = await session.call_tool(
                "get_unit_operation_results",
                arguments={"flowsheet": fs, "unit_name": "NONEXISTENT"},
            )
            data = parse_tool_result(result)
            assert data["success"] is False
            assert "not found" in (data.get("error") or "").lower()


# =========================================================================
# End-to-end workflow: build → solve → inspect
# =========================================================================


@pytest.mark.live
@pytest.mark.asyncio
class TestEndToEndWorkflow:
    """
    Build a simple heater flowsheet, solve it, and read results.

    Water at 300 K / 1 atm is heated to 400 K.
    """

    async def test_heater_workflow(self, authenticated_client_session):
        async with authenticated_client_session(url=DWSIM_MCP_URL) as session:
            await session.initialize()

            # 1. Create flowsheet
            create = await session.call_tool(
                "create_flowsheet",
                arguments={
                    "compounds": "Water",
                    "property_package": "Steam Tables (IAPWS-IF97)",
                },
            )
            fs = parse_tool_result(create)["handles"]["flowsheet"]["handle"]

            # 2. Add streams
            await session.call_tool(
                "add_material_stream",
                arguments={
                    "flowsheet": fs,
                    "name": "INLET",
                    "temperature": 300.0,
                    "pressure": 101325.0,
                    "compound_mole_fractions": json.dumps({"Water": 1.0}),
                    "total_molar_flow": 100.0,
                },
            )
            await session.call_tool(
                "add_material_stream",
                arguments={
                    "flowsheet": fs,
                    "name": "OUTLET",
                    "temperature": 300.0,
                    "pressure": 101325.0,
                    "compound_mole_fractions": json.dumps({"Water": 1.0}),
                    "total_molar_flow": 100.0,
                },
            )
            await session.call_tool(
                "add_energy_stream",
                arguments={
                    "flowsheet": fs,
                    "name": "Q-HTR",
                },
            )

            # 3. Add heater
            heat = await session.call_tool(
                "add_heater",
                arguments={
                    "flowsheet": fs,
                    "name": "HTR-1",
                    "inlet_stream_name": "INLET",
                    "outlet_stream_name": "OUTLET",
                    "energy_stream_name": "Q-HTR",
                    "outlet_temperature": 400.0,
                    "pressure_drop": 0.0,
                },
            )
            assert parse_tool_result(heat)["success"] is True

            # 4. Solve
            solve = await session.call_tool(
                "solve_flowsheet",
                arguments={
                    "flowsheet": fs,
                },
            )
            solve_data = parse_tool_result(solve)
            assert solve_data["success"] is True, f"Solve failed: {solve_data}"

            # 5. Read outlet stream results
            stream_res = await session.call_tool(
                "get_stream_results",
                arguments={
                    "flowsheet": fs,
                    "stream_name": "OUTLET",
                },
            )
            sr = parse_tool_result(stream_res)
            assert sr["success"] is True, f"get_stream_results failed: {sr.get('error')}"
            # Outlet temperature should be ~400 K
            assert sr["temperature"] is not None
            assert abs(sr["temperature"] - 400.0) < 1.0, f"Expected ~400 K outlet, got {sr['temperature']}"

            # 6. Read heater duty
            unit_res = await session.call_tool(
                "get_unit_operation_results",
                arguments={
                    "flowsheet": fs,
                    "unit_name": "HTR-1",
                },
            )
            ur = parse_tool_result(unit_res)
            assert ur["success"] is True, f"get_unit_operation_results failed: {ur.get('error')}"
            # Heater duty should be positive (heating)
            if ur["duty"] is not None:
                assert ur["duty"] > 0, f"Heater duty should be positive, got {ur['duty']}"

            # 7. Overall summary
            summary = await session.call_tool(
                "get_flowsheet_summary",
                arguments={
                    "flowsheet": fs,
                },
            )
            sm = parse_tool_result(summary)
            assert sm["success"] is True

    async def test_flash_separator_workflow(self, authenticated_client_session):
        """
        Binary water-ethanol mixture flashed at 350 K / 1 atm.

        After solving we expect a vapour and a liquid phase with
        different compositions.
        """
        async with authenticated_client_session(url=DWSIM_MCP_URL) as session:
            await session.initialize()

            # 1. Flowsheet
            create = await session.call_tool(
                "create_flowsheet",
                arguments={
                    "compounds": "Water,Ethanol",
                    "property_package": "NRTL",
                },
            )
            fs = parse_tool_result(create)["handles"]["flowsheet"]["handle"]

            # 2. Streams
            await session.call_tool(
                "add_material_stream",
                arguments={
                    "flowsheet": fs,
                    "name": "FEED",
                    "temperature": 350.0,
                    "pressure": 101325.0,
                    "compound_mole_fractions": json.dumps({"Water": 0.5, "Ethanol": 0.5}),
                    "total_molar_flow": 100.0,
                },
            )
            await session.call_tool(
                "add_material_stream",
                arguments={
                    "flowsheet": fs,
                    "name": "VAPOUR",
                    "temperature": 350.0,
                    "pressure": 101325.0,
                    "compound_mole_fractions": json.dumps({"Water": 0.5, "Ethanol": 0.5}),
                    "total_molar_flow": 50.0,
                },
            )
            await session.call_tool(
                "add_material_stream",
                arguments={
                    "flowsheet": fs,
                    "name": "LIQUID",
                    "temperature": 350.0,
                    "pressure": 101325.0,
                    "compound_mole_fractions": json.dumps({"Water": 0.5, "Ethanol": 0.5}),
                    "total_molar_flow": 50.0,
                },
            )

            # 3. Flash separator
            sep = await session.call_tool(
                "add_separator",
                arguments={
                    "flowsheet": fs,
                    "name": "FLASH-1",
                    "inlet_stream_name": "FEED",
                    "vapor_outlet_name": "VAPOUR",
                    "liquid_outlet_name": "LIQUID",
                    "temperature": 350.0,
                    "pressure": 101325.0,
                },
            )
            assert parse_tool_result(sep)["success"] is True

            # 4. Solve
            solve = await session.call_tool(
                "solve_flowsheet",
                arguments={
                    "flowsheet": fs,
                },
            )
            solve_data = parse_tool_result(solve)
            assert solve_data["success"] is True, f"Solve failed: {solve_data}"

            # 5. Read vapour stream
            vap = await session.call_tool(
                "get_stream_results",
                arguments={
                    "flowsheet": fs,
                    "stream_name": "VAPOUR",
                },
            )
            vr = parse_tool_result(vap)
            assert vr["success"] is True, f"vapour results failed: {vr.get('error')}"

            # 6. Read liquid stream
            liq = await session.call_tool(
                "get_stream_results",
                arguments={
                    "flowsheet": fs,
                    "stream_name": "LIQUID",
                },
            )
            lr = parse_tool_result(liq)
            assert lr["success"] is True, f"liquid results failed: {lr.get('error')}"


# =========================================================================
# Session persistence (handle survives across calls)
# =========================================================================


@pytest.mark.live
@pytest.mark.asyncio
class TestSessionPersistence:
    """Verify that the flowsheet handle persists across multiple tool calls."""

    async def test_handle_persists_across_calls(self, authenticated_client_session):
        """Create a flowsheet, add a stream, then read summary — all via handle."""
        async with authenticated_client_session(url=DWSIM_MCP_URL) as session:
            await session.initialize()

            # Create
            create = await session.call_tool(
                "create_flowsheet",
                arguments={
                    "compounds": "Water",
                    "property_package": "Steam Tables (IAPWS-IF97)",
                },
            )
            fs = parse_tool_result(create)["handles"]["flowsheet"]["handle"]

            # Add a stream (reuses the same handle / session)
            await session.call_tool(
                "add_material_stream",
                arguments={
                    "flowsheet": fs,
                    "name": "S1",
                    "temperature": 300.0,
                    "pressure": 101325.0,
                    "compound_mole_fractions": json.dumps({"Water": 1.0}),
                    "total_molar_flow": 10.0,
                },
            )

            # Summary should show at least the stream we added
            summary = await session.call_tool(
                "get_flowsheet_summary",
                arguments={
                    "flowsheet": fs,
                },
            )
            sm = parse_tool_result(summary)
            assert sm["success"] is True
            assert len(sm["object_list"]) >= 1

    async def test_invalid_handle_returns_error(self, authenticated_client_session):
        """Passing a bogus handle should produce a clear error."""
        async with authenticated_client_session(url=DWSIM_MCP_URL) as session:
            await session.initialize()

            result = await session.call_tool(
                "solve_flowsheet",
                arguments={
                    "flowsheet": "h_bogus_handle_000",
                },
            )

        text = result.content[0].text.lower()
        assert "not found" in text or "error" in text or "handle" in text
