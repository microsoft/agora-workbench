# COCO FSD File Format

COCO (CAPE-OPEN to CAPE-OPEN) simulator stores flowsheets as `.fsd` files.

## Structure

An `.fsd` file is a **ZIP archive** containing a single file:
`Flowsheet.xml` — the full flowsheet definition in XML.

## XML Schema Overview

```xml
<Flowsheet>
  <!-- Compounds used in the simulation -->
  <compound name="Benzene">
    <CAS>71-43-2</CAS>
    <formula>C6H6</formula>
  </compound>

  <!-- Property package (CAPE-OPEN based) -->
  <propertyPackage name="TEA Package">
    <CLSID>{...GUID...}</CLSID>
  </propertyPackage>

  <!-- Streams with specified and solved values -->
  <stream>
    <object name="Feed"/>
    <specifiedTemperature>320.0</specifiedTemperature>
    <specifiedPressure>2026500.0</specifiedPressure>
    <specifiedFlowRate>619.611</specifiedFlowRate>
    <specifiedMoleFraction>0.7168;0.2827;0.000435;0</specifiedMoleFraction>

    <!-- Solved phase data -->
    <phase name="Overall">
      <moleFraction>0.7168;0.2827;0.000435;0</moleFraction>
      <temperature>320.0</temperature>
      <pressure>2026500.0</pressure>
      <totalFlow>619.611</totalFlow>
    </phase>
    <phase name="Vapor">
      <moleFraction>...</moleFraction>
    </phase>
    <phase name="Liquid">
      <moleFraction>...</moleFraction>
    </phase>
  </stream>

  <!-- Unit operations with port connections -->
  <unitOperation>
    <object name="CSTR-1"/>
    <type>CSTR</type>
    <connection port="Inlet" feed="true" type="material">Feed</connection>
    <connection port="Outlet" feed="false" type="material">Product</connection>
  </unitOperation>

  <!-- Reaction package (COCO-specific, not portable) -->
  <reactionPackage name="Kinetic Reactions">
    <CLSID>{...GUID...}</CLSID>
  </reactionPackage>
</Flowsheet>
```

## Key Parsing Notes

- **Compound order matters**: mole fraction lists are semicolon-delimited and
  correspond to compounds in document order.
- **Stream names** are in `<object name="...">` child elements, not stream
  attributes.
- **Connections** on unit operations specify `feed="true"` for inlets and
  `feed="false"` for outlets. The text content is the stream name.
- **Solved data** in `<phase name="Overall">` contains the converged results
  from the COCO simulation, including compositions, temperature, pressure,
  and flow rates.
- **Reaction packages** use COM CLSIDs and are not directly portable to DWSIM.
  The converter tool back-calculates stoichiometry from the solved data instead.
