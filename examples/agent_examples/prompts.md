# ==============================================================================
# AgoraAgent Test Prompts
#
# Each section has a description and a copy-pasteable prompt inside ```  ```.
# ==============================================================================


# 1. DataLake + GPU OPF
# Purpose: Verify data lake artifact discovery and GPU-accelerated OPF

```
Run optimal power flow on a Texas grid using elec_s100_c50_ec_lv1.0_1H_E.nc in datalake
```


# 2. Programmatic Tool Use (tool chaining in code execution)
# Purpose: Verify tools called via execute_powergrid_code are traced
#          and results can be used across multiple calls

```
I need to run optimal power flow on a synthetic network.

Steps:
1. Create a synthetic 5-bus test grid with generators and loads.
2. Using the run_opf tool, run optimal power flow with the synthetic grid.
3. Then use the returned result to print the generator dispatch summary and compute total generation cost.
```


# 3. Cross-Server: Powergrid + Foundry (OPF → Deep Research)
# Purpose: Verify agent can chain tools across two different servers
#          (powergrid server for run_opf, foundry server for deep_research)

```
1. Run optimal power flow on the Texas grid using elec_s100_c50_ec_lv1.0_1H_E.nc in the datalake.
2. Based on the generation mix in the OPF results, use deep research to investigate what policy incentives or grid upgrades could further reduce carbon emissions in the Texas power grid.
```

# 4. Test skills
```
Analyze the topology of the network in texas_elec_no_flex_s100_c50_ec_lv1.0_1H_E.nc. 
    How many buses, lines, and generators does it have? Is the network fully connected? 
    Are there any critical lines (bridges) whose failure would disconnect the network?
```