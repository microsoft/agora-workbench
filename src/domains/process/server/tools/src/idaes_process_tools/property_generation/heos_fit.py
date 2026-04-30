from math import log

from idaes.core.util.misc import set_param_from_config
from pyomo.environ import Var, exp, value
from pyomo.environ import units as pyunits


class HEOSFitGAS:
    """
    HEOS_FIT property class for ideal‑gas heat capacity, enthalpy and entropy,
    and vapor pressure, built from pre‑fitted polynomial coefficients in the
    thermo/chemical property package.

    """

    class cp_mol_ig_comp(Var):
        @staticmethod
        def build_parameters(cobj):
            # Heat capacity correlation coefficients
            coeff_dict = cobj.config.parameter_data["cp_mol_ig_comp_coeff"]
            int_keys = sorted(coeff_dict.keys())  # e.g. [0,1,2,3,...,n]

            # Build all parameters for cp_mol_ig_comp_coeff
            for idx, i in enumerate(int_keys):
                # 1) Create a Var for term "i"
                var_name = f"cp_mol_ig_comp_coeff_{i}"
                cp_unit = coeff_dict[i][1]  # e.g. J/mol/K
                setattr(
                    cobj,
                    var_name,
                    Var(
                        doc=f"Polynomial coefficient a_{i} for Cp(T)",
                        units=cp_unit,
                    ),
                )
                # 2) Hook it up to your parameter_data via set_param_from_config
                set_param_from_config(
                    cobj,
                    param="cp_mol_ig_comp_coeff",  # your dict key
                    index=str(i),  # this picks out coeffs[i]
                )

            # Set up the offset and scale for z-scaling
            offset = cobj.config.parameter_data["vap_offset"]
            scale = cobj.config.parameter_data["vap_scale"]
            # 3) Create a Var for the offset
            var_name = "vap_offset"
            setattr(
                cobj,
                var_name,
                Var(
                    doc="Offset for z-scaling in Cp(T) and H(T)",
                    units=offset[1],
                ),
            )
            # Set parameter_data for the offset
            set_param_from_config(
                cobj,
                param="vap_offset",  # your dict key
                index=None,
            )
            var_name = "vap_scale"
            # 4) Create a Var for the scale
            setattr(
                cobj,
                var_name,
                Var(
                    doc="Scale for z-scaling in Cp(T) and H(T) and S(T)",
                    units=scale[1],
                ),
            )
            # Set parameter_data for scale
            set_param_from_config(
                cobj,
                param="vap_scale",  # your dict key
                index=None,  # this picks out coeffs[i]
            )

            # Variables for enthalpy coefficients are the integrated cp coefficients
            b_coeff_dict = cobj.config.parameter_data["cp_mol_ig_comp_int_coeff"]
            b_keys = sorted(b_coeff_dict.keys())  # e.g. [0,1,2,3,...,n]
            for idx, i in enumerate(b_keys):
                # 1) Create a Var for term "i"
                var_name = f"cp_mol_ig_comp_int_coeff_{i}"
                setattr(
                    cobj,
                    var_name,
                    Var(
                        doc=f"Polynomial coefficient b_{i} for H(T)",
                        units=b_coeff_dict[i][1],
                    ),
                )
                # 2) Hook it up to your parameter_data via set_param_from_config
                set_param_from_config(
                    cobj,
                    param="cp_mol_ig_comp_int_coeff",  # your dict key
                    index=str(i),  # this picks out coeffs[i]
                )

            # For entropy correlations we need the integrated coefficients over T:
            coeff_dict = cobj.config.parameter_data["cp_mol_ig_comp_int_T_coeff"]
            int_keys = sorted(coeff_dict.keys())  # e.g. [0,1,2,3,...,n]

            for idx, i in enumerate(int_keys):
                # 1) Create a Var for term "i"
                var_name = f"cp_mol_ig_comp_int_T_coeff_{i}"
                cp_unit = coeff_dict[i][1]  # e.g. J/mol/K
                setattr(
                    cobj,
                    var_name,
                    Var(
                        doc=f"Polynomial coefficient b_{i} for S(T)",
                        units=cp_unit,
                    ),
                )
                # 2) Hook it up to your parameter_data via set_param_from_config
                set_param_from_config(
                    cobj,
                    param="cp_mol_ig_comp_int_T_coeff",  # your dict key
                    index=str(i),  # this picks out coeffs[i]
                )

            # Log coefficient for entropy correlation
            log_coeff = cobj.config.parameter_data["cp_mol_ig_comp_int_T_log_coeff"]
            # 3) Create a Var for the offset
            var_name = "cp_mol_ig_comp_int_T_log_coeff"
            setattr(
                cobj,
                var_name,
                Var(
                    doc="Offset for z-scaling in Cp(T) and H(T)",
                    units=log_coeff[1],
                ),
            )
            # Set parameter_data for the offset
            set_param_from_config(
                cobj,
                param="cp_mol_ig_comp_int_T_log_coeff",  # your dict key
                index=None,
            )

        @staticmethod
        def return_expression(b, cobj, T):
            """
            Ideal‑gas molar heat capacity, Cp(T) = sum_i a_i * T**i
            Coeffs a_i must be stored in b.params.cp_mol_ig_comp_coeff[comp].
            Using the parameters that were built in build_parameters().
            """
            coeffs = cobj.config.parameter_data["cp_mol_ig_comp_coeff"]

            # 3) Build normalized z for the polynomial
            z = cobj.vap_offset + cobj.vap_scale * T
            expr = 0
            for i, _ in coeffs.items():
                a_i = getattr(cobj, f"cp_mol_ig_comp_coeff_{i}")
                expr = expr * z + a_i

            return expr * pyunits.J / pyunits.mol / pyunits.K

    class enth_mol_ig_comp:
        @staticmethod
        def build_parameters(cobj):
            """Should be already done in cp_mol_ig_comp.build_parameters()"""
            # CHeck if the parameters are already built
            if not hasattr(cobj, "cp_mol_ig_comp_int_coeff_0"):
                HEOSFitGAS.cp_mol_ig_comp.build_parameters(cobj)

            # Set formation enthalpy
            if cobj.parent_block().config.include_enthalpy_of_formation:
                units = cobj.parent_block().get_metadata().derived_units

                cobj.enth_mol_form_vap_comp_ref = Var(
                    doc="Vapor phase molar heat of formation @ Tref",
                    units=units.ENERGY_MOLE,
                )
                set_param_from_config(cobj, param="enth_mol_form_vap_comp_ref")

        @staticmethod
        def return_expression(b, cobj, T):
            """
            Ideal‑gas molar enthalpy, using pre‑integrated polynomial
            In thermo: horner_stable(T2, int_coeffs, offset, scale)- horner_stable(T1, int_coeffs, offset, scale)
            https://github.com/CalebBell/thermo/blob/d07fa840ca56f0e6330a03475b28c89c4ab2227a/thermo/utils/t_dependent_property.py
            """
            b_coeffs = cobj.config.parameter_data["cp_mol_ig_comp_int_coeff"]
            T = pyunits.convert(T, to_units=pyunits.K)
            Tr = pyunits.convert(b.params.temperature_ref, to_units=pyunits.K)

            z = cobj.vap_offset + cobj.vap_scale * T
            expr_T = 0
            for i, _ in b_coeffs.items():
                b_i = getattr(cobj, f"cp_mol_ig_comp_int_coeff_{i}")
                expr_T = expr_T * z + b_i

            z_ref = cobj.vap_offset + cobj.vap_scale * Tr
            expr_T_ref = 0
            for i, _ in b_coeffs.items():
                b_i = getattr(cobj, f"cp_mol_ig_comp_int_coeff_{i}")
                expr_T_ref = expr_T_ref * z_ref + b_i

            # TODO: Unit handling a bit messy at the moment because of the horner method
            h = (expr_T - expr_T_ref) * pyunits.J / pyunits.mol

            if hasattr(cobj, "enth_mol_form_vap_comp_ref"):
                h += cobj.enth_mol_form_vap_comp_ref

            return h
            # units = b.params.get_metadata().derived_units
            # return pyunits.convert(expr, units.ENERGY_MOLE)

    class entr_mol_ig_comp:
        @staticmethod
        def build_parameters(cobj):
            """Should be already done in cp_mol_ig_comp.build_parameters()"""
            # CHeck if the parameters are already built
            if not hasattr(cobj, "int_T_coeff_0"):
                HEOSFitGAS.cp_mol_ig_comp.build_parameters(cobj)

            # Standard entropy
            units = cobj.parent_block().get_metadata().derived_units

            cobj.entr_mol_form_vap_comp_ref = Var(
                doc="Vapor phase molar entropy of formation @ Tref",
                units=units.ENTROPY_MOLE,
            )
            set_param_from_config(cobj, param="entr_mol_form_vap_comp_ref")

        @staticmethod
        def return_expression(b, cobj, T):
            """
            Ideal‑gas molar entropy, using pre‑integrated polynomial + log term:
            S(T) = horner_stable_log(T2) - horner_stable_log(Tref) - R*log(P/P_ref)

            Where:
            horner_stable_log(x) = horner_stable(x, coeffs, offset, scale) + b_log * log(x)
            """
            # Units
            T = pyunits.convert(T, to_units=pyunits.K)
            Tr = pyunits.convert(b.params.temperature_ref, to_units=pyunits.K)

            # Get pressure from the state block if available
            # First try to get it from the containing block
            # if hasattr(b, "pressure"):
            #    P = b.pressure
            # Otherwise use reference pressure
            # else:
            #    P = b.params.pressure_ref
            # P_ref = b.params.pressure_ref

            offset = cobj.vap_offset
            scale = cobj.vap_scale
            coeffs = cobj.config.parameter_data["cp_mol_ig_comp_int_T_coeff"]
            log_coeff = cobj.config.parameter_data["cp_mol_ig_comp_int_T_log_coeff"][0]

            # Entropy at T
            z_T = offset + scale * T
            expr_T = 0
            for i, _ in coeffs.items():
                b_i = getattr(cobj, f"cp_mol_ig_comp_int_T_coeff_{i}")
                expr_T = expr_T * z_T + b_i
            # Use value() to extract the numeric value of T for the log function
            tot_expr_T = expr_T + log_coeff * log(value(T))

            # Entropy at Tr
            z_Tr = offset + scale * Tr
            expr_Tr = 0
            for i, _ in coeffs.items():
                b_i = getattr(cobj, f"cp_mol_ig_comp_int_T_coeff_{i}")
                expr_Tr = expr_Tr * z_Tr + b_i
            # Use value() to extract the numeric value of Tr for the log function
            tot_expr_Tr = expr_Tr + log_coeff * log(value(Tr))

            # Basic entropy calculation from temperature dependent part
            S = (tot_expr_T - tot_expr_Tr) * pyunits.J / pyunits.mol / pyunits.K
            # S = (expr_T - expr_Tr) * pyunits.J / pyunits.mol / pyunits.K

            # Add reference entropy if available
            if hasattr(cobj, "entr_mol_form_vap_comp_ref"):
                S += cobj.entr_mol_form_vap_comp_ref

            # Add pressure correction term for ideal gas
            # R = 8.3145 J/mol/K (gas constant)
            # R = 8.3145 * pyunits.J / pyunits.mol / pyunits.K
            # try:
            #    S -= R * log(value(P) / value(P_ref))
            # except:
            #    # If we can't evaluate the pressure, just skip the correction
            #    pass

            return S

    class pressure_sat_comp:
        @staticmethod
        def build_parameters(cobj):
            """Build parameters for vapor pressure calculation using HEOS_FIT.

            Note: The vapor pressure is calculated using a polynomial fit with z-scaling:
            ln(P_sat) = sum_i a_i * z**i
            where z = offset + scale * T
            """
            # Vapor pressure correlation coefficients
            coeff_dict = cobj.config.parameter_data["pressure_sat_comp_coeff"]
            int_keys = sorted(coeff_dict.keys())  # e.g. [0,1,2,3,...,n]

            # Build all parameters for pressure_sat_comp_coeff
            for idx, i in enumerate(int_keys):
                # 1) Create a Var for term "i"
                var_name = f"pressure_sat_comp_coeff_{i}"
                coeff_unit = coeff_dict[i][1]  # Units for pressure coefficients
                setattr(
                    cobj,
                    var_name,
                    Var(
                        doc=f"Polynomial coefficient a_{i} for ln(Psat)",
                        units=coeff_unit,
                    ),
                )
                # 2) Hook it up to your parameter_data via set_param_from_config
                set_param_from_config(
                    cobj,
                    param="pressure_sat_comp_coeff",  # your dict key
                    index=str(i),  # this picks out coeffs[i]
                )

            # Set up the offset and scale for z-scaling
            offset = cobj.config.parameter_data["psat_offset"]
            scale = cobj.config.parameter_data["psat_scale"]

            # 3) Create a Var for the offset
            var_name = "psat_offset"
            setattr(
                cobj,
                var_name,
                Var(
                    doc="Offset for z-scaling in vapor pressure calculation",
                    units=offset[1],
                ),
            )
            # Set parameter_data for the offset
            set_param_from_config(
                cobj,
                param="psat_offset",  # your dict key
                index=None,
            )

            # 4) Create a Var for the scale
            var_name = "psat_scale"
            setattr(
                cobj,
                var_name,
                Var(
                    doc="Scale for z-scaling in vapor pressure calculation",
                    units=scale[1],
                ),
            )
            # Set parameter_data for scale
            set_param_from_config(
                cobj,
                param="psat_scale",  # your dict key
                index=None,
            )

        @staticmethod
        def return_expression(b, cobj, T, dT=False):
            """
            Vapor pressure calculation using HEOS_FIT method with z-scaling.

            The vapor pressure is calculated as:
            ln(P_sat) = sum_i a_i * z**i
            where z = offset + scale * T

            Then:
            P_sat = exp(ln(P_sat))

            Coeffs a_i must be stored in b.params.pressure_sat_comp_coeff[comp].
            """
            if dT:
                return HEOSFitGAS.pressure_sat_comp.dT_expression(b, cobj, T)
            coeffs = cobj.config.parameter_data["pressure_sat_comp_coeff"]

            # Build normalized z for the polynomial
            z = cobj.psat_offset + cobj.psat_scale * T

            # Calculate ln(P_sat) using horner method
            ln_psat = 0
            for i, _ in coeffs.items():
                a_i = getattr(cobj, f"pressure_sat_comp_coeff_{i}")
                ln_psat = ln_psat * z + a_i

            # Convert ln(P_sat) to P_sat using Pyomo's exp
            # In the implementation in thermo (exp_stable_polynomial) they truncate the value if x > 709.7
            # TODO: Check if that's necessary
            psat = exp(ln_psat) * pyunits.Pa

            units = b.params.get_metadata().derived_units
            return pyunits.convert(psat, units.PRESSURE)

        @staticmethod
        def dT_expression(b, cobj, T):
            """
            Derivative of vapor pressure w.r.t. temperature using the HEOS_FIT method.
            Based on thermo's horner_stable_and_der:
                ln(Psat) = sum_i a_i * z^i
                d(ln(Psat))/dT = d(ln(Psat))/dz * dz/dT = d_poly * scale
                d(Psat)/dT = Psat * d(ln(Psat))/dT
            """
            coeffs = cobj.config.parameter_data["pressure_sat_comp_coeff"]
            z = cobj.psat_offset + cobj.psat_scale * T

            # Horner method to compute ln(Psat) and its derivative w.r.t z
            ln_psat = 0
            dln_psat_dz = 0
            for i, _ in coeffs.items():
                a_i = getattr(cobj, f"pressure_sat_comp_coeff_{i}")
                dln_psat_dz = z * dln_psat_dz + ln_psat
                ln_psat = z * ln_psat + a_i

            # Analogous to exp_horner_stable_and_der() function in polynomial_evaluation in thermo package
            val = exp(ln_psat) * pyunits.Pa
            der = dln_psat_dz * val
            dpsat_dT = der * cobj.psat_scale

            # Return with appropriate units
            units = b.params.get_metadata().derived_units
            return pyunits.convert(dpsat_dT, units.PRESSURE / units.TEMPERATURE)


class HEOSFitLIQ:
    """HEOS_FIT property class for liquid phase heat capacity, enthalpy and entropy,
    built from pre‑fitted polynomial coefficients in the thermo/chemical property package.

    """

    class cp_mol_liq_comp(Var):
        @staticmethod
        def build_parameters(cobj):
            # Heat capacity correlation coefficients
            coeff_dict = cobj.config.parameter_data["cp_mol_liq_comp_coeff"]
            int_keys = sorted(coeff_dict.keys())  # e.g. [0,1,2,3,...,n]

            # Build all parameters for cp_mol_liq_comp_coeff
            for idx, i in enumerate(int_keys):
                # 1) Create a Var for term "i"
                var_name = f"cp_mol_liq_comp_coeff_{i}"
                cp_unit = coeff_dict[i][1]  # e.g. J/mol/K
                setattr(
                    cobj,
                    var_name,
                    Var(
                        doc=f"Polynomial coefficient a_{i} for Cp(T)",
                        units=cp_unit,
                    ),
                )
                # 2) Hook it up to your parameter_data via set_param_from_config
                set_param_from_config(
                    cobj,
                    param="cp_mol_liq_comp_coeff",  # your dict key
                    index=str(i),  # this picks out coeffs[i]
                )

            # Set up the offset and scale for z-scaling
            offset = cobj.config.parameter_data["liq_offset"]
            scale = cobj.config.parameter_data["liq_scale"]
            # 3) Create a Var for the offset
            var_name = "liq_offset"
            setattr(
                cobj,
                var_name,
                Var(
                    doc="Offset for z-scaling in Cp(T) and H(T)",
                    units=offset[1],
                ),
            )
            # Set parameter_data for the offset
            set_param_from_config(
                cobj,
                param="liq_offset",  # your dict key
                index=None,
            )
            var_name = "liq_scale"
            # 4) Create a Var for the scale
            setattr(
                cobj,
                var_name,
                Var(
                    doc="Scale for z-scaling in Cp(T) and H(T) and S(T)",
                    units=scale[1],
                ),
            )
            # Set parameter_data for scale
            set_param_from_config(
                cobj,
                param="liq_scale",  # your dict key
                index=None,  # this picks out coeffs[i]
            )

            # Variables for enthalpy coefficients are the integrated cp coefficients
            b_coeff_dict = cobj.config.parameter_data["cp_mol_liq_comp_int_coeff"]
            b_keys = sorted(b_coeff_dict.keys())  # e.g. [0,1,2,3,...,n]
            for idx, i in enumerate(b_keys):
                # 1) Create a Var for term "i"
                var_name = f"cp_mol_liq_comp_int_coeff_{i}"
                setattr(
                    cobj,
                    var_name,
                    Var(
                        doc=f"Polynomial coefficient b_{i} for H(T)",
                        units=b_coeff_dict[i][1],
                    ),
                )
                # 2) Hook it up to your parameter_data via set_param_from_config
                set_param_from_config(
                    cobj,
                    param="cp_mol_liq_comp_int_coeff",  # your dict key
                    index=str(i),  # this picks out coeffs[i]
                )

            # For entropy correlations we need the integrated coefficients over T:
            coeff_dict = cobj.config.parameter_data["cp_mol_liq_comp_int_T_coeff"]
            int_keys = sorted(coeff_dict.keys())  # e.g. [0,1,2,3,...,n]

            for idx, i in enumerate(int_keys):
                # 1) Create a Var for term "i"
                var_name = f"cp_mol_liq_comp_int_T_coeff_{i}"
                cp_unit = coeff_dict[i][1]  # e.g. J/mol/K
                setattr(
                    cobj,
                    var_name,
                    Var(
                        doc=f"Polynomial coefficient c_{i} for S(T)",
                        units=cp_unit,
                    ),
                )
                # 2) Hook it up to your parameter_data via set_param_from_config
                set_param_from_config(
                    cobj,
                    param="cp_mol_liq_comp_int_T_coeff",  # your dict key
                    index=str(i),  # this picks out coeffs[i]
                )

            # Log coefficient for entropy correlation
            log_coeff = cobj.config.parameter_data["cp_mol_liq_comp_int_T_log_coeff"]
            # 3) Create a Var for the log coefficient
            var_name = "cp_mol_liq_comp_int_T_log_coeff"
            setattr(
                cobj,
                var_name,
                Var(
                    doc="Log coefficient for entropy calculation",
                    units=log_coeff[1],
                ),
            )
            # Set parameter_data for the log coefficient
            set_param_from_config(
                cobj,
                param="cp_mol_liq_comp_int_T_log_coeff",  # your dict key
                index=None,
            )

        @staticmethod
        def return_expression(b, cobj, T):
            """
            Liquid molar heat capacity, Cp(T) = sum_i a_i * T**i
            Coeffs a_i must be stored in b.params.cp_mol_liq_comp_coeff[comp].
            Using the parameters that were built in build_parameters().
            """
            coeffs = cobj.config.parameter_data["cp_mol_liq_comp_coeff"]

            # Build normalized z for the polynomial
            z = cobj.liq_offset + cobj.liq_scale * T
            expr = 0
            for i, _ in coeffs.items():
                a_i = getattr(cobj, f"cp_mol_liq_comp_coeff_{i}")
                expr = expr * z + a_i

            # units = b.params.get_metadata().derived_units
            return expr * pyunits.J / pyunits.mol / pyunits.K

    class enth_mol_liq_comp:
        @staticmethod
        def build_parameters(cobj):
            """Should be already done in cp_mol_liq_comp.build_parameters()"""
            # Check if the parameters are already built
            if not hasattr(cobj, "cp_mol_liq_comp_int_coeff_0"):
                HEOSFitLIQ.cp_mol_liq_comp.build_parameters(cobj)

            # Set formation enthalpy
            if cobj.parent_block().config.include_enthalpy_of_formation:
                units = cobj.parent_block().get_metadata().derived_units

                cobj.enth_mol_form_liq_comp_ref = Var(
                    doc="Liquid phase molar heat of formation @ Tref",
                    units=units.ENERGY_MOLE,
                )
                set_param_from_config(cobj, param="enth_mol_form_liq_comp_ref")

        @staticmethod
        def return_expression(b, cobj, T):
            """
            Liquid molar enthalpy, using pre‑integrated polynomial
            In thermo: horner_stable(T2, int_coeffs, offset, scale)- horner_stable(T1, int_coeffs, offset, scale)
            """
            b_coeffs = cobj.config.parameter_data["cp_mol_liq_comp_int_coeff"]
            T = pyunits.convert(T, to_units=pyunits.K)
            Tr = pyunits.convert(b.params.temperature_ref, to_units=pyunits.K)

            z = cobj.liq_offset + cobj.liq_scale * T
            expr_T = 0
            for i, _ in b_coeffs.items():
                b_i = getattr(cobj, f"cp_mol_liq_comp_int_coeff_{i}")
                expr_T = expr_T * z + b_i

            z_ref = cobj.liq_offset + cobj.liq_scale * Tr
            expr_T_ref = 0
            for i, _ in b_coeffs.items():
                b_i = getattr(cobj, f"cp_mol_liq_comp_int_coeff_{i}")
                expr_T_ref = expr_T_ref * z_ref + b_i

            # TODO: Unit handling a bit messy at the moment because of the horner method
            h = (expr_T - expr_T_ref) * pyunits.J / pyunits.mol

            if hasattr(cobj, "enth_mol_form_liq_comp_ref"):
                h += cobj.enth_mol_form_liq_comp_ref

            return h

    class entr_mol_liq_comp:
        @staticmethod
        def build_parameters(cobj):
            """Should be already done in cp_mol_liq_comp.build_parameters()"""
            # Check if the parameters are already built
            if not hasattr(cobj, "cp_mol_liq_comp_int_T_coeff_0"):
                HEOSFitLIQ.cp_mol_liq_comp.build_parameters(cobj)

            # Standard entropy
            units = cobj.parent_block().get_metadata().derived_units

            cobj.entr_mol_form_liq_comp_ref = Var(
                doc="Liquid phase molar entropy of formation @ Tref",
                units=units.ENTROPY_MOLE,
            )
            set_param_from_config(cobj, param="entr_mol_form_liq_comp_ref")

        @staticmethod
        def return_expression(b, cobj, T):
            """
            Liquid molar entropy, using pre‑integrated polynomial + log term:
            S(T) = horner_stable_log(T2) - horner_stable_log(Tref)

            Where:
            horner_stable_log(x) = horner_stable(x, coeffs, offset, scale) + b_log * log(x)
            """
            # Units
            T = pyunits.convert(T, to_units=pyunits.K)
            Tr = pyunits.convert(b.params.temperature_ref, to_units=pyunits.K)

            offset = cobj.liq_offset
            scale = cobj.liq_scale
            coeffs = cobj.config.parameter_data["cp_mol_liq_comp_int_T_coeff"]
            log_coeff = cobj.cp_mol_liq_comp_int_T_log_coeff

            # Entropy at T
            z_T = offset + scale * T
            expr_T = 0
            for i, _ in coeffs.items():
                c_i = getattr(cobj, f"cp_mol_liq_comp_int_T_coeff_{i}")
                expr_T = expr_T * z_T + c_i
            expr_T += log_coeff * log(value(T))

            # Entropy at Tr
            z_Tr = offset + scale * Tr
            expr_Tr = 0
            for i, _ in coeffs.items():
                c_i = getattr(cobj, f"cp_mol_liq_comp_int_T_coeff_{i}")
                expr_Tr = expr_Tr * z_Tr + c_i
            expr_Tr += log_coeff * log(value(Tr))

            S = (expr_T - expr_Tr) * pyunits.J / pyunits.mol / pyunits.K
            if hasattr(cobj, "entr_mol_form_liq_comp_ref"):
                S += cobj.entr_mol_form_liq_comp_ref

            return S

    class dens_mol_liq_comp:
        @staticmethod
        def build_parameters(cobj):
            """Build parameters for liquid molar density calculation.

            Note: The thermo package fits molar volume (m³/mol), so these coefficients
            represent a polynomial fit for molar volume. The density is calculated
            by taking the reciprocal of the molar volume.
            """
            # Molar volume correlation coefficients (will be inverted for density)
            coeff_dict = cobj.config.parameter_data["dens_mol_liq_comp_coeff"]
            int_keys = sorted(coeff_dict.keys())  # e.g. [0,1,2,3,...,n]

            # Build all parameters for dens_mol_liq_comp_coeff
            for idx, i in enumerate(int_keys):
                # 1) Create a Var for term "i"
                var_name = f"dens_mol_liq_comp_coeff_{i}"
                # Units are for molar volume (e.g., m³/mol)
                vol_unit = coeff_dict[i][1]
                setattr(
                    cobj,
                    var_name,
                    Var(
                        doc=f"Polynomial coefficient a_{i} for molar volume(T)",
                        units=vol_unit,
                    ),
                )
                # 2) Hook it up to your parameter_data via set_param_from_config
                set_param_from_config(
                    cobj,
                    param="dens_mol_liq_comp_coeff",  # your dict key
                    index=str(i),  # this picks out coeffs[i]
                )

            # Set up the offset and scale for z-scaling
            offset = cobj.config.parameter_data["density_offset"]
            scale = cobj.config.parameter_data["density_scale"]

            # 3) Create a Var for the offset
            var_name = "density_offset"
            setattr(
                cobj,
                var_name,
                Var(
                    doc="Offset for z-scaling in molar volume(T)",
                    units=offset[1],
                ),
            )
            # Set parameter_data for the offset
            set_param_from_config(
                cobj,
                param="density_offset",  # your dict key
                index=None,
            )

            # 4) Create a Var for the scale
            var_name = "density_scale"
            setattr(
                cobj,
                var_name,
                Var(
                    doc="Scale for z-scaling in molar volume(T)",
                    units=scale[1],
                ),
            )
            # Set parameter_data for scale
            set_param_from_config(
                cobj,
                param="density_scale",  # your dict key
                index=None,
            )

        @staticmethod
        def return_expression(b, cobj, T):
            """
            Liquid molar density, calculated as the reciprocal of molar volume.

            The thermo package fits molar volume (not density directly):
            V_m(T) = sum_i A_i * z**i  [m³/mol]
            where z = offset + scale * T

            Then density is calculated as:
            rho_m(T) = 1/V_m(T)  [mol/m³]

            Coeffs A_i must be stored in b.params.dens_mol_liq_comp_coeff[comp].
            """
            coeffs = cobj.config.parameter_data["dens_mol_liq_comp_coeff"]

            # Build normalized z for the polynomial
            z = cobj.density_offset + cobj.density_scale * T

            # Calculate molar volume using polynomial
            molar_volume = 0
            for i, _ in coeffs.items():
                a_i = getattr(cobj, f"dens_mol_liq_comp_coeff_{i}")
                molar_volume = molar_volume * z + a_i

            # Density is reciprocal of molar volume
            molar_density = 1 / molar_volume * pyunits.mol / pyunits.m**3

            # units = b.params.get_metadata().derived_units
            # pyunits.convert(molar_density, units.DENSITY_MOLE)
            return molar_density
